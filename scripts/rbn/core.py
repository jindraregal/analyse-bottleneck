"""Orchestration: discover transcripts, analyse sessions, aggregate, rank findings."""
import glob
import json
import os
import re
import statistics
from collections import Counter, defaultdict

from . import __version__, repo, telemetry
from .parse import parse_session
from .quota import analyze_quota
from .stack import evaluate
from .timeline import HEAVY, analyze_timeline, build_intervals, child_activity, child_breakdown, heavy_overlap, union_children, RANK
from .util import fmt_dur, iso, norm_command, redact

CONF_W = {"high": 1.0, "medium": 0.7, "low": 0.4}
CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


STALL_FG_S = 1800


def projects_root():
    return os.environ.get("RB_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects")


def encode_dir(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def find_groups(project=None, all_=False, session=None, since=None, root=None):
    root = root or projects_root()
    if session and os.path.isfile(session):
        return [_group(session)]
    try:
        dirs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []
    if all_:
        sel = dirs
    elif project:
        p = project.lower()
        exact = [d for d in dirs if d == project or d == encode_dir(project)]
        # a full directory name or path selects exactly that project, a fragment selects all matches
        sel = exact or [d for d in dirs if p in d.lower()]
    else:
        enc = encode_dir(os.getcwd())
        sel = [d for d in dirs if d == enc]
    files = []
    for d in sel:
        for f in glob.glob(os.path.join(root, d, "*.jsonl")):
            files.append(f)
    if session == "latest":
        files = sorted(files, key=os.path.getmtime)[-1:]
    elif session:
        files = [f for f in files if os.path.basename(f).startswith(session)]
    out = []
    for f in sorted(files, key=os.path.getmtime):
        if since is not None and os.path.getmtime(f) < since:
            continue
        out.append(_group(f))
    return out


def _group(path):
    sid = os.path.basename(path)[:-6]
    base = os.path.join(os.path.dirname(path), sid, "subagents")
    # Agent/Task subagents (nested ones too) sit flat in subagents/; workflow agents in subagents/workflows/<run>/
    subs = sorted(glob.glob(os.path.join(base, "agent-*.jsonl"))) + sorted(glob.glob(os.path.join(base, "workflows", "*", "agent-*.jsonl")))
    return {"main": path, "subs": subs, "project": os.path.basename(os.path.dirname(path)), "session_id": sid}


def top_commands(sessions, per_class=4):
    agg = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))
    for S in sessions:
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["t1"] is None:
                continue
            key = norm_command(t["cmd"], 110) if t["cmd"] else t["name"]
            a = agg[t["cls"]][redact(key, 110)]
            a[0] += 1
            a[1] += t["dur"]
            a[2] += t["err"]
    out = {}
    for cls, d in agg.items():
        rows = sorted(d.items(), key=lambda kv: -kv[1][1])[:per_class]
        out[cls] = [{"cmd": k, "runs": v[0], "seconds": round(v[1], 1), "errors": v[2]} for k, v in rows]
    return out


def env_report(sessions, rs, quota, overlap_total, comps):
    ev = defaultdict(lambda: {"count": 0, "seconds": 0.0, "conf": Counter(), "examples": [], "tools": set()})
    seen = set()
    for S in sessions:
        for sg in S["signals"]:
            key = (S["file"], sg["id"], sg.get("tid") or sg["ts"])
            if key in seen:
                continue
            seen.add(key)
            e = ev[sg["id"]]
            e["count"] += 1
            e["conf"][sg["conf"]] += 1
            t = S["tools"].get(sg.get("tid"))
            if t and t["t1"]:
                e["seconds"] += t["dur"]
            e["tools"].add(sg["cls"])
            if len(e["examples"]) < 2:
                e["examples"].append(redact(sg["snippet"], 140))
    sig_meta = {s["id"]: s for s in rs.signals}
    signals = []
    for sid, e in sorted(ev.items(), key=lambda kv: -kv[1]["count"]):
        top_conf = max(e["conf"], key=lambda c: CONF_ORDER.get(c, 0))
        signals.append({"id": sid, "label": sig_meta.get(sid, {}).get("label", sid), "count": e["count"], "confidence": top_conf,
                        "confidence_counts": dict(e["conf"]), "tool_seconds_upper_bound": round(e["seconds"], 1),
                        "classes": sorted(e["tools"]), "examples": e["examples"], "hint": sig_meta.get(sid, {}).get("hint", "")})
    timeouts = sum(1 for S in sessions for t in S["tools"].values() if t["timed_out"])
    interrupts = sum(1 for S in sessions for t in S["tools"].values() if t["interrupted"])
    stalls = [t["dur"] for S in sessions for t in S["tools"].values()
              if t["name"] == "Bash" and not t.get("bg") and t["t1"] and t["dur"] > STALL_FG_S]
    comp = [c for S in sessions for c in S["compactions"]]
    api_err = sum(S["api_errors"] for S in sessions)
    api_txt = Counter()
    for S in sessions:
        api_txt.update(S["api_error_texts"])
    return {
        "note": "Transcripts hold no CPU/memory telemetry. These are inferred from tool output and timing; confidence labels say how directly.",
        "signals": signals, "long_foreground_calls": {"count": len(stalls), "seconds": round(sum(stalls), 1), "confidence": "low",
                                  "basis": "foreground Bash calls longer than %ds: above the tool's own timeout cap, so likely a suspended or stalled session (or a raised limit); their time is counted as measured but may not be real work" % STALL_FG_S},
        "tool_timeouts": timeouts, "tool_interrupts": interrupts, "api_errors": api_err,
        "api_error_kinds": [{"text": k, "count": v} for k, v in api_txt.most_common(3)],
        "compactions": {"count": len(comp), "pre_tokens_total": sum(c.get("pre_tokens") or 0 for c in comp),
                        "duration_s": round(sum((c.get("dur_ms") or 0) for c in comp) / 1000.0, 1)},
        "heavy_overlap": {"seconds": round(overlap_total, 1), "confidence": "low", "basis": "two build/test/media/install tool calls were open at the same time, in one or in different sessions/subagents (timing only)"},
    }


def contention_hint(sessions, overlaps_by_sid):
    out = []
    groups = defaultdict(list)
    for S in sessions:
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["cls"] in ("build", "test", "media_gpu", "install") and t["cmd"] and t["t1"]:
                groups[(S["file"], norm_command(t["cmd"]))].append(t)
    for (f, key), ts in groups.items():
        if len(ts) < 3:
            continue
        durs = [t["dur"] for t in ts]
        med = statistics.median(durs)
        ov = overlaps_by_sid.get(f, [])
        slow = [d for t, d in zip(ts, durs) if d > 2.0 * med and any(o["start"] < t["t0"] + t["dur"] and o["start"] + o["seconds"] > t["t0"] for o in ov)]
        if slow:
            out.append({"cmd": redact(key, 90), "median_s": round(med, 1), "slow_runs_overlapping_other_heavy_jobs": len(slow), "max_s": round(max(durs), 1)})
    out.sort(key=lambda d: -d["max_s"])
    return out[:3]


def claim_order(groups):
    """Indexes of groups in the order they claim transcript records. A forked or resumed session copies earlier
    records (same uuid) into a new file; the group with the most subagent transcripts, then the largest main
    file, keeps them, so waits stay next to their subagent transcripts and nothing is counted twice."""
    def size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    return sorted(range(len(groups)), key=lambda i: (-len(groups[i]["subs"]), -size(groups[i]["main"]), groups[i]["main"]))


def _agent_id(path):
    b = os.path.basename(path)
    return b[len("agent-"):-len(".jsonl")] if b.startswith("agent-") and b.endswith(".jsonl") else b


def analyze_groups(groups, rs, win, opts):
    """analyze_group over all groups with records deduplicated across main transcripts; keeps the input order.

    Subagent transcripts are pooled across groups: a resumed or forked session that claimed the copied records
    (and with them the Agent calls) also gets the subagent transcripts those calls started, even when they sit
    in the original session's folder. Each subagent transcript is analysed in one group only."""
    seen = set()
    pool = {}
    for g in groups:
        for p in g["subs"]:
            pool.setdefault(_agent_id(p), p)
    claimed = set()
    res = [None] * len(groups)
    for i in claim_order(groups):
        res[i] = analyze_group(groups[i], rs, win, opts, seen, pool=pool, claimed=claimed)
    return [a for a in res if a]


def _sub_order(path):
    """Parse order of subagent transcripts: originals before forks, shallow before nested. A fork copies its
    parent agent's records (same uuid) into its own file; the original must claim them first."""
    try:
        with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {}
    m = m if isinstance(m, dict) else {}
    depth = m.get("spawnDepth")
    return (bool(m.get("isFork") or m.get("agentType") == "fork"), depth if isinstance(depth, int) else 0, path)


def analyze_group(g, rs, win, opts, seen=None, pool=None, claimed=None):
    """seen: record uuids already counted (shared across groups); subagent transcripts use it too, so a fork's
    copy of its parent agent's records is skipped and their tool runs and requests are counted once."""
    seen = set() if seen is None else seen
    main = parse_session(g["main"], rs, win, seen=seen)
    if main["first"] is None:
        return None
    claimed = set() if claimed is None else claimed
    paths = sorted((p for p in g["subs"] if p not in claimed), key=_sub_order)
    claimed.update(paths)
    subs = [parse_session(p, rs, win, is_sub=True, seen=seen) for p in paths]
    frontier = [main] + subs
    while pool and frontier:  # subagents started from this group's records whose transcripts live elsewhere
        want = []
        for S in frontier:
            for t in S["tools"].values():
                p = pool.get(t.get("agent_id") or "")
                if p and p not in claimed:
                    claimed.add(p)
                    want.append(p)
        frontier = [parse_session(p, rs, win, is_sub=True, seen=seen) for p in sorted(want, key=_sub_order)]
        subs += frontier
    subs = [s for s in subs if s["first"] is not None]
    spans = {s["session_id"][len("agent-"):]: (s["first"], s["last"]) for s in subs}
    idle = opts.get("idle_threshold", 180.0)
    fill = child_activity(subs, rs, spans, win)
    tl = analyze_timeline(main, rs, idle, spans, win, fill=fill)
    sub_tl = [analyze_timeline(s, rs, 1e12, None, win) for s in subs]
    by = dict(tl["by_cat"])
    inside = {}
    if sub_tl and tl["slices"]:
        un = union_children([x["slices"] for x in sub_tl], tl["lo"], tl["hi"])
        inside = child_breakdown(tl["slices"], un)
    sessions = [main] + subs
    heavy = []
    for S in sessions:
        ivs, _ = build_intervals(S, rs, spans)
        heavy += [iv for iv in ivs if iv[2] in HEAVY and iv[4] is not None]
    return {"heavy_ivs": heavy, "group": g, "main": main, "subs": subs, "tl": tl, "sub_tl": sub_tl, "by": by, "inside_children": inside,
            "sessions": sessions, "fill": fill}


def _cat_rollup(by):
    r = defaultdict(float)
    for k, v in by.items():
        r[k.split(":", 1)[0]] += v
    return r


def session_summary(a, rs, quota, stack, env):
    tl = a["tl"]
    span = tl["span"]
    auto = tl["autonomous"]
    by = {k: round(v, 1) for k, v in sorted(tl["by_cat"].items(), key=lambda kv: -kv[1])}
    sums = round(sum(tl["by_cat"].values()), 1)
    cs = a["main"]["cost_state"]
    rep = None
    if cs:
        rep = {"total_cost_usd": cs.get("totalCostUSD"), "api_duration_s": round((cs.get("totalAPIDuration") or 0) / (1000.0 if (cs.get("totalAPIDuration") or 0) > 10000 else 1), 1) if cs.get("totalAPIDuration") else None,
               "tool_duration_s": round((cs.get("totalToolDuration") or 0) / (1000.0 if (cs.get("totalToolDuration") or 0) > 10000 else 1), 1) if cs.get("totalToolDuration") else None}
    return {
        "session": a["group"]["session_id"], "project": a["group"]["project"], "start": iso(tl.get("lo")), "end": iso(tl.get("hi")),
        "span_s": round(span, 1), "autonomous_s": round(auto, 1), "human_s": round(tl["human"], 1), "idle_s": round(tl["idle"], 1),
        "seconds_by_category": by, "category_sum_check_s": sums, "parallel_factor": tl["parallel_factor"],
        "inside_subagent_wait_s": {k: round(v, 1) for k, v in sorted(a["inside_children"].items(), key=lambda kv: -kv[1])[:6]},
        "subagents": len(a["subs"]), "requests": quota["requests"], "units": quota["total_units"],
        "cache_hit_rate": quota["cache_hit_rate"], "models": {m: d["requests"] for m, d in quota["by_model"].items()},
        "reported_by_client": rep, "records": a["main"]["records"], "bad_lines": a["main"]["bad_lines"],
        "permission_modes": dict(a["main"]["perm_modes"]),
    }


def build_wall(analyses):
    total = defaultdict(float)
    span = auto = human = idle = 0.0
    inside = defaultdict(float)
    for a in analyses:
        for k, v in a["tl"]["by_cat"].items():
            total[k] += v
        span += a["tl"]["span"]
        auto += a["tl"]["autonomous"]
        human += a["tl"]["human"]
        idle += a["tl"]["idle"]
        for k, v in a["inside_children"].items():
            inside[k] += v
    roll = _cat_rollup(total)
    rows = []
    for k, v in sorted(total.items(), key=lambda kv: -kv[1]):
        base = k.split(":", 1)[0]
        excl = base in ("human_wait", "idle_gap")
        rows.append({"category": k, "seconds": round(v, 1), "pct_of_span": round(100 * v / span, 1) if span else 0,
                     "pct_of_autonomous": None if excl or not auto else round(100 * v / auto, 1), "excluded_from_autonomous": excl})
    return {"span_s": round(span, 1), "autonomous_s": round(auto, 1), "human_s": round(human, 1), "idle_s": round(idle, 1),
            "sum_check_pct": round(100 * sum(total.values()) / span, 2) if span else None,
            "categories": rows, "rollup_s": {k: round(v, 1) for k, v in sorted(roll.items(), key=lambda kv: -kv[1])},
            "inside_subagent_wait_s": {k: round(v, 1) for k, v in sorted(inside.items(), key=lambda kv: -kv[1])[:8]}}


def _advice(rs, key, fallback_title):
    a = rs.advice.get(key, {})
    return {"title": a.get("title", fallback_title), "why": a.get("why", ""), "fix": a.get("fix", ""), "before": a.get("before", ""), "after": a.get("after", ""),
            "ref": a.get("source", "")}


def _where(f):
    """'3 sessions (39 transcripts)': main sessions, as the view's items count them, plus every transcript
    (subagents included) when that differs."""
    n, t = f.get("sessions", 0), f.get("transcripts", f.get("sessions", 0))
    s = "%d session%s" % (n, "" if n == 1 else "s")
    return s + (" (%d transcripts)" % t if t != n else "")


def candidates(rs, wall, quota, env, stack, opts):
    """Build the ranked list from coded signals only (no inference)."""
    cands = []
    for f in stack:
        cands.append({"id": f["id"], "kind": "stack_rule", "title": f["title"], "evidence": "%d %s, %s measured, %s; e.g. %s" % (
            f["events"], f.get("unit", "runs"), fmt_dur(f["measured_seconds"]), _where(f),
            ((f["examples"][0].get("raw") or f["examples"][0]["cmd"]) if f["examples"] else "")),
            "saving_minutes": f["saving_seconds"] / 60.0, "saving_units": f["saving_tokens"], "confidence": f["confidence"],
            "arithmetic": f["arithmetic"], "why": f["why"], "fix": f["fix"], "before": f["before"], "after": f["after"],
            "ref": f.get("ref")})
    cm = quota["cache_misses"]
    waste = cm["waste_units"]
    if waste > 0:
        causes = ", ".join("%s x%d" % (k, v["count"]) for k, v in cm["by_cause"].items() if v["waste_units"] > 0)
        a = _advice(rs, "cache_miss", "Cache miss")
        cands.append({"id": "cache_miss", "kind": "quota", "title": a["title"], "evidence": "%d rebuilds (%s); longest idle before a rebuild %ss" % (cm["count"], causes, cm.get("max_gap_s", "?")),
                      "saving_minutes": 0.0, "saving_units": waste, "confidence": "high", "arithmetic": {"tokens": "sum(rebuilt tokens x (%.2f write - %.2f read) x tier cost)" % (rs.weights["cache_write_5m"], rs.weights["cache_read"])}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    o = quota["oversized_results"]
    if o["count"]:
        a = _advice(rs, "oversized_results", "Oversized outputs")
        sv = int((o["carry_units"] + o["total_tokens"] * rs.weights["cache_write_5m"]) * 0.8)
        cands.append({"id": "oversized_results", "kind": "quota", "title": a["title"], "evidence": "%d results >= %d chars, %s tokens total, carried %s units" % (o["count"], rs.oversized_chars, o["total_tokens"], o["carry_units"]),
                      "saving_minutes": 0.0, "saving_units": sv, "confidence": "medium", "arithmetic": {"tokens": "(carry %d + %d tok x %.2f write) x 0.8 removable" % (o["carry_units"], o["total_tokens"], rs.weights["cache_write_5m"]), "assumption": o["saving_assumption"]}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    r = quota["repeated_reads"]
    if r["repeat_reads"] >= 3:
        a = _advice(rs, "repeated_reads", "Repeated reads")
        cands.append({"id": "repeated_reads", "kind": "quota", "title": a["title"], "evidence": "%d repeat reads over %d files (%d tokens)" % (r["repeat_reads"], r["files"], r["wasted_tokens"]),
                      "saving_minutes": 0.0, "saving_units": int(r["wasted_tokens"] * rs.weights["cache_write_5m"]), "confidence": "medium", "arithmetic": {"tokens": "%d re-read tokens x %.2f write weight" % (r["wasted_tokens"], rs.weights["cache_write_5m"])}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    c = quota["repeated_commands"]
    if c["redundant_runs"] >= 3:
        a = _advice(rs, "repeated_commands", "Repeated commands")
        cands.append({"id": "repeated_commands", "kind": "quota", "title": a["title"], "evidence": "%d redundant reruns of %d commands (%s)" % (c["redundant_runs"], c["count"], c["top"][0]["cmd"] if c["top"] else ""),
                      "saving_minutes": c["seconds"] / 60.0, "saving_units": int(c["tokens"] * rs.weights["cache_write_5m"]), "confidence": "medium", "arithmetic": {"time": "sum of rerun durations with no edit in between = %ds" % c["seconds"], "tokens": "%d result tokens x %.2f" % (c["tokens"], rs.weights["cache_write_5m"])}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    l = quota["retry_loops"]
    if l["count"]:
        a = _advice(rs, "retry_loops", "Retry loops")
        cands.append({"id": "retry_loops", "kind": "quota", "title": a["title"], "evidence": "%d loops, %ds in retries (%s)" % (l["count"], l["seconds"], l["top"][0]["cmd"] if l["top"] else ""),
                      "saving_minutes": l["seconds"] / 60.0, "saving_units": int(l["tokens"] * rs.weights["cache_write_5m"]), "confidence": "medium", "arithmetic": {"time": "durations of attempts after the first = %ds" % l["seconds"]}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    p = quota["polling"]
    if p["polls"] >= 5:
        a = _advice(rs, "polling", "Polling")
        cands.append({"id": "polling", "kind": "quota", "title": a["title"], "evidence": "%d sleep/poll calls, %s asleep, %s context tokens re-read" % (p["polls"], fmt_dur(p["seconds"]), p["context_tokens_reprocessed"]),
                      "saving_minutes": 0.0, "saving_units": int(p["units"] * 0.8), "confidence": "medium", "arithmetic": {"tokens": "%d units of poll turns x 0.8 (a blocking wait still needs 1 turn)" % p["units"], "time": "waiting itself is not saved, only the per-poll overshoot and tokens"}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    m = quota["model_fit"]
    if m["mechanical_requests"] >= 10 and m["potential_saving_units"] > 0:
        a = _advice(rs, "model_fit", "Model fit")
        cands.append({"id": "model_fit", "kind": "quota", "title": a["title"], "evidence": "%d mechanical turns on top tier = %d units" % (m["mechanical_requests"], m["mechanical_units"]),
                      "saving_minutes": 0.0, "saving_units": m["potential_saving_units"], "confidence": "low", "arithmetic": {"tokens": "%d units x (1 - low/top cost ratio)" % m["mechanical_units"], "assumption": m["assumption"]}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    b = quota["batchable_turns"]
    if b["sequences"]:
        a = _advice(rs, "batchable_turns", "Batchable turns")
        cands.append({"id": "batchable_turns", "kind": "quota", "title": a["title"], "evidence": "%d sequences, %d turns" % (b["sequences"], b["turns"]),
                      "saving_minutes": b["saving_seconds"] / 60.0, "saving_units": b["saving_units"], "confidence": "medium", "arithmetic": {"tokens": b["assumption"]}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    t = quota["templated_loops"]
    if t["count"]:
        a = _advice(rs, "templated_loops", "Templated loops")
        cands.append({"id": "templated_loops", "kind": "quota", "title": a["title"], "evidence": "%d loops; e.g. %s" % (t["count"], t["top"][0]["template"] if t["top"] else ""),
                      "saving_minutes": t["saving_seconds"] / 60.0, "saving_units": t["saving_units"], "confidence": "medium", "arithmetic": {"tokens": t["assumption"]}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    fo = quota["subagent_fan_out"]
    if fo["small_spawns"] >= 3:
        a = _advice(rs, "subagent_fan_out", "Subagent fan-out")
        cands.append({"id": "subagent_fan_out", "kind": "quota", "title": a["title"], "evidence": "%d subagents, %d of them <= 3 requests; cold-start cost of the small ones %d units" % (fo["spawns"], fo["small_spawns"], fo["small_cold_start_units"]),
                      "saving_minutes": 0.0, "saving_units": int(fo["small_cold_start_units"] * 0.7), "confidence": "low", "arithmetic": {"tokens": "cold-start cache writes of tiny subagents x 0.7"}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    if env["compactions"]["count"] >= 2:
        a = _advice(rs, "compaction", "Compaction")
        cm_ = env["compactions"]
        cands.append({"id": "compaction", "kind": "env", "title": a["title"], "evidence": "%d compactions, %d tokens summarised, %ss in compaction" % (cm_["count"], cm_["pre_tokens_total"], cm_["duration_s"]),
                      "saving_minutes": cm_["duration_s"] * 0.5 / 60.0, "saving_units": int(cm_["pre_tokens_total"] * rs.weights["cache_write_5m"] * 0.5), "confidence": "low",
                      "arithmetic": {"tokens": "%d pre-compaction tokens x %.2f x 0.5 avoidable" % (cm_["pre_tokens_total"], rs.weights["cache_write_5m"])}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    for s in env["signals"]:
        if s["confidence"] == "low":
            continue  # reported in the environment section, never ranked
        key = "env_" + s["id"]
        a = rs.advice.get(key) or rs.advice.get("env_generic", {})
        cands.append({"id": key, "kind": "env", "title": s["label"], "evidence": "%d occurrence(s), up to %s in affected calls; %s" % (s["count"], fmt_dur(s["tool_seconds_upper_bound"]), (s["examples"][0] if s["examples"] else "")),
                      "saving_minutes": s["tool_seconds_upper_bound"] * 0.5 / 60.0, "saving_units": 0, "confidence": s["confidence"],
                      "arithmetic": {"time": "upper bound %s (time of the calls that hit it) x 0.5 assumed avoidable" % fmt_dur(s["tool_seconds_upper_bound"])},
                      "why": a.get("why", "Inferred from tool output."), "fix": a.get("fix") or s["hint"], "before": a.get("before", ""), "after": a.get("after", ""),
                      "ref": a.get("source", "")})
    api_s = sum(r["seconds"] for r in wall["categories"] if r["category"] == "api_wait")
    if wall["autonomous_s"] and api_s >= 120 and api_s / wall["autonomous_s"] > 0.05:
        a = _advice(rs, "api_wait", "API wait")
        cands.append({"id": "api_wait", "kind": "env", "title": a["title"], "evidence": "%s (%.0f%% of autonomous time) in requests slower than 3x their expected generation time" % (fmt_dur(api_s), 100 * api_s / wall["autonomous_s"]),
                      "saving_minutes": api_s * 0.3 / 60.0, "saving_units": 0, "confidence": "low",
                      "arithmetic": {"time": "%s x 0.3 assumed avoidable by lower concurrency / off-peak runs" % fmt_dur(api_s)}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    span = wall["span_s"]
    if span and wall["idle_s"] / span > 0.10:
        a = _advice(rs, "idle_gap", "Idle gaps")
        cands.append({"id": "idle_gap", "kind": "attention", "title": a["title"], "evidence": "%s idle (%.0f%% of span), separate from human waits" % (fmt_dur(wall["idle_s"]), 100 * wall["idle_s"] / span),
                      "saving_minutes": 0.0, "saving_units": 0, "confidence": "medium", "arithmetic": {"time": "not counted as autonomous time; cause unknown from transcript"}, **{k: a[k] for k in ("why", "fix", "before", "after", "ref")}})
    tm = opts.get("token_minute_equiv") or 0.0
    if not tm:
        auto_min = wall["autonomous_s"] / 60.0
        tm = max(1000.0, quota["total_units"] / auto_min) if auto_min > 0 else 20000.0
    total_units = max(1, quota["total_units"])
    for c in cands:
        c["score"] = (c["saving_minutes"] + c["saving_units"] / tm) * CONF_W.get(c["confidence"], 0.4)
        c["saving_minutes"] = round(c["saving_minutes"], 1)
        c["saving_pct_of_units"] = round(100.0 * c["saving_units"] / total_units, 1)
    cands.sort(key=lambda c: -c["score"])
    return cands


def run(args_ns, rs, root=None):
    """Main entry for analyze. args_ns: object with project, all, session, since, until, idle_threshold, top, machine_log."""
    opts = {"idle_threshold": getattr(args_ns, "idle_threshold", 180.0), "token_minute_equiv": getattr(args_ns, "token_minute_equiv", 0.0)}
    win = (getattr(args_ns, "since_ts", None), getattr(args_ns, "until_ts", None))
    groups = find_groups(args_ns.project, args_ns.all, args_ns.session, win[0], root)
    analyses = analyze_groups(groups, rs, win, opts)
    return build_report(analyses, rs, opts, args_ns)


def build_report(analyses, rs, opts, args_ns=None):
    repo.reset()  # project files are read once per report
    all_s = [s for a in analyses for s in a["sessions"]]
    per = []
    per_ids = []
    # heavy jobs overlapping within a session AND across sessions/subagents running on the same machine
    tid_sid = {}
    all_heavy = []
    for a in analyses:
        for iv in a["heavy_ivs"]:
            tid_sid[iv[4]] = a["group"]["session_id"]
        all_heavy += a["heavy_ivs"]
    overlaps_all = heavy_overlap(all_heavy)
    for o in overlaps_all:
        o["sid"] = tid_sid.get(o["tools"][0], "") if o.get("tools") else ""
        o["sids"] = sorted({tid_sid.get(t, "") for t in o.get("tools", [])})
    ov_by_file = {a["main"]["file"]: [o for o in overlaps_all if a["group"]["session_id"] in o["sids"]] for a in analyses}
    for a in analyses:
        mine = ov_by_file[a["main"]["file"]]
        q = analyze_quota(a["sessions"], rs)
        st = evaluate(a["sessions"], rs, mine, q["avg_fresh_tokens_per_request"], {S["session_id"]: a["group"]["session_id"] for S in a["sessions"]})
        ov_s = sum(o["seconds"] for o in mine)
        env = env_report(a["sessions"], rs, q, ov_s, None)
        summ = session_summary(a, rs, q, st, env)
        per.append(summ)
        ids = {f["id"] for f in st}
        ids |= {"env_" + s["id"] for s in env["signals"] if s["confidence"] != "low" or s["count"] >= 3}
        for k, cond in (("cache_miss", q["cache_misses"]["waste_units"] > 0), ("oversized_results", q["oversized_results"]["count"] > 0),
                        ("repeated_reads", q["repeated_reads"]["repeat_reads"] >= 3), ("repeated_commands", q["repeated_commands"]["redundant_runs"] >= 3),
                        ("retry_loops", q["retry_loops"]["count"] > 0), ("polling", q["polling"]["polls"] >= 5), ("templated_loops", q["templated_loops"]["count"] > 0),
                        ("batchable_turns", q["batchable_turns"]["sequences"] > 0)):
            if cond:
                ids.add(k)
        per_ids.append((summ["session"], ids))
    q_all = analyze_quota(all_s, rs)
    wall = build_wall(analyses)
    avg_tok = q_all["avg_fresh_tokens_per_request"]
    group_of = {S["session_id"]: a["group"]["session_id"] for a in analyses for S in a["sessions"]}
    skipped = {}
    st_all = evaluate(all_s, rs, overlaps_all, avg_tok, group_of, skipped)
    ov_total = sum(o["seconds"] for o in overlaps_all)
    cross = sum(1 for o in overlaps_all if len(o["sids"]) > 1)
    env = env_report(all_s, rs, q_all, ov_total, None)
    env["contention_hint"] = contention_hint(all_s, {a["main"]["file"]: overlaps_all for a in analyses})
    env["heavy_overlap"]["cross_session_overlaps"] = cross
    tele = build_telemetry(analyses, all_s, args_ns)
    ranked = candidates(rs, wall, q_all, env, st_all, opts)
    tc = telemetry_candidates(rs, tele, wall)
    if tc:
        for c in tc:
            c["score"] = c["saving_minutes"] * CONF_W.get(c["confidence"], 0.4)
            c["saving_minutes"] = round(c["saving_minutes"], 1)
            c["saving_pct_of_units"] = 0.0
        ranked = sorted(ranked + tc, key=lambda c: -c["score"])
    top = getattr(args_ns, "top", 8) if args_ns is not None else 8
    # recurring across runs
    cnt = Counter()
    for sid, ids in per_ids:
        cnt.update(ids)
    score = {c["id"]: c for c in ranked}
    recurring = []
    for k, n in cnt.most_common():
        if n >= 2 and k in score:
            recurring.append({"id": k, "title": score[k]["title"], "sessions": n, "of": len(per_ids), "total_saving_minutes": score[k]["saving_minutes"], "fix": score[k]["fix"]})
    recurring.sort(key=lambda r: (-r["sessions"], -r["total_saving_minutes"]))
    for i, c in enumerate(ranked, 1):
        c["rank"] = i
    sinks = [r for r in wall["categories"] if not r["excluded_from_autonomous"]][:6]
    report = {
        "meta": {"tool": "analyse-bottleneck", "version": __version__, "sessions": len(analyses), "rules_warnings": rs.warnings,
                 "project_checks": sorted(skipped.values(), key=lambda k: -k["seconds"]),
                 "weights_note": "units = weighted input-token equivalents (see rules/models.json); a comparison device, not money",
                 "honesty": ["Transcripts hold no CPU/memory telemetry: machine signals are inferred unless --sysstat or --machine-log supplies measurements (see telemetry.coverage).",
                             "Permission waits are inferred from unusually slow instant tools; human waits come from gaps before human prompts.",
                             "Savings are estimates with stated arithmetic and assumptions, not measurements."]},
        "wall_clock": wall, "time_sinks": sinks, "top_commands_by_class": top_commands(all_s),
        "environment": env, "quota": q_all, "stack_findings": st_all, "ranked": ranked[:top], "ranked_total": len(ranked),
        "recurring": recurring[:top], "sessions": per,
    }
    ml = getattr(args_ns, "machine_log", None) if args_ns is not None else None
    if ml:
        from .machine import correlate
        report["machine"] = correlate(ml, analyses)
    report["telemetry"] = tele
    tele["coverage"] = telemetry.coverage(report)
    return report


def build_telemetry(analyses, all_s, args_ns):
    win = (getattr(args_ns, "since_ts", None), getattr(args_ns, "until_ts", None)) if args_ns is not None else (None, None)
    lo = win[0] if win[0] is not None else min((S["first"] for S in all_s if S["first"]), default=None)
    hi = win[1] if win[1] is not None else max((S["last"] for S in all_s if S["last"]), default=None)
    tele = {"latency": telemetry.latency(all_s), "hooks": telemetry.hooks(all_s), "measures": telemetry.measures(all_s)}
    if getattr(args_ns, "host_snapshot", True):
        tele["host"] = telemetry.host_snapshot()
    sd = getattr(args_ns, "sysstat", None)
    if sd:
        tele["sysstat"] = telemetry.sysstat(sd, lo, hi, analyses)
    jd = getattr(args_ns, "jobs", None)
    if jd:
        cwds = sorted({S["cwd"] for S in all_s if S.get("cwd")})
        pdirs = sorted({a["group"]["project"] for a in analyses})
        tele["jobs"] = telemetry.jobs(jd, lo, hi, cwds, pdirs)
    dj = getattr(args_ns, "dora_json", None)
    if dj:
        try:
            with open(dj, encoding="utf-8") as f:
                tele["dora"] = json.load(f)
        except (OSError, ValueError) as e:
            tele["dora"] = None
            tele["dora_error"] = str(e)
    return tele


def telemetry_candidates(rs, tele, wall):
    out = []

    def add(cid, title, evidence, minutes, conf, arith, fallback):
        a = rs.advice.get(cid) or fallback
        out.append({"id": cid, "kind": "telemetry", "title": a.get("title", title), "evidence": evidence, "saving_minutes": minutes, "saving_units": 0,
                    "confidence": conf, "arithmetic": {"time": arith}, "why": a.get("why", ""), "fix": a.get("fix", ""),
                    "before": a.get("before", ""), "after": a.get("after", ""), "measured": True})
    lat = tele.get("latency") or {}
    ce = lat.get("context_effect") or {}
    sm, lg = ce.get("ctx_lt_60k_first_block_p50"), ce.get("ctx_ge_150k_first_block_p50")
    if sm and lg and ce.get("n_large", 0) >= 20 and lg > 1.5 * sm:
        extra = (lg - sm) * ce["n_large"]
        add("large_context_latency", "Large contexts answer slower", "first block p50 %.1fs at >=150k context tokens vs %.1fs below 60k (%d large requests, measured)" % (lg, sm, ce["n_large"]),
            extra * 0.3 / 60.0, "medium", "(p50 large - p50 small) x %d large requests x 0.3 assumed avoidable by keeping contexts smaller" % ce["n_large"],
            {"why": "Each request with a huge context takes longer before the first block arrives.", "fix": "Hand noisy work to subagents, start task-sized sessions, compact before the context passes ~150k."})
    sl = lat.get("slow_ge_60s") or {}
    if sl.get("count", 0) >= 5 and sl.get("share_of_request_time", 0) >= 0.1:
        add("slow_api_requests", "Slow API requests (>= 60 s to first block)", "%d requests, %s, %.0f%% of all request time (measured)" % (sl["count"], fmt_dur(sl["seconds"]), 100 * sl["share_of_request_time"]),
            0.0, "medium", "not avoidable from the client side; shows how much of inference time is long thinking or provider latency",
            {"why": "Long first-block times are long thinking blocks or provider queueing.", "fix": "Lower effort for mechanical subagents; check whether slow requests cluster at certain hours."})
    hk = tele.get("hooks") or {}
    if hk.get("seconds_total", 0) >= 60:
        add("hook_overhead", "Stop hook overhead", "%d stop-hook runs, %s total, p90 %s ms per stop (measured)" % (hk["stop_hook_runs"], fmt_dur(hk["seconds_total"]), hk["ms_per_stop"]["p90"]),
            hk["seconds_total"] * 0.8 / 60.0, "high", "hook time x 0.8 if heavy hooks run detached",
            {"why": "Synchronous hooks add their full runtime to every stop.", "fix": "Run heavy hook work detached (fork and return) and rate-limit it."})
    jb = tele.get("jobs") or {}
    qw = (jb.get("queue_wait_s") or {}).get("sum", 0)
    if qw >= 60:
        add("queue_wait", "Jobs waiting in a queue / admission gate", "%d of %d jobs waited, %s in total, p90 %ss (measured from job records)" % (jb["waited_jobs"], jb["jobs"], fmt_dur(qw), jb["queue_wait_s"]["p90"]),
            qw * 0.5 / 60.0, "medium", "queue wait x 0.5 assumed avoidable by scheduling heavy jobs apart or raising capacity",
            {"why": "Jobs sat waiting for memory or a lock instead of running.", "fix": "Stagger heavy jobs, reduce per-job memory (fewer workers), or add capacity."})
    ss = tele.get("sysstat") or {}
    sat = ss.get("saturated_during_autonomous_s") or {}
    for k, label in (("cpu_pct", "CPU saturated"), ("mem_used_pct", "Memory nearly full"), ("iowait_pct", "Disk I/O wait high"), ("steal_pct", "CPU steal (noisy neighbour)")):
        if sat.get(k, 0) >= 600:
            add("host_" + k, label + " during autonomous work", "%s of autonomous time in %ss sysstat samples with %s >= %.0f (measured, host-wide)" % (fmt_dur(sat[k]), int(ss.get("interval_s", 600)), k, telemetry.SAT[k]),
                0.0, "medium", "host-wide 10-min resolution: shows exposure, not a saving",
                {"why": "The host itself was the limit for part of the run.", "fix": "Serialize heavy jobs, cap workers, or move heavy work off the shared host."})
    return out
