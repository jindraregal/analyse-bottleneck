"""Short markdown rendering of a report dict."""
from .util import fmt_dur, fmt_int


def _row(*cells):
    return "| " + " | ".join(str(c) for c in cells) + " |"


def render(rep, top=8, with_commands=True):
    w = rep["wall_clock"]
    q = rep["quota"]
    L = []
    L.append("# Run bottleneck report")
    L.append("")
    L.append("Scope: %d session(s), span %s = autonomous %s + human/permission wait %s + idle gaps %s (the last two are excluded from the shares below)." % (
        rep["meta"]["sessions"], fmt_dur(w["span_s"]), fmt_dur(w["autonomous_s"]), fmt_dur(w["human_s"]), fmt_dur(w["idle_s"])))
    L.append("Quota: %d requests, %s units, cache hit rate %s." % (q["requests"], fmt_int(q["total_units"]), ("%.0f%%" % (100 * q["cache_hit_rate"])) if q["cache_hit_rate"] is not None else "n/a"))
    L.append("")
    L.append("## Where the time went")
    L.append(_row("category", "time", "% of autonomous", "% of span"))
    L.append(_row("---", "---", "---", "---"))
    for r in w["categories"][:12]:
        L.append(_row(r["category"], fmt_dur(r["seconds"]), "-" if r["pct_of_autonomous"] is None else "%.1f" % r["pct_of_autonomous"], "%.1f" % r["pct_of_span"]))
    if w["inside_subagent_wait_s"]:
        L.append("")
        L.append("Inside subagent/background waits: " + ", ".join("%s %s" % (k, fmt_dur(v)) for k, v in list(w["inside_subagent_wait_s"].items())[:5]))
    if with_commands:
        tc = rep["top_commands_by_class"]
        heavy = [(c, v[0]) for c, v in tc.items() if c in ("build", "test", "install", "media_gpu", "network", "lint_typecheck") and v and v[0]["seconds"] >= 5]
        if heavy:
            L.append("")
            L.append("Slowest commands by class: " + "; ".join("%s: `%s` %s x%d" % (c, v["cmd"][:60], fmt_dur(v["seconds"]), v["runs"]) for c, v in heavy[:5]))
    L.append("")
    L.append("## Ranked bottlenecks (estimated saving, with confidence)")
    if not rep["ranked"]:
        L.append("Nothing crossed an evidence threshold.")
    for c in rep["ranked"][:top]:
        L.append("")
        L.append("%d. **%s** — ~%.1f min, ~%s units (%.1f%% of quota), confidence %s" % (c["rank"], c["title"], c["saving_minutes"], fmt_int(c["saving_units"]), c.get("saving_pct_of_units", 0), c["confidence"]))
        L.append("   - Evidence: %s" % c["evidence"])
        ar = c.get("arithmetic") or {}
        txt = "; ".join("%s: %s" % (k, v) for k, v in ar.items() if v and v != "n/a")
        if txt:
            L.append("   - Arithmetic: %s" % txt)
        if c.get("before"):
            L.append("   - Before: `%s`  After: `%s`" % (c["before"], c["after"]))
        if c.get("fix"):
            L.append("   - Fix: %s" % c["fix"])
    env = rep["environment"]
    L.append("")
    L.append("## Environment signals (inferred from tool output)")
    if env["signals"]:
        for s in env["signals"][:8]:
            L.append("- %s x%d, confidence %s, up to %s of tool time" % (s["label"], s["count"], s["confidence"], fmt_dur(s["tool_seconds_upper_bound"])))
    else:
        L.append("- none detected")
    L.append("- compactions %d, tool timeouts (auto-backgrounded) %d, API errors %d, overlapping heavy jobs %s" % (env["compactions"]["count"], env["tool_timeouts"], env["api_errors"], fmt_dur(env["heavy_overlap"]["seconds"])))
    lf = env.get("long_foreground_calls") or {}
    if lf.get("count"):
        L.append("- %d foreground call(s) over 30 min, %s in total: probably a suspended/stalled session, not real work (low confidence)" % (lf["count"], fmt_dur(lf["seconds"])))
    for h in env.get("contention_hint", []):
        L.append("- contention hint: `%s` median %ss, %d slow run(s) overlapped other heavy jobs (max %ss)" % (h["cmd"], h["median_s"], h["slow_runs_overlapping_other_heavy_jobs"], h["max_s"]))
    if rep.get("machine"):
        m = rep["machine"]
        L.append("- machine log: %s samples; %s" % (m.get("samples"), "; ".join(m.get("corroboration", [])) or "no corroborating findings"))
    t = rep.get("telemetry") or {}
    if t:
        L.append("")
        L.append("## Measured telemetry")
        lat = t.get("latency") or {}
        if lat.get("matched"):
            fb, ce, cs, sl = lat["first_block_s"], lat["context_effect"], lat["cold_start"], lat["slow_ge_60s"]
            L.append("- API: %d requests, %s in requests; first block p50 %ss / p90 %ss / max %ss; >=60s: %d (%.0f%% of request time)" % (
                lat["matched"], fmt_dur(lat["total_request_s"]), fb["p50"], fb["p90"], fb["max"], sl["count"], 100 * sl["share_of_request_time"]))
            for mname, d in list(lat["by_model"].items())[:3]:
                L.append("  - %s: %d req, first block p50 %ss, total p50 %ss, %s out tok/s, context p50 %s tok" % (
                    mname, d["requests"], d["first_block_s"]["p50"], d["total_s"]["p50"], d["output_tokens_per_s_p50"], fmt_int(d["context_tokens_p50"] or 0)))
            if ce["n_large"] and ce["n_small"]:
                L.append("  - context effect: first block p50 %ss below 60k tokens (n=%d) vs %ss at >=150k (n=%d)" % (ce["ctx_lt_60k_first_block_p50"], ce["n_small"], ce["ctx_ge_150k_first_block_p50"], ce["n_large"]))
            L.append("  - cold start: first request of a session p50 %ss (n=%d) vs others %ss; after a >=20k cache write p50 %ss (n=%d)" % (
                cs["first_request_of_session_p50"], cs["n_first"], cs["other_requests_p50"], cs["cache_write_ge_20k_p50"], cs["n_cache_write_ge_20k"]))
        else:
            L.append("- API latency: %s" % lat.get("basis", "n/a"))
        hk = t.get("hooks") or {}
        if hk.get("stop_hook_runs"):
            L.append("- Stop hooks: %d runs, %ss total, p90 %s ms per stop, %d errors" % (hk["stop_hook_runs"], hk["seconds_total"], hk["ms_per_stop"]["p90"], hk["errors"]))
        for m in (t.get("measures") or {}).get("items", [])[:6]:
            L.append("- %s (%s, tool-printed): n=%d, p50 %ss, p90 %ss, total %s" % (m["label"], m["kind"], m["n"], m["p50"], m["p90"], fmt_dur(m["sum"])))
        ss = t.get("sysstat")
        if ss and ss.get("samples"):
            h = ss["host"]
            L.append("- Host (sysstat, %d samples / %ss): CPU p50 %s%% p90 %s%% max %s%%; iowait p90 %s%%; mem used p90 %s%% max %s%%; disk util p90 %s%%" % (
                ss["samples"], int(ss["interval_s"]), h["cpu_pct"]["p50"], h["cpu_pct"]["p90"], h["cpu_pct"]["max"], (h.get("iowait_pct") or {}).get("p90"),
                (h.get("mem_used_pct") or {}).get("p90"), (h.get("mem_used_pct") or {}).get("max"), (h.get("disk_util_pct") or {}).get("p90")))
            d = ss.get("during_autonomous_avg") or {}
            if d:
                L.append("  - during this scope's autonomous time (weighted): CPU %s%%, iowait %s%%, mem used %s%%, disk util %s%%" % (d.get("cpu_pct"), d.get("iowait_pct"), d.get("mem_used_pct"), d.get("disk_util_pct")))
            for k, v in (ss.get("by_heavy_class") or {}).items():
                L.append("  - while %s ran (%s): CPU %s%%, iowait %s%%, mem %s%%" % (k, fmt_dur(v["seconds"]), v.get("cpu_pct"), v.get("iowait_pct"), v.get("mem_used_pct")))
            for n in ss.get("findings", []):
                L.append("  - " + n)
        elif ss:
            L.append("- Host: %s" % ss.get("basis"))
        jb = t.get("jobs")
        if jb and jb.get("jobs"):
            L.append("- Jobs: %d, run p50 %ss / p90 %ss, queue wait total %s (p90 %ss, %d waited), peak mem p90 %s GiB, killed %d" % (
                jb["jobs"], jb["run_s"]["p50"], jb["run_s"]["p90"], fmt_dur(jb["queue_wait_s"]["sum"]), jb["queue_wait_s"]["p90"], jb["waited_jobs"], jb["peak_gib"]["p90"], jb["killed"]))
        if jb and jb.get("gap"):
            L.append("  - " + jb["gap"])
        dr = t.get("dora")
        if dr:
            m = dr.get("metrics", {})
            parts = []
            for k, label in (("deployment_frequency_per_day", "deploys/day"), ("change_failure_rate", "change failure rate")):
                if k in m:
                    parts.append("%s %s [%s]" % (label, m[k]["value"], m[k]["kind"]))
            lt, tr = m.get("lead_time_for_changes_h", {}), m.get("time_to_restore_h", {})
            parts.append("lead time p50 %s h [%s]" % (lt.get("p50"), lt.get("kind")))
            parts.append("restore p50 %s h [%s]" % (tr.get("p50"), tr.get("kind")))
            L.append("- Delivery (DORA-style): " + "; ".join(parts))
            if dr.get("ci"):
                ci = dr["ci"]
                L.append("  - CI: %d runs, %d failed, p50 %s min, %s h of CI time" % (ci["runs"], ci["failed"], ci["duration_min"]["p50"], ci["duration_min"]["sum_h"]))
        if t.get("host"):
            L.append("- Host snapshot now: %s GB free (%s%% used), %s CPUs, %s" % (t["host"].get("disk_free_gb"), t["host"].get("disk_used_pct"), t["host"].get("cpus"), t["host"].get("gpu")))
        cov = t.get("coverage") or []
        if cov:
            L.append("")
            L.append("## Coverage: measured vs inferred vs missing")
            L.append(_row("dimension", "status", "source", "note"))
            L.append(_row("---", "---", "---", "---"))
            for c in cov:
                L.append(_row(c["dimension"], c["status"], c["source"], c["note"]))
    if rep["recurring"]:
        L.append("")
        L.append("## Recurring across runs")
        for r in rep["recurring"]:
            L.append("- %s: in %d of %d sessions" % (r["title"], r["sessions"], r["of"]))
    L.append("")
    L.append("_Units are weighted token equivalents (rules/models.json), not money. Savings are estimates; see arithmetic lines. Quota items overlap (the same turns can be polling, mechanical and batchable): do not add them up._")
    return "\n".join(L) + "\n"
