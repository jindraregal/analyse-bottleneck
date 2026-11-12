"""View model for the one-chart report (schema run-bottleneck/view@1).

Everything here is deterministic: slices come from the leaf attribution in drill.py, items are grouped
command signatures / models / tools, actions are the coded ranked findings mapped to slices. LLM notes
are merged only from a separate notes file and are always marked source "llm".
"""
import os
import re
import statistics
import time
from collections import Counter, defaultdict

from .util import scrub
from .classify import classify_bash
from .drill import MODEL_CATS, drill_analyses, run_key
from .util import iso
from .describe import dedupe, describe
from . import deepdive as deepdive_mod

SCHEMA = "run-bottleneck/view@1"
MAX_ITEMS = 6
MAX_ACTIONS = 3
MIN_ITEM_S, MIN_ITEM_PCT = 60.0, 0.5  # smaller items fold into the slice's rest
DO_FIRST_MIN = 10.0  # the 'Do first' line needs at least this saving (minutes)
MIN_SAVING_MIN = 1.0  # actions that save less are not shown
CONF_RANK = {"high": 2, "medium": 1, "low": 0}
_GENERIC_WORDS = {"npx", "npm", "pnpm", "pnpx", "yarn", "bun", "bunx", "node", "run", "exec", "bash", "sh", "python", "python3",
                  "uv", "env", "time", "sudo", "-m", "-c", "x", "dlx"}


# ---------------------------------------------------------------- formatting

def fmt_h(sec):
    sec = float(sec or 0)
    if sec >= 3600:
        return "%.1f h" % (sec / 3600.0)
    if sec >= 60:
        return "%d min" % round(sec / 60.0)
    if 0 < sec < 0.5:
        return "<1 s"
    return "%d s" % round(sec)


def _cut(text, n):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    if " " in cut[n // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "…"


def project_label(names):
    out = []
    for d in names:
        s = re.sub(r"^-?(?:home|Users)-[^-]+-(?:(?:git|src|code|projects|repos|work)-)?", "", d) or d
        if s not in out:
            out.append(s)
    if not out:
        return "sessions"
    return out[0] if len(out) == 1 else "%s + %d more" % (out[0], len(out) - 1)


# ---------------------------------------------------------------- segments

def _seg_defs(rs):
    cfg = rs.segments or {}
    defs = cfg.get("segments") or [{"id": "other", "label": "Other", "leaf": []}]
    leaf_to = {}
    for d in defs:
        for lf in d.get("leaf", []):
            leaf_to[lf] = d["id"]
    labels = {d["id"]: d["label"] for d in defs}
    labels.setdefault("other", "Other")
    labels.setdefault("unknown_wait", "Waiting — cause unknown")
    order = {d["id"]: i for i, d in enumerate(defs)}
    return cfg, leaf_to, labels, order


def leaf_segment(cat, leaf_to):
    if cat in leaf_to:
        return leaf_to[cat]
    return leaf_to.get(cat.split(":", 1)[0], "other")


def choose_segments(sec_by_seg, total, cfg):
    """Which slices stay visible; the rest fold into 'other'. -> set of visible ids."""
    merge_pct = float(cfg.get("merge_below_pct", 2.0))
    keep_unknown = float(cfg.get("keep_unknown_above_pct", 0.5))
    max_n = int(cfg.get("max_segments", 9))
    min_n = int(cfg.get("min_segments", 4))
    ranked = sorted(((v, k) for k, v in sec_by_seg.items() if v > 0 and k != "other"), reverse=True)
    vis = []
    for v, k in ranked:
        pct = 100.0 * v / total if total else 0
        if pct >= merge_pct or (k == "unknown_wait" and pct > keep_unknown):
            vis.append(k)
    # never fewer than min_n slices when the data has them (other counts as one if it gets anything)
    for v, k in ranked:
        if len(vis) + 1 >= min_n:
            break
        if k not in vis:
            vis.append(k)
    rest = [k for k in sec_by_seg if k not in vis and sec_by_seg[k] > 0]
    room = max_n - (1 if rest else 0)
    if len(vis) > room:
        room = max_n - 1  # cutting slices creates an 'other' slice
        keep_u = "unknown_wait" in vis
        vis_sorted = sorted(vis, key=lambda k: -sec_by_seg[k])
        vis = vis_sorted[:room]
        if keep_u and "unknown_wait" not in vis:
            vis = vis[:-1] + ["unknown_wait"]
    return set(vis)


# ---------------------------------------------------------------- items

def _run_stats(keys, runs, inst):
    """keys: (cat, key) pairs -> (count, durations): every run of them in the window when known, else the runs
    that got seconds."""
    durs, n = [], 0
    for c, k in keys:
        for d in runs.get(run_key(c, k)) or ():
            n += 1
            if d is not None:
                durs.append(d)
    if n < len(inst):
        n, durs = len(inst), [d for d in inst.values() if d is not None]
    return n, durs


def _items(leaves, runs, vruns=None, gkey=None, rgroups=None):
    """Leaves grouped by key (or by gkey(leaf) when given: one-off scripts of one task share a key).
    rgroups: run key -> main sessions it ran in (drill run_groups); an item's session count covers every run,
    like its run count, not only the runs that won time."""
    rgroups = rgroups or {}
    groups = {}
    for lf in leaves:
        k = (gkey(lf) if gkey else None) or lf["key"]
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"whats": Counter(), "label": lf["label"], "seconds": 0.0, "inst": {}, "via": Counter(), "desc": Counter(),
                             "groups": set(), "est": 0.0, "wf": Counter(), "cats": set(), "members": set(), "spec": False,
                             "rdesc": Counter()}
        g["cats"].add(lf.get("cat", ""))
        g["members"].add((lf.get("cat", ""), lf["key"], lf["via"]))
        g["whats"][lf["what"]] += lf["seconds"]
        g["seconds"] += lf["seconds"]
        g["spec"] = g["spec"] or lf.get("cat", "") not in MODEL_CATS
        if lf.get("inst") is not None:
            g["inst"][lf["inst"]] = lf.get("dur")
            g["desc"][lf.get("label") or ""] += lf["seconds"]  # the label describes the runs that took the time
            g["rdesc"][lf.get("desc") or ""] += lf["seconds"]
        if lf.get("est"):
            g["est"] += lf["seconds"]
        if lf.get("waited_for"):
            g["wf"][lf["waited_for"]] += lf["seconds"]
        g["via"][lf["via"]] += lf["seconds"]
        g["groups"].add(lf["group"])
    items = []
    for key, g in groups.items():
        oneoff = key.startswith("oneoff|")
        if oneoff:  # runs of exactly these scripts by exactly this task
            n, durs = _run_stats_via(g["members"], vruns or {}, g["inst"])
            ran_in = set().union(*(rgroups.get(run_key(c, k) + "\t" + via) or () for c, k, via in g["members"]))
        else:
            n, durs = _run_stats([(c, key) for c in g["cats"]], runs, g["inst"])
            ran_in = set().union(*(rgroups.get(run_key(c, key)) or () for c in g["cats"]))
        if key.startswith(("unres|", "gaps")):
            durs = []  # a wait's own length or a gap says nothing about a run
        what = g["whats"].most_common(1)[0][0]
        label = ""
        if g["desc"] and not oneoff:
            lab, lab_s = g["desc"].most_common(1)[0]
            if lab_s >= 0.5 * g["seconds"]:  # one run (or one description) explains most of it; else the signature does
                label = lab
        if oneoff:
            task = task_label(g["via"].most_common(1)[0][0])
            label = "one-off scripts of %s" % task
            what = "%s (+%s)" % (what, _plural(len({m[1] for m in g["members"]}) - 1, "other script"))
        elif key.startswith("poll|") and len(g["whats"]) > 1:
            lab, lab_s = g["rdesc"].most_common(1)[0]
            label = lab if lab and lab_s >= 0.5 * g["seconds"] else "%d polls waiting for %s" % (len(g["whats"]), key[len("poll|"):])
            what = "%s (+%s)" % (what, _plural(len(g["whats"]) - 1, "other poll"))
        label = label or (what if g["desc"] else g["label"])  # gaps etc. have no runs: keep their own label
        it = {"label": _cut(label, 80), "what": _cut(what, 90), "seconds": round(g["seconds"], 1), "count": n,
              "p50_s": round(statistics.median(durs), 1) if durs else None,
              "via": " · ".join(v for v, _ in g["via"].most_common(2)), "sessions": len(g["groups"] | ran_in),
              "_key": key, "_spec": g["spec"], "_via_share": round(g["via"].most_common(1)[0][1] / g["seconds"], 3) if g["seconds"] else 0.0,
              "_n_via": len({_task_key(v) for v in g["via"]})}
        if g["est"] >= max(30.0, 0.05 * g["seconds"]):
            it["est_s"] = round(g["est"], 1)
        if g["wf"]:
            it["waited_for"] = g["wf"].most_common(1)[0][0]
        items.append(it)
    items.sort(key=lambda it: (-it["seconds"], it["what"]))
    return items


def _run_stats_via(members, vruns, inst):
    durs, n = [], 0
    for c, k, via in members:
        for d in vruns.get(run_key(c, k) + "\t" + via) or ():
            n += 1
            if d is not None:
                durs.append(d)
    if n < len(inst):
        n, durs = len(inst), [d for d in inst.values() if d is not None]
    return n, durs


def _listed_s(items, sec):
    return sum(it["seconds"] for it in top_items(items, sec))


def build_items(leaves, runs=None, vruns=None, oneoff=False, rgroups=None):
    """Leaves grouped by key. seconds: time counted in this slice; count/p50_s: all runs of that key in the window
    (a run that overlapped a higher-priority activity gets no seconds but still ran); est_s: seconds from
    background runs without an end notice (an upper bound); waited_for: what a poll loop waited on.

    oneoff: scratch/temp/inline scripts of one task that would not be listed on their own become one item
    'one-off scripts of <task>', task by task (biggest first), each only when it raises the time the listed items
    cover. Scripts that are listed anyway keep their own row."""
    runs = runs or {}
    items = _items(leaves, runs, vruns, rgroups=rgroups)
    if not oneoff:
        return items
    sec = sum(lf["seconds"] for lf in leaves)
    listed = {it["_key"] for it in top_items(items, sec)}
    cand = {}
    cand_s = Counter()
    for lf in leaves:
        if lf.get("scratch") and lf["key"] not in listed:
            cand.setdefault(lf["via"], set()).add(lf["key"])
            cand_s[lf["via"]] += lf["seconds"]
    grouped = {}
    best = _listed_s(items, sec)
    for via in sorted(cand, key=lambda v: (-cand_s[v], v)):
        if len(cand[via]) < 2:
            continue  # one script is already its own item
        trial = dict(grouped)
        for k in cand[via]:
            trial[(via, k)] = "oneoff|" + via
        its = _items(leaves, runs, vruns, lambda lf, t=trial: t.get((lf["via"], lf["key"])), rgroups)
        cov = _listed_s(its, sec)
        if cov > best + 0.5:
            grouped, best, items = trial, cov, its
    for lf in leaves:
        g = grouped.get((lf["via"], lf["key"]))
        if g:
            lf["_gkey"] = g
    return items


def build_other_items(leaves, labels, runs=None, rgroups=None):
    """Items of the 'other' slice: one per small slice folded into it ('Media encode, mostly ffmpeg ...'), so
    a test run that went to a folded slice is not listed next to the real Tests slice as if it were 'other';
    the slice's own leaves (gaps, rare tools) stay as plain items."""
    runs = runs or {}
    by = defaultdict(list)
    for lf in leaves:
        by[lf["seg0"]].append(lf)
    items = build_items(by.pop("other", []), runs, rgroups=rgroups)
    for sid, lv in by.items():
        top = build_items(lv, runs, rgroups=rgroups)[0]
        inst = {}
        for lf in lv:
            if lf.get("inst") is not None:
                inst[lf["inst"]] = lf.get("dur")
        keys = {(lf.get("cat", ""), lf["key"]) for lf in lv if not lf["key"].startswith(("unres|", "gaps"))}
        n, durs = _run_stats(keys, runs, inst)
        if sid == "unknown_wait":
            durs = []
        vs = Counter()  # who ran the folded slice as a whole, not just its top item
        for lf in lv:
            vs[lf["via"]] += lf["seconds"]
        sec = sum(lf["seconds"] for lf in lv)
        items.append({"label": _cut("%s, mostly %s" % (labels.get(sid, sid), top["label"]), 80), "what": top["what"],
                      "seconds": round(sec, 1), "count": n,
                      "p50_s": round(statistics.median(durs), 1) if durs else None,
                      "via": " · ".join(v for v, _ in vs.most_common(2)) or top["via"],
                      "sessions": len({lf["group"] for lf in lv}.union(*((rgroups or {}).get(run_key(c, k)) or () for c, k in keys))),
                      "_via_share": round(vs.most_common(1)[0][1] / sec, 3) if sec and vs else 0.0,
                      "_n_via": len({_task_key(v) for v in vs}) or 1})
    items.sort(key=lambda it: (-it["seconds"], it["what"]))
    return items


# ---------------------------------------------------------------- tasks (who ran it)

MAX_TASKS = 4
TASKS_SHOW_BELOW = 0.70  # 'By task' is shown when the listed items cover less of the slice than this


def task_label(via):
    """'subagent: Batch runner (T-105..T-115) (general-purpose)' -> 'Batch runner (T-105..T-115)';
    'workflow agent: research (nightly)' -> 'research (nightly)'; 'main session' stays."""
    v = re.sub(r"\s+", " ", str(via or "")).strip()
    m = re.match(r"^subagent: (.*?)(?: \([A-Za-z][\w:-]*\))?$", v)
    if m:
        v = m.group(1)
    else:
        v = re.sub(r"^(?:workflow agent|workflow): ", "", v)
        v = re.sub(r"^subagent (\w+) \(spawner unknown\)$", r"subagent \1", v)
    return _cut(v.rstrip(" .…"), 60) or "unknown"


def _task_key(via):
    return task_label(via).lower()


def build_tasks(leaves, listed=None):
    """Seconds of a slice by who ran them (main session, or the subagent's / workflow agent's task description).
    -> (top MAX_TASKS [{label, seconds, items}], rest seconds, number of other tasks, the biggest of them in
    seconds). items: distinct things that
    task ran in the slice. listed: item keys already listed for the slice; the tasks picked are those with the most
    time those items do not already cover (a task that is one listed item adds nothing), shown with their whole
    time, biggest first; a task bigger than the smallest one picked is added (up to 2), so no hidden task is
    bigger than a shown one."""
    listed = listed or set()
    sec, free, keys, labels = Counter(), Counter(), {}, {}
    for lf in leaves:
        k = _task_key(lf["via"])
        sec[k] += lf["seconds"]
        if (lf.get("_gkey") or lf["key"]) not in listed:
            free[k] += lf["seconds"]
        keys.setdefault(k, set()).add(lf["key"])
        labels.setdefault(k, task_label(lf["via"]))
    pick = sorted((k for k in sec if sec[k] > 0), key=lambda k: (-round(free[k], 1), -sec[k], k))[:MAX_TASKS]
    if pick:
        # biggest first must stay true: a task bigger than the smallest one shown is shown too (at most 2 more),
        # else the plain biggest MAX_TASKS are shown
        floor = min(sec[k] for k in pick)
        bigger = [k for k in sec if k not in pick and sec[k] > floor]
        pick = pick + bigger if len(bigger) <= 2 else sorted((k for k in sec if sec[k] > 0), key=lambda k: (-sec[k], k))[:MAX_TASKS]
    pick.sort(key=lambda k: (-sec[k], k))
    top = [{"label": labels[k], "seconds": round(sec[k], 1), "items": len(keys[k])} for k in pick]
    rest = [k for k in sec if k not in pick and sec[k] > 0]
    return top, round(sum(sec[k] for k in rest), 1), len(rest), round(max([sec[k] for k in rest] or [0.0]), 1)


def coverage(leaves, items, tasks):
    """-> (items share, tasks share, union share) of the slice's seconds. Items count only when they name what ran
    (a model name alone does not)."""
    total = sum(lf["seconds"] for lf in leaves) or 1.0
    ik = {it.get("_key") for it in items if it.get("_spec", True)}
    tk = {t["label"].lower() for t in tasks}
    i_s = t_s = u_s = 0.0
    for lf in leaves:
        a = (lf.get("_gkey") or lf["key"]) in ik
        b = _task_key(lf["via"]) in tk
        i_s += lf["seconds"] if a else 0.0
        t_s += lf["seconds"] if b else 0.0
        u_s += lf["seconds"] if (a or b) else 0.0
    return i_s / total, t_s / total, u_s / total


def top_items(items, sec):
    """The items worth a row: at least max(60 s, 0.5 % of the slice), at most MAX_ITEMS; synthetic model rows never."""
    items = [it for it in items if it["what"] != "<synthetic>"]
    floor = max(MIN_ITEM_S, MIN_ITEM_PCT / 100.0 * sec)
    top = [it for it in items if it["seconds"] >= floor][:MAX_ITEMS]
    return top or items[:1]


# ---------------------------------------------------------------- actions

def _imperative(c):
    """First clause of the rule's fix: 'Keep calls under the TTL (background ..., with ...)' -> 'Keep calls under the TTL'."""
    fix = (c.get("fix") or "").strip()
    if not fix:
        return _cut(c.get("title") or c.get("id"), 70)
    first = re.split(r"(?<=[.;])\s+|;\s+|\s+—\s+", fix)[0].rstrip(" .;")
    short = re.split(r"\s+\(|,\s+or\s+|,\s+with\s+", first)[0].rstrip(" ,.;:")
    if len(short) >= 12:
        first = short
    return _cut(first, 70)


_UNITS = re.compile(r"\b(\d{5,}) units\b")


def _units(text):
    """'12345678 units' -> '12.3M weighted tokens' (quota units are tokens weighted by their price)."""
    def h(m):
        v = float(m.group(1))
        return ("%.1fM" % (v / 1e6) if v >= 1e6 else "%dk" % round(v / 1e3)) + " weighted tokens"
    return _UNITS.sub(h, text or "")


def _arith(c):
    a = c.get("arithmetic") or {}
    parts = []
    if isinstance(a, dict):
        for k, lab in (("time", "time"), ("tokens", "quota"), ("assumption", "assumption")):
            v = a.get(k)
            if v and v != "n/a":
                parts.append("%s: %s" % (lab, _units(str(v))))
    elif a:
        parts.append(_units(str(a)))
    if c.get("saving_pct_of_units"):
        parts.append("quota: ~%s%% of weighted tokens" % c["saving_pct_of_units"])
    return scrub("; ".join(parts), 400)


def _finding_cmd(c, report):
    cid = c["id"]
    q = report.get("quota") or {}
    if cid in ("repeated_commands", "retry_loops", "templated_loops"):
        top = (q.get(cid) or {}).get("top") or []
        if top:
            return top[0].get("cmd") or top[0].get("template")
    for f in report.get("stack_findings") or []:
        if f.get("id") == cid and f.get("examples"):
            return f["examples"][0].get("raw") or f["examples"][0].get("cmd")
    return None


def finding_segment(c, report, rs, cfg, leaf_to, visible):
    """-> visible segment id for a ranked finding, or None (not shown)."""
    fmap = cfg.get("finding_segments") or {}
    cid = c["id"]
    target = fmap.get(cid, "@cmd" if c.get("kind") == "stack_rule" else ("@env" if cid.startswith("env_") else "other"))
    if target is None:
        return None
    cands = target if isinstance(target, list) else [target]
    for t in cands:
        if t == "@cmd":
            cmd = _finding_cmd(c, report)
            t = leaf_segment(classify_bash(cmd, rs), leaf_to) if cmd else "other"
        elif t == "@env":
            sig = next((s for s in (report.get("environment") or {}).get("signals", []) if "env_" + s["id"] == cid), None)
            cls = (sig or {}).get("classes") or []
            t = leaf_segment(cls[0], leaf_to) if cls else "other"
        if t in visible:
            return t
    return "other" if "other" in visible else None


def _words(text):
    return {w for w in re.split(r"[\s/]+", text.lower()) if w and w not in _GENERIC_WORDS and not w.startswith("-")}


def _off_slice(c, report, rs, info):
    """True when an action does not fit the slice it landed in: a fix that is no instruction ('See the signal
    hint'), a command example that names none of the slice's items, or the polling rule where the slice's
    main items already are single-call blocking loops."""
    if (c.get("fix") or "").strip().lower().startswith("see the signal hint"):
        return True
    if info is None or info["id"] == "other":
        return False
    items = info["items"]
    if c["id"] == "polling":
        loops = sum(it["seconds"] for it in info.get("all_items") or items if re.match(r"^(until|while) ", it["what"]))
        return loops >= 0.5 * info["seconds"]
    if c.get("kind") == "stack_rule":
        cmd = _finding_cmd(c, report)
        if cmd:
            from .drill import signature
            ex = _words(signature(cmd, rs)[0] or "")
            have = set()
            for it in items:
                have |= _words(it["what"])
            return bool(ex) and not (ex & have)
    return False


def _action(c, cap_min, before=None):
    mins = c.get("saving_minutes") or 0.0
    arith = _arith(c)
    if cap_min is not None and mins > cap_min:
        mins = cap_min
        arith = (arith + "; " if arith else "") + "capped at this slice's %s" % fmt_h(cap_min * 60)
    return {"id": c["id"], "title": _imperative(c), "saving_min": round(mins, 1) if mins > 0 else None,
            "confidence": c.get("confidence", "low"), "source": "rule", "evidence": scrub(_units(c.get("evidence") or ""), 300),
            "why": scrub(c.get("why") or "", 300), "arithmetic": arith,
            "before": scrub(before or c.get("before"), 200) or None, "after": scrub(c.get("after"), 200) or None,
            "ref": c.get("ref") or None}


def build_actions(report, rs, cfg, leaf_to, visible, seg_info=None):
    """Coded findings mapped to slices, then the slice rules (rules/segments.json segment_rules). Savings are
    capped at the slice's own time; actions saving under a minute and actions that do not fit their slice are
    dropped; per slice the biggest savings come first."""
    seg_info = seg_info or {}
    out = defaultdict(list)
    for c in report.get("ranked") or []:
        seg = finding_segment(c, report, rs, cfg, leaf_to, visible)
        if seg is None or _off_slice(c, report, rs, seg_info.get(seg)):
            continue
        info = seg_info.get(seg)
        ex = _finding_cmd(c, report) if c.get("kind") == "stack_rule" else None
        a = _action(c, info["seconds"] / 60.0 if info else None, before=("e.g. " + ex) if ex else None)
        if a["saving_min"] is not None and a["saving_min"] < MIN_SAVING_MIN:
            continue
        if (c.get("saving_minutes") or 0) > 0 and a["saving_min"] is None:
            continue
        out[seg].append(a)
    for r in cfg.get("segment_rules") or []:
        a = segment_rule_action(r, seg_info.get(r.get("segment")))
        if a:
            out[r["segment"]].append(a)
    for seg, lst in out.items():
        lst.sort(key=lambda a: (-(a["saving_min"] or 0), -CONF_RANK.get(a["confidence"], 0)))
        out[seg] = lst[:MAX_ACTIONS]
    return out


def segment_rule_action(r, info):
    """A rule about a whole slice (e.g. deploys), filled from the slice's own items: {n} runs of all items,
    {p50} median of the top item, {total} the slice's time, {what} the top item's command."""
    if not info or not info["all_items"] or info["pct"] < float(r.get("min_pct", 0)):
        return None
    items = info["all_items"]
    n = sum(it["count"] for it in items)
    if n < int(r.get("min_runs", 1)):
        return None
    top = items[0]
    vals = {"n": n, "p50": fmt_h(top["p50_s"]) if top.get("p50_s") else "n/a", "total": fmt_h(info["seconds"]), "what": top["what"]}

    def fill(key):
        try:
            return (r.get(key) or "").format(**vals)
        except (KeyError, IndexError, ValueError):
            return r.get(key) or ""
    frac = r.get("saving_fraction")
    saving = info["seconds"] * float(frac) / 60.0 if frac else None
    if saving is not None:
        arith = "time: %s in this slice x %.2f = %s; assumption: %s" % (fmt_h(info["seconds"]), float(frac), fmt_h(saving * 60), fill("assumption"))
    else:
        arith = "assumption: " + fill("assumption") if r.get("assumption") else ""
    return {"id": r["id"], "title": _cut(fill("title"), 70), "saving_min": round(saving, 1) if saving else None,
            "confidence": r.get("confidence", "low"), "source": "rule", "evidence": scrub(fill("evidence"), 300),
            "why": scrub(fill("why"), 300), "arithmetic": scrub(arith, 400),
            "before": scrub(fill("before"), 200) or None, "after": scrub(fill("after"), 200) or None,
            "ref": r.get("source") or None}


def rescale_same_as(segs, dds, rs):
    """A deep-dive tip with same_as_share [n, of, text] replaces the fixed saving_fraction of the slice rule it
    repeats with the measured share (e.g. 10 of 25 deploys started right after the previous one). Only lowers."""
    fracs = {r.get("id"): r.get("saving_fraction") for r in (rs.segments or {}).get("segment_rules") or []}
    by_id = {a.get("id"): a for s in segs for a in s.get("actions") or []}
    for dd in dds:
        for t in dd.get("actions") or []:
            a, sh = by_id.get(t.get("same_as")), t.get("same_as_share")
            frac = fracs.get(t.get("same_as"))
            if not (a and sh and a.get("saving_min") and frac):
                continue
            n, of, text = sh
            slice_s = a["saving_min"] * 60.0 / float(frac)
            new = slice_s * n / of / 60.0
            if new >= a["saving_min"]:
                continue
            a["saving_min"] = round(new, 1) if new >= MIN_SAVING_MIN else None
            a["arithmetic"] = scrub("time: %s in this slice x %d/%d = %s; measured: %s; upper bound: those deploys could "
                                    "have waited for the next fix" % (fmt_h(slice_s), n, of, fmt_h(new * 60), text), 400)
            a["evidence"] = scrub("%s; %s" % (a.get("evidence") or "", text), 300)


def do_first(segs):
    """The one action to start with: the biggest estimated saving on the page (medium/high confidence first)."""
    best = None
    for s in segs:
        for a in s["actions"]:
            if not a.get("saving_min") or a["saving_min"] < DO_FIRST_MIN:
                continue
            k = (a["confidence"] != "low", a["saving_min"])
            if best is None or k > best[0]:
                best = (k, {"title": a["title"], "saving_min": a["saving_min"], "segment": s["id"], "label": s["label"], "id": a["id"]})
    return best[1] if best else None


# ---------------------------------------------------------------- view

def _who(it):
    """Who ran an item, for one-line summaries -> (text, shared): the spawner when it ran at least half of the
    item's time, else 'N tasks' with shared=True ('' when unknown)."""
    first = (it.get("via") or "").split(" · ")[0]
    if not first:
        return "", False
    if it.get("_via_share", 1.0) >= 0.5 or it.get("_n_via", 1) <= 1:
        return first, False
    return "%d tasks" % it["_n_via"], True


def _summary(seg, items, total_s):
    if not items:
        return _cut("%s, nothing more specific recorded." % fmt_h(seg), 110)
    it = items[0]
    bits = ["'%s' %s" % (_cut(it["label"], 44), fmt_h(it["seconds"]))]
    if it["count"] > 1:
        bits.append("%d runs" % it["count"] if not it["what"].startswith(("claude", "unknown model")) else "%d requests" % it["count"])
    if it.get("p50_s") and it["count"] > 1:
        bits.append("p50 %s" % fmt_h(it["p50_s"]))
    who, shared = _who(it)
    s = ", ".join(bits) + ((", across %s" if shared else ", via %s") % who if who else "")
    return _cut(s + ".", 110)


def _via_short(via):
    """'subagent: e2e verify (general-purpose)' -> "subagent 'e2e verify'"."""
    m = re.match(r"^(subagent|workflow agent|workflow): (.*?)(?: \([^)]*\))?$", via or "")
    return "%s '%s'" % (m.group(1), m.group(2)) if m else (via or "")


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _lower_first(label):
    """'Network, git & APIs' -> 'network, git & APIs'; keeps acronyms ('CI runs')."""
    if len(label) > 1 and label[1].islower():
        return label[0].lower() + label[1:]
    return label


def _headline(agent_s, segs):
    if not segs:
        return "No agent time in this window."
    top = segs[0]
    head = "%s agent time. Biggest: %s %s" % (fmt_h(agent_s), _lower_first(top["label"]), fmt_h(top["seconds"]))
    if top["items"]:
        it = top["items"][0]
        # 'mostly' only when the item is at least half of the slice; 'run by' only when one task ran at least half of it
        lead = "mostly" if top["seconds"] and it["seconds"] >= 0.5 * top["seconds"] else "biggest item"
        who, shared = _who(it)
        by = (" across %s" % who) if shared else (" in the main session" if who == "main session"
                                                   else " run by %s" % _via_short(who) if who else "")
        tail = ", %s '%s'" % (lead, it["label"])
        room = 140 - len(head) - 1
        if len(tail) + len(by) > room:
            by = ""
        if len(tail) > room:
            tail = ", %s '%s'" % (lead, _cut(it["label"], max(8, room - 14 - len(lead))))
        head += tail + by
    return _cut(head + ".", 140)


def build_view(report, analyses, opts):
    """report: core.build_report output (ranked list complete); analyses: core.analyze_group outputs.
    opts: rs (RuleSet), title, since/until (epoch or None), idle_threshold, sysstat (bool), generated (epoch)."""
    return build_view_checked(report, analyses, opts)[0]


def build_view_checked(report, analyses, opts):
    """-> (view, check). check: leaf sum vs autonomous time and the unresolved seconds by reason."""
    rs = opts["rs"]
    cfg, leaf_to, labels, order = _seg_defs(rs)
    win = (opts.get("since"), opts.get("until"))
    d = drill_analyses(analyses, rs, win, opts.get("idle_threshold", 180.0))
    wall = report["wall_clock"]
    agent_s = wall["autonomous_s"]
    raw_by_seg = defaultdict(float)
    for lf in d["leaves"]:
        lf["seg0"] = leaf_segment(lf["cat"], leaf_to)
        raw_by_seg[lf["seg0"]] += lf["seconds"]
    total = sum(raw_by_seg.values())
    visible = choose_segments(raw_by_seg, total, cfg)
    by_seg = defaultdict(list)
    for lf in d["leaves"]:
        by_seg[lf["seg0"] if lf["seg0"] in visible else "other"].append(lf)
    if by_seg.get("other"):
        visible.add("other")
    runs = d.get("runs") or {}
    segs, info = [], {}
    vruns = d.get("runs_by_via") or {}
    rgroups = d.get("run_groups") or {}
    cover = {}
    for sid in visible:
        lv = by_seg.get(sid) or []
        sec = sum(lf["seconds"] for lf in lv)
        if sec <= 0:
            continue
        items = build_other_items(lv, labels, runs, rgroups) if sid == "other" else build_items(lv, runs, vruns, oneoff=True, rgroups=rgroups)
        top = top_items(items, sec)
        tasks, t_rest, t_rest_n, t_rest_max = build_tasks(lv, {it.get("_key") for it in top if it.get("_spec", True)})
        cov = coverage(lv, top, tasks) if sid != "other" else (sum(i["seconds"] for i in top) / sec, 0.0, sum(i["seconds"] for i in top) / sec)
        cover[sid] = {"items_pct": round(100 * cov[0], 1), "tasks_pct": round(100 * cov[1], 1), "union_pct": round(100 * cov[2], 1)}
        pct = round(100.0 * sec / (agent_s or total), 1) if (agent_s or total) else 0.0
        info[sid] = {"id": sid, "seconds": sec, "pct": pct, "items": top, "all_items": items}
        sg = {"id": sid, "label": labels.get(sid, sid), "seconds": round(sec, 1), "pct": pct,
              "summary": _summary(sec, top, total), "items": top,
              "rest_s": round(max(0.0, sec - sum(i["seconds"] for i in top)), 1), "rest_n": max(0, len(items) - len(top)),
              "tasks": tasks, "tasks_rest_s": t_rest, "tasks_rest_n": t_rest_n, "tasks_rest_max_s": t_rest_max,
              # 'By task' only where the listed items leave much of the slice unexplained (or only name models)
              "tasks_show": bool(tasks) and sid != "other" and cov[0] < TASKS_SHOW_BELOW and len(tasks) + t_rest_n > 1,
              "actions": [], "notes": []}
        segs.append(sg)
    actions = build_actions(report, rs, cfg, leaf_to, visible, info)
    for sg in segs:
        sg["actions"] = actions.get(sg["id"], [])
    segs.sort(key=lambda s: (-s["seconds"], order.get(s["id"], 99)))
    since = opts.get("since") if opts.get("since") is not None else min((a["tl"]["lo"] for a in analyses if a["tl"].get("lo") is not None), default=None)
    until = opts.get("until") if opts.get("until") is not None else max((a["tl"]["hi"] for a in analyses if a["tl"].get("hi") is not None), default=None)
    n_sub = sum(len(a["subs"]) for a in analyses)
    view = {
        "schema": SCHEMA,
        "title": opts.get("title") or project_label(sorted({a["group"]["project"] for a in analyses})),
        "generated": iso(opts.get("generated") or time.time()),
        "window": {"since": iso(since), "until": iso(until), "sessions": len(analyses), "subagent_sessions": n_sub,
                   "transcript_files": len(analyses) + n_sub},
        "totals": {"agent_s": round(agent_s, 1), "span_s": wall["span_s"],
                   "excluded": [{"id": "human", "label": "Waiting for you", "seconds": wall["human_s"]},
                                {"id": "idle", "label": "Idle (nobody working)", "seconds": wall["idle_s"]}]},
        "headline": "",
        "segments": segs,
        "basis": [],
    }
    view["headline"] = _headline(agent_s, segs)
    for sg in segs:
        for it in sg["items"]:
            if it.get("_n_via", 1) > 1:
                it["n_tasks"] = it["_n_via"]  # distinct tasks that ran it; the page says 'by X +N-1', not '+1' for the 2 named
            for k in ("_key", "_spec", "_via_share", "_n_via"):
                it.pop(k, None)
    for sg in segs:
        for it in sg["items"]:
            it["short"], it["short_src"] = describe(rs, it.get("what"), it.get("label"), "", sg["id"])
        dedupe(rs, sg["items"])
    view["waiting"] = build_waiting(analyses, rs)
    # deep dives: a cause with a big share of agent time (deploys) gets a focused second analysis (rules/deepdive.json)
    dds = deepdive_mod.build_all(rs, analyses, d["leaves"], lambda lf: lf["seg0"] if lf["seg0"] in visible else "other",
                                 agent_s, win, labels)
    shown = {a.get("id") for s in segs for a in s.get("actions") or []}
    rescale_same_as(segs, dds, rs)
    for dd in dds:  # a slice rule that already says the same (and carries the estimate) wins over the deep-dive tip
        dd["actions"] = [a for a in dd["actions"] if not (a.get("same_as") and a["same_as"] in shown)]
    if dds:
        view["deep_dives"] = dds
    df = do_first(segs)
    if df:
        view["do_first"] = df
    view["basis"] = _basis(view, d, report, dict(opts, dup_records=sum(S.get("dup_records", 0) for a in analyses for S in a["sessions"])))
    check = {"leaf_sum_s": round(total, 1), "autonomous_s": round(d["autonomous"], 1), "report_autonomous_s": round(agent_s, 1),
             "capped_s": round(d.get("capped_s", 0.0), 1),
             "unresolved_s": {k: round(v, 1) for k, v in sorted(d["unresolved"].items())}, "by_cat": d["by_cat"],
             "coverage": cover}
    return view, check


def _basis(view, d, report, opts):
    w = view["window"]
    unres = sum(d["unresolved"].values())
    ex = view["totals"]["excluded"]
    agent_s = view["totals"].get("agent_s") or 0.0
    rows = [
        {"what": "Transcripts read", "how": "~/.claude/projects JSONL, streamed", "kind": "deterministic",
         "detail": "%s + %s, %s to %s UTC." % (_plural(w["sessions"], "session"), _plural(w["subagent_sessions"], "subagent/workflow transcript"), (w["since"] or "?")[:16].replace("T", " "), (w["until"] or "?")[:16].replace("T", " "))},
        {"what": "Slice sizes", "how": "each second counted once, to the most specific activity", "kind": "deterministic",
         "detail": "Agent slices are agent time; the hatched slice is the time the main sessions waited for you (%s); parked sessions (%s) and idle time (%s) are left out. Both add up per session, so sessions open at the same time each count." % (
             fmt_h((view.get("waiting") or {}).get("seconds", ex[0]["seconds"])), fmt_h((view.get("waiting") or {}).get("parked_s", 0)), fmt_h(ex[1]["seconds"]))},
        {"what": "What exactly", "how": "a wait is shown as what the subagent or background command was doing", "kind": "deterministic",
         "detail": "Commands unwrapped (wrappers, cd, chains) and grouped by signature; labels are the agent's own descriptions. Runs and medians count every run in the window, also runs that overlapped something counted first."},
        {"what": "Actions", "how": "coded rules in rules/*.json", "kind": "deterministic",
         "detail": "Savings are estimates, capped at the slice's time; each shows its arithmetic and assumption."},
    ]
    if opts.get("dup_records"):
        rows.append({"what": "Forked sessions", "how": "copied records counted once", "kind": "deterministic",
                     "detail": "%d records that forked or resumed sessions and forked subagents copy from another transcript were skipped (same record id)." % opts["dup_records"]})
    ss = (report.get("telemetry") or {}).get("sysstat")
    used = any(a["id"].startswith("host_") for sg in view.get("segments") or [] for a in sg.get("actions") or [])
    if opts.get("sysstat") and ss and not ss.get("error"):
        if used:
            rows.append({"what": "Host load", "how": "sysstat archives", "kind": "measured", "detail": "Host-wide CPU/memory/disk samples joined to the window."})
    else:
        rows.append({"what": "Host load", "how": "not collected", "kind": "missing", "detail": "CPU, memory and disk are not in transcripts; rerun with --sysstat."})
    lat = (report.get("telemetry") or {}).get("latency") or {}
    if lat.get("matched"):
        rows.append({"what": "Model request times", "how": "request sent → last token", "kind": "measured",
                     "detail": "%d requests timed; model time in the chart is from response timestamps." % lat["matched"]})
    if unres > 0:
        names = {"none": "no child transcript", "idle": "child idle between steps", "nested": "nested wait, no transcript"}
        parts = ", ".join("%s %s" % (names.get(k, k), fmt_h(v)) for k, v in sorted(d["unresolved"].items(), key=lambda kv: -kv[1]))
        rows.append({"what": "Unresolved waits", "how": "no child activity at that moment", "kind": "missing",
                     "detail": _cut("%s of waiting could not be traced to a command (%s); shown as 'cause unknown'." % (fmt_h(unres), parts), 200)})
    cap = d.get("capped_s") or 0.0
    if cap >= 60:
        rows.append({"what": "Background runs without an end", "how": "no completion notice in the transcript", "kind": "missing",
                     "detail": "%s (%.0f%% of agent time) is from background commands that never reported their end; counted until the session's last record or 30 min, marked ≤." % (fmt_h(cap), 100.0 * cap / (agent_s or 1))})
    dora = (report.get("telemetry") or {}).get("dora")
    if dora:
        m = dora.get("metrics") or {}
        df, lt, cfr = m.get("deployment_frequency_per_day") or {}, m.get("lead_time_for_changes_h") or {}, m.get("change_failure_rate") or {}
        bits = ["%s mainline changes in %g days" % (dora.get("mainline_changes", "?"), dora.get("window_days") or 0)]
        if df.get("value") is not None:
            bits.append("%.1f/day, %s" % (df["value"], re.sub(r"\s*\((.*)\)$", r": \1", df.get("kind") or "?")))
        if lt.get("p50") is not None:
            bits.append("lead time p50 %.1f h, p90 %.1f h" % (lt["p50"], lt.get("p90") or 0))
        if cfr.get("value") is not None:
            bits.append("%s (%.1f%%)" % (_plural(dora.get("reverts") or 0, "revert"), 100 * cfr["value"]))
        rows.append({"what": "Delivery (DORA)", "how": "git history via analyze.py dora", "kind": "measured",
                     "detail": _cut("; ".join(bits) + ". Not part of the chart.", 200)})
    for dd in view.get("deep_dives") or []:
        rows.append({"what": "Deep dive: %s" % dd["title"].lower(), "how": "triggered by the share of agent time (rules/deepdive.json)", "kind": "deterministic",
                     "detail": _cut("Triggered: %s. Facts are counted from the transcripts; checks read the project's files as they are today, not as they were during the runs. The AI part is a subagent started with the generated brief." % dd["trigger"], 300)})
    rows.append({"what": "AI notes", "how": "none on this page", "kind": "none",
                 "detail": "Everything above is computed. A model may add notes per slice; they are marked AI."})
    return rows


# ---------------------------------------------------------------- waiting for you

_DEFAULT_PARTS = [{"id": "prompt", "cat": "human_wait:prompt", "label": "Your next message"},
                  {"id": "permission", "cat": "human_wait:permission", "label": "Permission prompts"},
                  {"id": "approval", "cat": "human_wait:approval", "label": "Approval gate"},
                  {"id": "ask", "cat": "human_wait:ask", "label": "Questions to you"},
                  {"id": "away", "cat": "human_wait:away", "label": "Session parked", "in_ring": False}]


def _waited_tool(S, s, e, rs):
    """The tool call that stood still during a person-wait [s, e] -> its command signature or tool name."""
    from .drill import signature
    best = None
    for tid in S["tool_order"]:
        t = S["tools"][tid]
        t1 = t["t1"] if t["t1"] is not None else t.get("bg_end")
        if t1 is None or t["t0"] >= e or t1 <= s:
            continue
        ov = min(e, t1) - max(s, t["t0"])
        if best is None or ov > best[0]:
            best = (ov, t)
    if best is None:
        return ""
    t = best[1]
    if t["name"] == "Bash" and t.get("cmd"):
        key = signature(t["cmd"], rs)[0]
        return scrub(re.sub(r"\b(GATE_REASON|APPROVAL_REASON)=(\"[^\"]*\"|'[^']*'|\S+)\s*", "", key or ""), 80) or "Bash"
    return t["name"]


def build_waiting(analyses, rs):
    """Time the main sessions waited on a person (excluded from agent time), by kind, with deterministic tips
    (rules/waiting.json). Each kind: seconds, episodes, median, longest, sessions; for your next message also
    how many of those turns ended with a question; for prompts, gates and questions the commands or tools that
    waited most often."""
    import bisect
    cfg = getattr(rs, "waiting", None) or {}
    parts = cfg.get("parts") or _DEFAULT_PARTS
    by_cat = {p["cat"]: p for p in parts}
    acc = {p["id"]: {"eps": [], "q": 0, "q_s": 0.0, "top": Counter(), "top_s": Counter(), "ses": set()} for p in parts}
    for a in analyses:
        S = a["main"]
        reqs = sorted(((r["end"], r) for r in S["requests"].values()), key=lambda x: x[0])
        ends = [x[0] for x in reqs]
        for sl in a["tl"]["slices"]:
            c = sl[2]
            if not c or not c.startswith("human_wait"):
                continue
            p = by_cat.get(c) or by_cat.get("human_wait:ask")
            if p is None:
                continue
            d, sec = acc[p["id"]], sl[1] - sl[0]
            if sec <= 0:
                continue
            d["eps"].append(sec)
            d["ses"].add(a["group"]["session_id"])
            if p["id"] == "prompt":
                i = bisect.bisect_right(ends, sl[0] + 2.0) - 1
                if i >= 0 and reqs[i][1].get("ends_q"):
                    d["q"] += 1
                    d["q_s"] += sec
            elif p["id"] != "away":
                w = _waited_tool(S, sl[0], sl[1], rs)
                if w:
                    d["top"][w] += 1
                    d["top_s"][w] += sec
    out_parts, parked = [], 0.0
    for p in parts:
        d = acc[p["id"]]
        if not d["eps"]:
            continue
        if p.get("in_ring") is False:
            parked += sum(d["eps"])
            continue
        eps = d["eps"]
        top = [{"what": w, "short": describe(rs, w)[0], "count": n, "seconds": round(d["top_s"][w], 1)}
               for w, n in sorted(d["top"].items(), key=lambda kv: (-kv[1], -d["top_s"][kv[0]], kv[0]))[:4]]
        out_parts.append({"id": p["id"], "label": p.get("label") or p["id"], "help": p.get("help") or "",
                          "seconds": round(sum(eps), 1), "count": len(eps), "p50_s": round(statistics.median(eps), 1),
                          "longest_s": round(max(eps), 1), "sessions": len(d["ses"]),
                          "questions": d["q"], "questions_s": round(d["q_s"], 1), "top": top})
    out_parts.sort(key=lambda x: -x["seconds"])
    actions = []
    for r in cfg.get("rules") or []:
        p = next((x for x in out_parts if x["id"] == r.get("part")), None)
        a = waiting_rule_action(r, p)
        if a:
            actions.append(a)
    actions.sort(key=lambda a: (-(a["saving_min"] or 0), -CONF_RANK.get(a["confidence"], 0)))
    kept = []  # up to MAX_ACTIONS tips for you and as many for the agent: the page lists them apart
    for who in ("you", "agent"):
        kept += [a for a in actions if a["for"] == who][:MAX_ACTIONS]
    actions = [a for a in actions if a in kept]
    return {"id": "waiting", "label": "Waiting for you", "seconds": round(sum(x["seconds"] for x in out_parts), 1),
            "parked_s": round(parked, 1), "parts": out_parts, "actions": actions, "notes": []}


def _wait_basis(r, p):
    """(seconds the saving_fraction applies to, how it was counted) for a waiting rule; see rules/waiting.json."""
    b = r.get("saving_basis") or "seconds"
    if b == "questions_s":
        return p["questions_s"], "%s after %d stops that ended with a question" % (fmt_h(p["questions_s"]), p["questions"])
    if b == "extra_p50":
        extra = max(0, p["count"] - p["sessions"])
        return extra * p["p50_s"], "(%d waits - %s) x median %s = %s" % (p["count"], _plural(p["sessions"], "session"),
                                                                         fmt_h(p["p50_s"]), fmt_h(extra * p["p50_s"]))
    return p["seconds"], "%s waiting" % fmt_h(p["seconds"])


def waiting_rule_action(r, p):
    if not p or p["count"] < int(r.get("min_count", 1)) or p["questions"] < int(r.get("min_questions", 0)):
        return None
    if p["p50_s"] < float(r.get("min_p50_s", 0)):
        return None
    top = p["top"][0] if p["top"] else {}
    vals = {"n": p["count"], "total": fmt_h(p["seconds"]), "p50": fmt_h(p["p50_s"]), "longest": fmt_h(p["longest_s"]),
            "q": p["questions"], "q_total": fmt_h(p["questions_s"]), "top": top.get("what") or "?", "top_n": top.get("count") or 0,
            "sessions": _plural(p["sessions"], "session")}

    def fill(key):
        try:
            return (r.get(key) or "").format(**vals)
        except (KeyError, IndexError, ValueError):
            return r.get(key) or ""
    frac = r.get("saving_fraction")
    base_s, base_txt = _wait_basis(r, p)
    saving = base_s * float(frac) / 60.0 if frac else None
    if saving is not None and saving < MIN_SAVING_MIN:
        saving = None
    arith = ""
    if saving:
        arith = ("time: %s" % base_txt) if float(frac) == 1.0 else "time: %s x %.2f = %s" % (base_txt, float(frac), fmt_h(saving * 60))
    if r.get("assumption") and saving:
        arith += "; assumption: " + fill("assumption")
    return {"id": r["id"], "for": "agent" if r.get("for") == "agent" else "you",
            "title": _cut(fill("title"), 70), "saving_min": round(saving, 1) if saving else None,
            "confidence": r.get("confidence", "low"), "source": "rule", "evidence": scrub(fill("evidence"), 300),
            "why": scrub(fill("why"), 300), "arithmetic": scrub(arith, 400),
            "before": scrub(fill("before"), 200) or None, "after": scrub(fill("after"), 200) or None,
            "ref": r.get("source") or None}


# ---------------------------------------------------------------- notes

def merge_notes(view, notes):
    """View with LLM notes attached (source 'llm'); the deterministic headline is never replaced."""
    if not notes:
        return view
    from .view_render import merge_notes as render_merge
    return render_merge(view, notes)


# ---------------------------------------------------------------- brief for a model

def render_brief(view, max_lines=60):
    """Compact text of the view for an LLM: per slice its items (numbered) and actions, with numbers."""
    for k_items, k_act in ((5, 2), (4, 2), (3, 2), (3, 1), (2, 1), (1, 1), (1, 0)):
        lines = _brief_lines(view, k_items, k_act)
        if len(lines) <= max_lines:
            return "\n".join(lines) + "\n"
    return "\n".join(lines[:max_lines]) + "\n"


def _brief_lines(view, k_items, k_act):
    w, t = view["window"], view["totals"]
    ex = {e["id"]: e["seconds"] for e in t["excluded"]}
    L = ["# %s: where agent time went (%s)" % (view["title"], SCHEMA),
         "Window %s .. %s UTC; %s, %s. Agent time %s (span %s, summed per session; excluded: waiting for you %s, idle %s)."
         % ((w["since"] or "?")[:16], (w["until"] or "?")[:16], _plural(w["sessions"], "session"), _plural(w["subagent_sessions"], "subagent transcript"),
            fmt_h(t["agent_s"]), fmt_h(t["span_s"]), fmt_h(ex.get("human")), fmt_h(ex.get("idle"))),
         "Headline: " + view["headline"],
         "All numbers below are computed; item ids (e.g. tests.1) are what notes should cite. Time = counted in the slice;"
         " Nx and p50 = every run in the window; <= = upper bound (background runs without an end notice)."]
    for s in view["segments"]:
        L.append("## %s [%s] %s = %.1f%%" % (s["label"], s["id"], fmt_h(s["seconds"]), s["pct"]))
        for i, it in enumerate(s["items"][:k_items], 1):
            p50 = ", p50 %s" % fmt_h(it["p50_s"]) if it.get("p50_s") is not None else ""
            lab = "" if it["label"] == it["what"] else " \"%s\"" % _cut(it["label"], 50)
            cnt = ", %dx" % it["count"] if it["count"] else ""
            wf = " (waits for %s)" % it["waited_for"] if it.get("waited_for") else ""
            le = "<=" if it.get("est_s") else ""
            L.append("- %s.%d %s%s%s: %s%s%s%s, %s, via %s" % (s["id"], i, _cut(it["what"], 70), wf, lab, le, fmt_h(it["seconds"]), cnt, p50, _plural(it["sessions"], "session"), _cut(it["via"], 70) + (" (%d tasks)" % it["n_tasks"] if it.get("n_tasks", 0) > 2 else "") + (" [shown as: %s]" % it["short"] if it.get("short") else "")))
        hidden = s["rest_s"] + sum(it["seconds"] for it in s["items"][k_items:])
        n_hidden = s.get("rest_n", 0) + max(0, len(s["items"]) - k_items)
        if hidden >= 60:
            L.append("- rest %s%s" % (fmt_h(hidden), " in %d smaller items" % n_hidden if n_hidden > 1 else ""))
        if s.get("tasks_show") and s.get("tasks"):
            bits = ["%s %s" % (_cut(t["label"], 44), fmt_h(t["seconds"])) for t in s["tasks"]]
            if s.get("tasks_rest_s", 0) >= 60:
                each = " (each <= %s)" % fmt_h(s["tasks_rest_max_s"]) if s.get("tasks_rest_n", 0) > 1 and s.get("tasks_rest_max_s") else ""
                bits.append("%s%s %s" % (_plural(s.get("tasks_rest_n", 0), "other task"), each, fmt_h(s["tasks_rest_s"])))
            L.append("- by task: " + " · ".join(bits))
        for a in s["actions"][:k_act]:
            sv = "saves ~%s" % fmt_h(a["saving_min"] * 60) if a.get("saving_min") else "time saving n/a"
            L.append("- action %s (%s, %s): %s | %s" % (a["id"], a["confidence"], sv, a["title"], _cut(a["evidence"], 90)))
    wt = view.get("waiting") or {}
    if wt.get("parts"):
        tot = (t["agent_s"] or 0) + wt["seconds"]
        L.append("## Waiting for you [waiting] %s (not agent time; %.0f%% of agent time + waiting%s)" % (fmt_h(wt["seconds"]), 100.0 * wt["seconds"] / tot if tot else 0,
                 "; parked sessions %s not counted" % fmt_h(wt["parked_s"]) if wt.get("parked_s", 0) >= 60 else ""))
        for i, p in enumerate(wt["parts"], 1):
            q = ", %d ended with a question (%s)" % (p["questions"], fmt_h(p["questions_s"])) if p.get("questions") else ""
            top = "; most: " + ", ".join("%s %dx" % (_cut(x["what"], 40), x["count"]) for x in p["top"][:2]) if p.get("top") else ""
            L.append("- waiting.%d %s: %s, %dx, p50 %s, longest %s%s%s" % (i, p["label"], fmt_h(p["seconds"]), p["count"], fmt_h(p["p50_s"]), fmt_h(p["longest_s"]), q, top))
        for a in wt.get("actions", [])[:k_act]:
            L.append("- action %s (%s): %s | %s" % (a["id"], a["confidence"], a["title"], _cut(a["evidence"], 90)))
    for dd in view.get("deep_dives") or []:
        L.append("## Deep dive: %s [deepdive:%s] %s = %.0f%% of agent time (%s)" % (dd["title"], dd["id"], fmt_h(dd["seconds"]), dd["share_pct"], dd["trigger"]))
        L.append("- " + "; ".join("%s %s: %s" % (f["id"], f["label"], _cut(f["value"], 70)) for f in dd["facts"][:6]))
        bad = [c["text"] for c in dd.get("checks") or [] if not c.get("ok")]
        if bad:
            L.append("- not ok on disk: " + "; ".join(_cut(b, 70) for b in bad[:3]))
        L.append("- START A SUBAGENT with the prompt in %s; merge its JSON into notes.json (key deepdive:%s)" % (dd.get("brief_file") or "<name>.deepdive-%s.md" % dd["id"], dd["id"]))
    if view.get("do_first"):
        df = view["do_first"]
        L.insert(3, "Do first: %s (saves ~%s, %s)" % (df["title"], fmt_h(df["saving_min"] * 60), df["segment"]))
    miss = [b for b in view.get("basis", []) if b["kind"] == "missing"]
    if miss:
        L.append("Missing: " + "; ".join("%s: %s" % (b["what"], _cut(b["detail"], 90).rstrip(".")) for b in miss) + ".")
    return L
