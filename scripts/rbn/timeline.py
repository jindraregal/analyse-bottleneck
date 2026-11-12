"""Wall-clock attribution by interval union.

Every second between the first and last record of a session is assigned to exactly one
category (sweep line with a priority order), so the categories sum to 100% of the span even
when tools and subagents run in parallel. Human/permission waits and idle gaps are reported
separately and excluded from the autonomous time.
"""
import bisect
import heapq

# lower rank wins when intervals overlap
ORDER = ["human_wait", "media_gpu", "build", "test", "install", "lint_typecheck", "network", "mcp",
         "browser_automation", "web", "container", "git", "script_run", "service", "shell_other", "poll_wait",
         "file_ops", "other_tool", "harness", "subagent_wait", "thinking", "inference", "api_wait",
         "background_wait", "overhead", "idle_gap"]
RANK = {n: i for i, n in enumerate(ORDER)}
HEAVY = ("build", "test", "media_gpu", "install")
INSTANT_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS", "NotebookEdit", "ToolSearch", "Skill")
API_BASE_S, API_TOKENS_PER_S, API_MIN_STALL_S, API_STALL_FACTOR = 20.0, 25.0, 120.0, 3.0
BYPASS = ("bypassPermissions", "acceptEdits", "dontAsk")


def rank_of(cat):
    base = cat.split(":", 1)[0]
    r = RANK.get(base, RANK["file_ops"])
    return r


def build_intervals(S, rs, child_spans=None, perm_threshold=45.0, bg_cap=1800.0, fill=None):
    """-> list of (start, end, category, rank, tool_id_or_None[, payload]).

    Request intervals carry a 6th element ("req", request_id) so a payload sweep can name the model.
    fill: (start, end) stretches when a subagent of this session was working (child_activity); they count as
    waiting on subagents, so child work after the parent stopped waiting (a resumed agent, an agent that
    notified early) is not lost in the parent's idle or prompt gaps."""
    out = [(s, e, "subagent_wait", RANK["background_wait"], None) for s, e in (fill or ())]
    child_spans = child_spans or {}
    pm_total = sum(S["perm_modes"].values())
    # permission waits are only inferred when the transcript shows an interactive (non-bypass) mode
    session_bypass = S["is_sub"] or pm_total == 0 or sum(S["perm_modes"].get(m, 0) for m in BYPASS) > 0.5 * pm_total
    human_sorted = sorted(S["human_ts"])
    for r in S["requests"].values():
        s, e = r["start"], max(r["end"], r["start"])
        if e <= s:
            continue
        think = min(r["think_ms"] / 1000.0, e - s)
        rp = ("req", r["id"])
        if think > 0:
            out.append((s, s + think, "thinking", RANK["thinking"], None, rp))
        gen = e - s - think
        if r.get("api_error"):
            out.append((s + think, e, "api_wait", RANK["api_wait"], None, rp))
        elif gen > 0:
            # generation should take roughly output_tokens / rate; a large excess is API-side waiting
            # (retries, overload, rate-limit backoff, stalled stream), reported separately.
            expected = API_BASE_S + r["usage"].get("output_tokens", 0) / API_TOKENS_PER_S
            excess = gen - expected if gen > max(API_MIN_STALL_S, API_STALL_FACTOR * expected) else 0.0
            if excess > 0:
                out.append((s + think, s + think + excess, "api_wait", RANK["api_wait"], None, rp))
            out.append((s + think + excess, e, "inference", RANK["inference"], None, rp))
    for tid in S["tool_order"]:
        t = S["tools"][tid]
        t0, t1 = t["t0"], t["t1"]
        if t1 is None or t1 < t0:
            continue
        cat = t["cls"]
        dur = t1 - t0
        instant = t["name"] in INSTANT_TOOLS or (t["name"] == "Bash" and t.get("ro"))
        if instant and dur > perm_threshold and not session_bypass and t.get("pm") not in BYPASS:
            cat = "human_wait:permission"
        if cat.startswith("human_wait") and cat not in ("human_wait:permission", "human_wait:approval"):
            cat = "human_wait:ask"
        out.append((t0, t1, cat, rank_of(cat), tid))
        if t.get("bg") and t["name"] == "Bash" and t["cls"] != "service":
            end = t["bg_end"] if t["bg_end"] else min(S["last"] or t1, t1 + bg_cap)
            if end > t1:
                if t["cls"].startswith("human_wait"):
                    out.append((t1, end, t["cls"], rank_of(t["cls"]), tid))
                else:
                    out.append((t1, end, "background_wait:" + t["cls"], RANK["background_wait"], tid))
        if t["cls"] == "subagent_wait" and t.get("is_async"):
            span = child_spans.get(t.get("agent_id"))
            end = t["bg_end"] or (span[1] if span else None)
            if end and end > t1:
                out.append((t1, end, "subagent_wait", RANK["background_wait"], tid))
    return out, human_sorted


def sweep(intervals, lo, hi, payload=False):
    """Priority sweep. intervals: (s,e,cat,rank,...). -> list of (s,e,cat or None) partitioning [lo,hi].

    Lower rank wins; among intervals of equal rank the earliest start wins, then input order (deterministic,
    and independent of category names). payload=True also says WHICH interval won each slice:
    -> (s,e,cat or None,payload or None), where payload is iv[5] if present else iv[4] (the tool id); the
    category of every instant is the same as without payload."""
    out = _sweep_payload(intervals, lo, hi)
    if payload:
        return out
    merged = []
    for s, e, c, _ in out:
        if merged and merged[-1][2] == c and abs(merged[-1][1] - s) < 1e-9:
            merged[-1] = (merged[-1][0], e, c)
        else:
            merged.append((s, e, c))
    return merged


def _sweep_payload(intervals, lo, hi):
    ev = []
    pay = []
    for i, iv in enumerate(intervals):
        s, e = max(iv[0], lo), min(iv[1], hi)
        pay.append(iv[5] if len(iv) > 5 else (iv[4] if len(iv) > 4 else None))
        if e > s:
            key = (iv[3], iv[0], i, iv[2])  # i is unique: the category name never decides a tie
            ev.append((s, 1, key))
            ev.append((e, 0, key))
    ev.sort()
    heap, dead = [], set()
    slices = []
    prev = lo
    for t, kind, key in ev:
        if t > prev:
            while heap and heap[0] in dead:
                dead.discard(heapq.heappop(heap))
            if heap:
                b = heap[0]
                slices.append((prev, t, b[3], pay[b[2]]))
            else:
                slices.append((prev, t, None, None))
            prev = t
        if kind == 1:
            heapq.heappush(heap, key)
        else:
            dead.add(key)
    if hi > prev:
        slices.append((prev, hi, None, None))
    merged = []
    for s, e, c, p in slices:
        if merged and merged[-1][2] == c and merged[-1][3] == p and abs(merged[-1][1] - s) < 1e-9:
            merged[-1] = (merged[-1][0], e, c, p)
        else:
            merged.append((s, e, c, p))
    return merged


def classify_gaps(slices, human_sorted, idle_threshold, away_sorted=()):
    """Name the slices nobody owned. Extra tuple fields (a payload) are kept as they are."""
    out = []
    for sl in slices:
        s, e, c = sl[0], sl[1], sl[2]
        if c is not None:
            out.append(sl)
            continue
        rest = tuple(sl[3:])
        i = bisect.bisect_left(human_sorted, s - 0.5)
        if i < len(human_sorted) and human_sorted[i] <= e + 3.0:
            out.append((s, e, "human_wait:prompt") + rest)
        elif away_sorted and _near(away_sorted, s, e):
            out.append((s, e, "human_wait:away") + rest)
        elif e - s > idle_threshold:
            out.append((s, e, "idle_gap") + rest)
        else:
            out.append((s, e, "overhead") + rest)
    return out


def _near(sorted_ts, s, e):
    i = bisect.bisect_left(sorted_ts, s - 0.5)
    return i < len(sorted_ts) and sorted_ts[i] <= e + 3.0


def heavy_overlap(intervals, min_overlap=20.0):
    ev = []
    for iv in intervals:
        if iv[2] in HEAVY and iv[4] is not None:
            ev.append((iv[0], 1, iv[2], iv[4]))
            ev.append((iv[1], 0, iv[2], iv[4]))
    ev.sort()
    active = {}
    res = []
    start = None
    for t, kind, cat, tid in ev:
        if kind == 1:
            active[tid] = cat
            if len(active) == 2:
                start = t
        else:
            if len(active) >= 2 and start is not None and len(active) == 2:
                if t - start >= min_overlap:
                    res.append({"start": start, "seconds": round(t - start, 1), "classes": sorted(set(active.values())), "tools": sorted(active)})
            active.pop(tid, None)
            if len(active) < 2:
                start = None
    return res


def child_activity(subs, rs, child_spans=None, window=(None, None)):
    """-> merged (start, end) stretches when any child transcript was doing something: a tool call, a model
    request, or waiting on a background command that reported its end. A child's own wait on a subagent is
    not activity (its child is in the list too); approvals and gaps are not either."""
    ivs = []
    for S in subs:
        lo, hi = S["first"], S["last"]
        if lo is None or hi is None:
            continue
        if window[0] is not None:
            lo = max(lo, window[0])
        if window[1] is not None:
            hi = min(hi, window[1])
        for iv in build_intervals(S, rs, child_spans)[0]:
            cat = iv[2]
            if cat == "subagent_wait" or cat.startswith("human_wait"):
                continue
            if cat.startswith("background_wait") and not (S["tools"].get(iv[4]) or {}).get("bg_end"):
                continue  # no end notice: how long it really ran is unknown
            s, e = max(lo, iv[0]), min(hi, iv[1])
            if e > s:
                ivs.append((s, e))
    ivs.sort()
    out = []
    for s, e in ivs:
        if out and s <= out[-1][1] + 1e-9:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def analyze_timeline(S, rs, idle_threshold=180.0, child_spans=None, window=(None, None), fill=None):
    """fill (child_activity, already clipped to the window) also widens the span: a subagent still working
    after the session's last record (or before its first one in the window) is agent time of this session."""
    lo = max(S["first"], window[0]) if (S["first"] is not None and window[0] is not None) else S["first"]
    hi = min(S["last"], window[1]) if (S["last"] is not None and window[1] is not None) else S["last"]
    if fill and lo is not None and hi is not None:
        f_lo, f_hi = min(s for s, _ in fill), max(e for _, e in fill)
        if window[0] is not None:
            f_lo = max(f_lo, window[0])
        if window[1] is not None:
            f_hi = min(f_hi, window[1])
        lo, hi = min(lo, f_lo), max(hi, f_hi)
    if lo is None or hi is None or hi <= lo:
        return {"span": 0.0, "by_cat": {}, "slices": [], "autonomous": 0.0, "human": 0.0, "idle": 0.0, "heavy_overlap": [], "parallel_factor": 1.0}
    ivs, human_sorted = build_intervals(S, rs, child_spans, fill=fill)
    slices = classify_gaps(sweep(ivs, lo, hi), human_sorted, idle_threshold, sorted(S["away_ts"]))
    by = {}
    for s, e, c in slices:
        by[c] = by.get(c, 0.0) + (e - s)
    span = hi - lo
    human = sum(v for k, v in by.items() if k.startswith("human_wait"))
    idle = by.get("idle_gap", 0.0)
    tool_ivs = [iv for iv in ivs if iv[4] is not None and iv[2] not in ("background_wait",) and not iv[2].startswith("background_wait") and iv[3] < RANK["thinking"] and not iv[2].startswith("human_wait")]
    raw = sum(iv[1] - iv[0] for iv in tool_ivs)
    uni = sum(e - s for s, e, c in sweep([(a, b, "x", 0) for a, b, *_ in tool_ivs], lo, hi) if c)
    return {"span": span, "lo": lo, "hi": hi, "by_cat": by, "slices": slices, "human": human, "idle": idle,
            "autonomous": span - human - idle, "heavy_overlap": heavy_overlap(ivs),
            "parallel_factor": round(raw / uni, 2) if uni > 0 else 1.0}


def child_breakdown(parent_slices, child_slices, parent_cats=("subagent_wait",)):
    """Seconds of each child category inside parent slices attributed to waiting on children."""
    cs = [(s, e, c) for s, e, c in child_slices if c]
    starts = [s for s, _, _ in cs]
    res = {}
    for ps, pe, pc in parent_slices:
        if pc is None or not (pc in parent_cats or pc.startswith("background_wait")):
            continue
        i = max(0, bisect.bisect_right(starts, ps) - 1)
        while i < len(cs) and cs[i][0] < pe:
            s, e, c = cs[i]
            ov = min(pe, e) - max(ps, s)
            if ov > 0:
                res[c] = res.get(c, 0.0) + ov
            i += 1
    return res


def union_children(child_slice_lists, lo, hi):
    ivs = []
    for sl in child_slice_lists:
        for s, e, c in sl:
            if c:
                ivs.append((s, e, c, rank_of(c) if c not in ("overhead", "idle_gap") else 99))
    return [(s, e, c) for s, e, c in sweep(ivs, lo, hi) if c]
