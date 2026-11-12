"""Deep dives: when one cause (deploys) takes a big share of agent time, a focused second analysis runs.

The deterministic part runs here, inside `view`, every time the trigger fires (rules/deepdive.json):
  * which agent time belongs to the cause, across every slice (a deploy is often waited on in 'Sleep/poll loops'
    and checked in 'Network, git & APIs', not only run in 'Deploys'),
  * episodes (one deploy and the calls around it), how they were waited on, how often they came back to back,
  * the platform, and checks of the project's deploy files on disk (Dockerfile cache mounts, layer order, ...),
  * coded tips whose conditions are these facts.
The AI part is a subagent the skill starts with the brief written next to the view (OUT/<name>.deepdive-<id>.md);
its notes come back through notes.json under 'deepdive:<id>' and show with an AI badge.
"""
import fnmatch
import os
import re
import statistics

from . import drill, repo

_SKIP = {"node_modules", ".git", ".next", "dist", "build", "coverage", ".venv", "venv", "__pycache__", ".turbo", ".cache",
         "target", "vendor", ".claude"}
_WAIT_CATS = ("poll_wait",)
_LABEL_CATS = ("poll_wait", "deploy", "network")


def fmt_h(sec):
    sec = float(sec or 0)
    if sec >= 3570:
        return "%.1f h" % (sec / 3600)
    if sec >= 59.5:
        return "%d min" % round(sec / 60)
    return "%d s" % round(sec)


def _rx(pat):
    try:
        return re.compile(pat, re.I | re.M) if pat else None
    except re.error:
        return None


class _Spec:
    def __init__(self, raw, rs):
        self.raw = raw
        self.id = str(raw.get("id") or "deep")
        self.title = raw.get("title") or self.id
        self.match = _rx(raw.get("match"))
        self.exclude = _rx(raw.get("exclude"))
        self.run = _rx(raw.get("run"))
        self.segments = set(raw.get("segments") or [])
        self.platforms = [(p.get("id"), p.get("label"), _rx(p.get("regex"))) for p in raw.get("platforms") or [] if isinstance(p, dict)]
        cfg = drill._seg_cfg(rs)
        self.deploy_parts = cfg.get("_deploy") or []

    def matches(self, what, waited_for="", label="", cat=""):
        texts = [what or "", waited_for or ""]
        if cat.split(":")[0] in _LABEL_CATS:
            texts.append(label or "")
        if self.exclude and what and self.exclude.search(what.strip()) and not waited_for:
            return False
        return bool(self.match) and any(t and self.match.search(t) for t in texts)

    def is_run(self, what):
        if self.run and self.run.search(what or ""):
            return True
        return any(rx.search(what or "") for rx in self.deploy_parts)


# ---------------------------------------------------------------- facts from the timeline

def _leaf_share(spec, leaves, seg_of):
    """Agent seconds of the cause, by slice; and the matched items (what -> seconds, count, slice)."""
    by_seg, items, via_main = {}, {}, 0.0
    for lf in leaves:
        seg = seg_of(lf)
        if not (seg in spec.segments or lf.get("cat") == "deploy" and "deploys" in spec.segments
                or spec.matches(lf.get("what"), lf.get("waited_for"), lf.get("label"), lf.get("cat") or "")):
            continue
        sec = float(lf.get("seconds") or 0)
        by_seg[seg] = by_seg.get(seg, 0.0) + sec
        what = (lf.get("waited_for") and "waits for " + lf["waited_for"]) or lf.get("what") or "?"
        it = items.setdefault((what, seg), {"what": what, "seconds": 0.0, "segment": seg, "insts": set()})
        it["seconds"] += sec
        if lf.get("inst"):
            it["insts"].add(tuple(lf["inst"]))
        if lf.get("via") == "main session":
            via_main += sec
    out = sorted(({"what": v["what"], "seconds": round(v["seconds"], 1), "count": len(v["insts"]), "segment": v["segment"]}
                  for v in items.values()), key=lambda x: -x["seconds"])
    return by_seg, out, via_main


def _calls(spec, analyses, rs, win):
    """Every call of the cause in the window, with its role: run (starts a deploy), wait (poll) or check."""
    lo, hi = win
    calls, cache = [], {}
    for a in analyses:
        sess = {S["session_id"]: S for S in a["sessions"]}
        labels = drill.spawner_labels(a)
        for S in a["sessions"]:
            sid = S["session_id"]
            for tid in S["tool_order"]:
                t = S["tools"][tid]
                if t["name"] in ("Agent", "Task", "Workflow") or t.get("t0") is None:
                    continue
                if (lo is not None and t["t0"] < lo) or (hi is not None and t["t0"] > hi):
                    continue
                info = drill.describe(t["cls"], ("tool", sid, tid), sess, labels, rs, cache)
                if not info:
                    continue
                cat = info.get("cat") or t["cls"] or ""
                if not spec.matches(info.get("what"), info.get("waited_for"), info.get("label"), cat) and cat != "deploy":
                    continue
                if t["cls"] in _WAIT_CATS:
                    role = "wait"
                elif cat == "deploy" or spec.is_run(info.get("what")) or spec.is_run(t.get("cmd") or ""):
                    role = "run"
                else:
                    role = "check"
                dur = info.get("dur")
                calls.append({"sid": sid, "group": a["group"]["session_id"], "t0": t["t0"], "dur": dur if dur is not None else 0.0,
                              "role": role, "bg": bool(t.get("bg")), "what": info.get("what") or "", "cmd": t.get("cmd") or info.get("what") or "",
                              "via": info.get("via")})
    calls.sort(key=lambda c: c["t0"])
    return calls


def _episodes(calls, gap_s):
    """Calls of one session less than gap_s apart -> episodes; only those with a run or a wait count as a deploy."""
    by_sid = {}
    for c in calls:
        by_sid.setdefault(c["sid"], []).append(c)
    eps = []
    for lst in by_sid.values():
        cur = None
        for c in lst:
            end = c["t0"] + max(c["dur"], 0.0)
            if cur and c["t0"] - cur["end"] <= gap_s:
                cur["calls"].append(c)
                cur["end"] = max(cur["end"], end)
            else:
                cur = {"start": c["t0"], "end": end, "calls": [c]}
                eps.append(cur)
    eps = [e for e in eps if any(c["role"] in ("run", "wait") for c in e["calls"])]
    eps.sort(key=lambda e: e["start"])
    return eps


def _platforms(spec, calls):
    """[(id, label)] of the platforms the calls name."""
    seen = []
    for pid, label, rx in spec.platforms:
        if rx and any(rx.search(c["cmd"]) or rx.search(c["what"]) for c in calls):
            seen.append((pid, label))
    return seen


# ---------------------------------------------------------------- checks on disk

def _roots(analyses):
    """Repository roots of the sessions' working directories (deleted worktrees map to the main checkout)."""
    roots = []
    for a in analyses:
        for S in [a["main"]]:
            cwd = S.get("cwd")
            if not cwd:
                continue
            d, _ = repo._existing(cwd)
            if not d:
                continue
            top = d
            for p in repo._up(d):
                top = p
            if os.path.isdir(os.path.join(top, ".git")) or os.path.exists(os.path.join(top, ".git")):
                if top not in roots:
                    roots.append(top)
    return roots[:3]


def _find(root, pattern, limit=12):
    """Files under root whose relative path or name matches one of the '|'-separated globs (bounded walk)."""
    pats = [p for p in pattern.split("|") if p]
    out, seen = [], 0
    for d, dirs, files in os.walk(root):
        seen += 1
        if seen > 4000:
            break
        rel = os.path.relpath(d, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = sorted(x for x in dirs if x not in _SKIP and not (x.startswith(".") and x != ".github"))
        if depth >= 5:
            dirs[:] = []
        for f in sorted(files):
            rp = f if rel == "." else os.path.join(rel, f)
            if any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch(rp, p) for p in pats):
                out.append(os.path.join(d, f))
                if len(out) >= limit:
                    return out
    return out


def _read(path, limit=200000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _short_paths(paths, roots):
    def rel(p):
        for r in roots:
            if p.startswith(r + os.sep):
                return os.path.relpath(p, r)
        return os.path.basename(p)
    names = [rel(p) for p in paths]
    return ", ".join(names[:3]) + (" +%d" % (len(names) - 3) if len(names) > 3 else "")


def run_checks(spec, roots, platform_ids=()):
    files = {}
    for key, pat in (spec.raw.get("files") or {}).items():
        found = []
        for r in roots:
            found += _find(r, pat)
        files[key] = found
    out = []
    for chk in spec.raw.get("checks") or []:
        need = chk.get("platforms")
        if need and not set(need) & set(platform_ids or ()):
            continue  # a cloudbuild.yaml in the repo says nothing when the deploys go elsewhere
        if set(chk.get("unless_platforms") or ()) & set(platform_ids or ()):
            continue  # a cache mount does nothing on a builder that starts empty (Cloud Build): cloudbuild-cache covers it
        paths = files.get(chk.get("files")) or []
        if not paths:
            continue
        typ, bad, hit, minor = chk.get("type"), [], [], []
        if typ == "file_has":
            rx = _rx(chk.get("regex"))
            hit = [p for p in paths if rx and rx.search(_read(p))]
            ok = bool(hit)
            bad = [] if ok else paths
        elif typ == "order":
            a, b, soft = _rx(chk.get("first")), _rx(chk.get("then")), _rx(chk.get("minor_if"))
            for p in paths:
                txt = _read(p)
                ma, mb = a.search(txt) if a else None, b.search(txt) if b else None
                if ma and mb and ma.start() < mb.start():
                    line = txt.count("\n", 0, ma.start()) + 1
                    end = txt.find("\n", mb.start())
                    then_line = txt[txt.rfind("\n", 0, mb.start()) + 1: end if end >= 0 else len(txt)]
                    # e.g. the install already has a cache mount: rerunning it only relinks packages, a small gain
                    (minor if soft and soft.search(then_line) else bad).append("%s:%d" % (p, line))
            ok = not bad and not minor
        elif typ == "exists":
            need = chk.get("need") or []
            for p in paths:
                d = os.path.dirname(p)
                cands = [os.path.join(d, n) for n in need] + [os.path.join(r, n) for r in roots for n in need]
                if not any(os.path.exists(c) for c in cands):
                    bad.append(p)
            ok = not bad
        else:
            continue
        shown = _short_paths([b.split(":")[0] if typ != "order" else b for b in bad] if bad else (hit or paths), roots)
        if typ == "order" and (bad or minor):
            shown = ", ".join(_short_paths([b.rsplit(":", 1)[0]], roots) + ":" + b.rsplit(":", 1)[1] for b in (bad or minor)[:3])
        only_minor = bool(minor) and not bad
        text = (chk.get("ok") if ok else chk.get("minor") if only_minor else chk.get("bad")) or chk.get("bad") or chk.get("id")
        item = {"id": chk.get("id"), "ok": ok, "text": text.replace("{files}", shown)}
        if only_minor:
            item["minor"] = True  # shown under the deploy files, not raised as advice
        elif not ok and isinstance(chk.get("tip"), dict):
            item["tip"] = chk["tip"]
        out.append(item)
    return out


# ---------------------------------------------------------------- tips and brief

_COND = re.compile(r"^\s*(\w+)\s*(>=|>|<=|<|==)\s*([\d.]+)\s*$")


def _cond(expr, facts):
    m = _COND.match(expr or "")
    if not m:
        return False
    v = facts.get(m.group(1))
    if not isinstance(v, (int, float)):
        return False
    x = float(m.group(3))
    return {">=": v >= x, ">": v > x, "<=": v <= x, "<": v < x, "==": v == x}[m.group(2)]


def _fill(text, facts):
    return re.sub(r"\{(\w+)\}", lambda m: str(facts.get(m.group(1), m.group(0))), str(text or ""))


def _share(expr, facts):
    """'back_to_back / episodes' -> (10, 25) when both are numbers and 0 <= n <= of, of > 0; else None."""
    m = re.match(r"^\s*(\w+)\s*/\s*(\w+)\s*$", expr or "")
    if not m:
        return None
    n, of = facts.get(m.group(1)), facts.get(m.group(2))
    if not all(isinstance(x, (int, float)) for x in (n, of)) or of <= 0 or not 0 <= n <= of:
        return None
    return n, of


def build(spec_raw, rs, analyses, leaves, seg_of, agent_s, win, labels=None):
    """-> deep dive dict, or None when it does not trigger."""
    spec = _Spec(spec_raw, rs)
    by_seg, items, via_main = _leaf_share(spec, leaves, seg_of)
    total = sum(by_seg.values())
    share = 100.0 * total / agent_s if agent_s else 0.0
    min_s, min_pct = float(spec_raw.get("min_s") or 0), float(spec_raw.get("min_share_pct") or 0)
    or_s = float(spec_raw.get("or_min_s") or 0)
    if total <= 0 or total < min_s or (share < min_pct and not (or_s and total >= or_s)):
        return None
    why = ("%d%% ≥ %d%% of agent time" % (round(share), round(min_pct))) if share >= min_pct else ("%s ≥ %s" % (fmt_h(total), fmt_h(or_s)))
    calls = _calls(spec, analyses, rs, win)
    eps = _episodes(calls, float(spec_raw.get("episode_gap_s") or 900))
    b2b_s = float(spec_raw.get("back_to_back_s") or 1800)
    b2b = sum(1 for p, e in zip(eps, eps[1:]) if e["start"] - p["end"] < b2b_s)
    lens = [e["end"] - e["start"] for e in eps if e["end"] > e["start"]]
    status = sum(1 for c in calls if c["role"] in ("wait", "check"))
    runs = [c for c in calls if c["role"] == "run"]
    fg_long = [c for c in runs if not c["bg"] and c["dur"] > 60]
    n_eps = len(eps)
    facts = {
        "id": spec.id, "total": fmt_h(total), "share": "%d%%" % round(share),
        "episodes": n_eps, "runs": len(runs), "status_calls": status,
        "checks_per_deploy": round(status / n_eps, 1) if n_eps else 0,
        "back_to_back": b2b, "back_to_back_min": int(round(b2b_s / 60)),
        "episode_p50": fmt_h(statistics.median(lens)) if lens else "?",
        "episode_longest": fmt_h(max(lens)) if lens else "?",
        "foreground_long": len(fg_long), "longest_run": fmt_h(max((c["dur"] for c in fg_long), default=0)),
        "main_pct": "%d%%" % round(100.0 * via_main / total) if total else "0%",
    }
    plats = _platforms(spec, calls)
    platforms = [lab for _, lab in plats]
    labels = labels or {}
    seg_txt = ", ".join("%s %s" % (labels.get(sid, sid), fmt_h(sec)) for sid, sec in
                        sorted(by_seg.items(), key=lambda x: -x[1]) if sec >= 30)
    fl = [
        ("Agent time", "%s, %s of agent time" % (facts["total"], facts["share"])),
        ("Where it shows", seg_txt),
        ("Deploys", "%d%s" % (n_eps, " (%d deploy commands)" % len(runs) if runs and len(runs) != n_eps else "")),
        ("Typical deploy", "%s from first to last call, longest %s" % (facts["episode_p50"], facts["episode_longest"]) if lens else ""),
        ("Status checks", "%d calls, %s per deploy" % (status, facts["checks_per_deploy"]) if status else ""),
        ("Back to back", "%d deploys started within %d min of the previous one's end" % (b2b, facts["back_to_back_min"]) if b2b else ""),
        ("In the foreground", "%d deploy calls over 1 min, longest %s" % (len(fg_long), facts["longest_run"]) if fg_long else ""),
        ("Run by", "main session %s of the time" % facts["main_pct"]),
        ("Platform", ", ".join(platforms)),
    ]
    fact_rows = [{"id": "%s.f%d" % (spec.id, i), "label": k, "value": v} for i, (k, v) in enumerate([x for x in fl if x[1]], 1)]
    roots = _roots(analyses)
    checks = run_checks(spec, roots, [pid for pid, _ in plats]) if roots else []
    actions = []
    for tip in spec_raw.get("tips") or []:
        if _cond(tip.get("cond"), facts):
            actions.append({"id": tip.get("id"), "title": _fill(tip.get("title"), facts)[:90], "saving_min": None,
                            "confidence": tip.get("confidence", "low"), "source": "rule",
                            "evidence": _fill(tip.get("evidence"), facts), "why": _fill(tip.get("why"), facts),
                            "arithmetic": "", "before": _fill(tip.get("before"), facts), "after": _fill(tip.get("after"), facts),
                            "ref": tip.get("source"), "same_as": tip.get("same_as")})
            sh = _share(tip.get("same_as_share"), facts)
            if sh and tip.get("same_as"):  # the slice rule's guessed fraction gives way to the measured one
                actions[-1]["same_as_share"] = [sh[0], sh[1], _fill(tip.get("same_as_share_text"), facts)]
    for c in checks:
        tip = c.get("tip")
        if tip:
            actions.append({"id": "dd-" + str(c["id"]), "title": tip.get("title", "")[:90], "saving_min": None, "confidence": "medium",
                            "source": "rule", "evidence": c["text"], "why": tip.get("why", ""), "arithmetic": "",
                            "before": tip.get("before"), "after": tip.get("after"), "ref": tip.get("source")})
    segs = sorted((s for s, v in by_seg.items() if v >= 60 and (v >= 0.1 * total)), key=lambda s: -by_seg[s])
    return {"id": spec.id, "title": spec.title, "icon": spec_raw.get("icon") or "search", "seconds": round(total, 1),
            "share_pct": round(share, 1), "segments": segs, "by_segment": {k: round(v, 1) for k, v in by_seg.items()},
            "trigger": why,
            "facts": fact_rows, "checks": [{k: v for k, v in c.items() if k != "tip"} for c in checks],
            "items": items[:6], "platforms": platforms, "actions": actions[:5], "notes": [],
            "roots": [os.path.basename(r) for r in roots], "brief_file": None}


def build_all(rs, analyses, leaves, seg_of, agent_s, win, labels=None):
    out = []
    for raw in getattr(rs, "deepdive", None) or []:
        if isinstance(raw, dict):
            dd = build(raw, rs, analyses, leaves, seg_of, agent_s, win, labels)
            if dd:
                out.append(dd)
    return out


def render_brief(dd, rs, view):
    """The subagent's prompt for one deep dive (rules/deepdive.json 'brief', filled with the computed facts)."""
    raw = next((r for r in getattr(rs, "deepdive", None) or [] if isinstance(r, dict) and r.get("id") == dd["id"]), None)
    lines = (raw or {}).get("brief") or []
    w = view.get("window") or {}
    vals = {
        "id": dd["id"], "title": view.get("title") or "?", "total": fmt_h(dd["seconds"]), "share": "%d%%" % round(dd["share_pct"]),
        "window": "%s .. %s UTC" % ((w.get("since") or "?")[:16].replace("T", " "), (w.get("until") or "?")[:16].replace("T", " ")),
        "trigger": dd.get("trigger") or "?",
        "facts": "\n".join("- %s %s: %s" % (f["id"], f["label"], f["value"]) for f in dd["facts"]),
        "checks": "\n".join("- %s %s" % ("ok:" if c["ok"] else "minor:" if c.get("minor") else "NOT OK:", c["text"]) for c in dd["checks"]) or "- no deploy files found in %s" % (", ".join(dd.get("roots") or []) or "the project"),
        "items": "\n".join("- %s: %s, %s, in %s" % (it["what"][:90], fmt_h(it["seconds"]), "%d calls" % it["count"] if it["count"] else "-", it["segment"])
                           for it in dd["items"]),
        "platforms": ", ".join(dd.get("platforms") or []) or "not recognised from the commands",
    }
    return "\n".join(re.sub(r"\{(\w+)\}", lambda m: str(vals.get(m.group(1), m.group(0))), ln) for ln in lines) + "\n"
