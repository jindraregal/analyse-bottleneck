"""Measured telemetry next to the transcript inference.

Everything here is either read from records the harness wrote (API send times, hook durations),
printed by a tool about itself (rules/measures.json), or read from an optional external source
(sysstat archives, job records of a wrapper). Each block says where its numbers come from, and
`coverage()` states per dimension whether the report holds measured data, inference, or nothing.
"""
import bisect
import glob
import json
import os
import shutil
import subprocess
import time
from collections import defaultdict

from .util import fmt_dur

MAX_SEND_TO_FIRST_S = 900.0  # a send further away than this from the reply is not the reply's send
EXCLUDED = ("human_wait", "idle_gap")


def _q(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = min(len(v) - 1, max(0, int(round(p / 100.0 * (len(v) - 1)))))
    return round(v[k], 2)


def _dist(vals):
    return {"n": len(vals), "p50": _q(vals, 50), "p90": _q(vals, 90), "p99": _q(vals, 99), "max": round(max(vals), 2) if vals else None,
            "sum": round(sum(vals), 1)}


# --- API latency -----------------------------------------------------------------------------

def latency(sessions):
    """Per request: send time (api-request record) -> first content block -> last content block.

    'first_block_s' is NOT time-to-first-token: transcripts write a record when a whole content
    block (often a thinking block) is complete. It is an upper bound on TTFT.
    """
    rows = []
    unmatched = 0
    for S in sessions:
        sends = sorted(t for t, _ in S.get("api_sends", []))
        reqs = [r for r in S.get("req_list", []) if r.get("model") and r["model"] != "<synthetic>" and not r.get("api_error")]
        first_req = True
        for r in reqs:
            i = bisect.bisect_right(sends, r["first"] + 0.001) - 1
            if i < 0 or r["first"] - sends[i] > MAX_SEND_TO_FIRST_S or sends[i] < r["start"] - 2.0:
                unmatched += 1
                first_req = False
                continue
            send = sends[i]
            u = r.get("usage", {})
            ctx = int(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))
            out = int(u.get("output_tokens", 0))
            gen = max(0.0, r["end"] - r["first"])
            rows.append({"model": r["model"], "first": r["first"] - send, "total": r["end"] - send, "ctx": ctx, "out": out,
                         "tps": (out / gen) if gen >= 1.0 and out >= 50 else None, "cold": first_req, "sub": S.get("is_sub", False),
                         "cache_create": int(u.get("cache_creation_input_tokens", 0))})
            first_req = False
    if not rows:
        return {"matched": 0, "unmatched": unmatched, "basis": "no api-request send records in these transcripts (older Claude Code versions do not write them)"}
    by = defaultdict(list)
    for x in rows:
        by[x["model"]].append(x)
    by_model = {}
    for m, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        by_model[m] = {"requests": len(xs), "first_block_s": _dist([x["first"] for x in xs]), "total_s": _dist([x["total"] for x in xs]),
                       "output_tokens": _dist([x["out"] for x in xs]), "output_tokens_per_s_p50": _q([x["tps"] for x in xs if x["tps"]], 50),
                       "context_tokens_p50": _q([x["ctx"] for x in xs], 50), "context_tokens_p90": _q([x["ctx"] for x in xs], 90)}
    # context size vs latency: the measured cost of a big context
    small = [x["first"] for x in rows if x["ctx"] < 60000]
    large = [x["first"] for x in rows if x["ctx"] >= 150000]
    cold = [x["first"] for x in rows if x["cold"]]
    warm = [x["first"] for x in rows if not x["cold"]]
    rebuild = [x["first"] for x in rows if x["cache_create"] >= 20000]
    slow = [x for x in rows if x["first"] >= 60]
    total = sum(x["total"] for x in rows)
    return {
        "matched": len(rows), "unmatched": unmatched,
        "basis": "send time from 'api-request' records, reply from assistant records of the same request; first_block_s is an upper bound on time-to-first-token",
        "total_request_s": round(total, 1),
        "first_block_s": _dist([x["first"] for x in rows]),
        "by_model": by_model,
        "context_effect": {"ctx_lt_60k_first_block_p50": _q(small, 50), "ctx_ge_150k_first_block_p50": _q(large, 50), "n_small": len(small), "n_large": len(large)},
        "cold_start": {"first_request_of_session_p50": _q(cold, 50), "other_requests_p50": _q(warm, 50), "n_first": len(cold),
                       "cache_write_ge_20k_p50": _q(rebuild, 50), "n_cache_write_ge_20k": len(rebuild)},
        "slow_ge_60s": {"count": len(slow), "seconds": round(sum(x["total"] for x in slow), 1), "share_of_request_time": round(sum(x["total"] for x in slow) / total, 3) if total else 0.0},
    }


# --- hooks -----------------------------------------------------------------------------------

def hooks(sessions):
    rows = [h for S in sessions for h in S.get("hooks", [])]
    if not rows:
        return {"stop_hook_runs": 0, "basis": "no stop_hook_summary records"}
    ms = [h["ms_sum"] for h in rows]
    return {"stop_hook_runs": len(rows), "hooks_per_run_max": max(h["n"] for h in rows), "seconds_total": round(sum(ms) / 1000.0, 1),
            "ms_per_stop": _dist(ms), "errors": sum(h["errors"] for h in rows), "prevented_continuation": sum(1 for h in rows if h["prevented"]),
            "basis": "stop_hook_summary records (durationMs per hook, measured by the harness)"}


# --- self-reported tool durations ------------------------------------------------------------

def measures(sessions):
    by = defaultdict(list)
    meta = {}
    for S in sessions:
        for m in S.get("measures", []):
            by[m["id"]].append(m["sec"])
            meta[m["id"]] = (m["kind"], m["label"])
    out = []
    for k, v in by.items():
        d = _dist(v)
        d.update({"id": k, "kind": meta[k][0], "label": meta[k][1]})
        out.append(d)
    out.sort(key=lambda d: -d["sum"])
    return {"items": out, "basis": "durations printed by the tools themselves (rules/measures.json); only where the output was captured"}


# --- activity intervals (for joining external telemetry) -------------------------------------

def activity_intervals(analyses):
    """Merged autonomous intervals of the main sessions (subagent time sits inside subagent_wait)."""
    ivs = []
    for a in analyses:
        for s, e, c in a["tl"]["slices"]:
            if c and not c.startswith(EXCLUDED) and e > s:
                ivs.append((s, e))
    return _merge(ivs)


def heavy_intervals(analyses):
    by = defaultdict(list)
    for a in analyses:
        for iv in a["heavy_ivs"]:
            by[iv[2]].append((iv[0], iv[1]))
    return {k: _merge(v) for k, v in by.items()}


def _merge(ivs):
    out = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _overlap(ivs, starts, s, e):
    i = max(0, bisect.bisect_right(starts, s) - 1)
    tot = 0.0
    while i < len(ivs) and ivs[i][0] < e:
        a, b = max(ivs[i][0], s), min(ivs[i][1], e)
        if b > a:
            tot += b - a
        i += 1
    return tot


# --- sysstat ---------------------------------------------------------------------------------

def _sadf_rows(path):
    try:
        out = subprocess.run(["sadf", "-j", path, "--", "-u", "-r", "-d"], capture_output=True, text=True, timeout=120).stdout
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    rows = []
    for h in data.get("sysstat", {}).get("hosts", []):
        for st in h.get("statistics", []):
            ts = st.get("timestamp") or {}
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                tt = time.strptime(ts["date"] + " " + ts["time"], fmt)
                t = _timegm(tt) if ts.get("utc", 1) else time.mktime(tt)
            except (KeyError, ValueError):
                continue
            cpu = (st.get("cpu-load") or [{}])[0]
            mem = st.get("memory") or {}
            disks = [d for d in st.get("disk", []) if not str(d.get("disk-device", "")).startswith(("loop", "zram", "ram", "sr"))]
            if "idle" not in cpu:
                continue
            rows.append({"t": t, "interval": float(ts.get("interval") or 600), "cpu_pct": round(100.0 - cpu["idle"], 2), "iowait_pct": cpu.get("iowait"),
                         "steal_pct": cpu.get("steal"), "mem_used_pct": mem.get("memused-percent"), "commit_pct": mem.get("commit-percent"),
                         "disk_util_pct": max((d.get("util-percent", 0) for d in disks), default=None),
                         "disk_await_ms": max((d.get("await", 0) for d in disks), default=None)})
    return rows


def _timegm(tt):
    import calendar
    return calendar.timegm(tt)


SAT = {"cpu_pct": 85.0, "iowait_pct": 10.0, "steal_pct": 10.0, "mem_used_pct": 90.0, "disk_util_pct": 80.0}


def sysstat(dirpath, since, until, analyses):
    if not shutil.which("sadf"):
        return {"samples": 0, "basis": "sadf not installed (sysstat)"}
    lo = (since or 0) - 86400
    files = [p for p in sorted(glob.glob(os.path.join(dirpath, "sa[0-9][0-9]"))) if os.path.getmtime(p) >= lo]
    rows = []
    for p in files:
        rows += _sadf_rows(p)
    rows = [r for r in rows if (since is None or r["t"] >= since) and (until is None or r["t"] - r["interval"] <= until)]
    rows.sort(key=lambda r: r["t"])
    if not rows:
        return {"samples": 0, "basis": "no sysstat samples in the window (%s)" % dirpath}
    act = activity_intervals(analyses)
    act_starts = [s for s, _ in act]
    heavy = heavy_intervals(analyses)
    keys = ("cpu_pct", "iowait_pct", "steal_pct", "mem_used_pct", "commit_pct", "disk_util_pct", "disk_await_ms")
    allv = defaultdict(list)
    wsum = defaultdict(float)
    wtot = 0.0
    sat_act = defaultdict(float)
    for r in rows:
        s, e = r["t"] - r["interval"], r["t"]
        w = _overlap(act, act_starts, s, e) if act else 0.0
        wtot += w
        for k in keys:
            v = r.get(k)
            if isinstance(v, (int, float)):
                allv[k].append(v)
                wsum[k] += v * w
        for k, lim in SAT.items():
            v = r.get(k)
            if isinstance(v, (int, float)) and v >= lim:
                sat_act[k] += w
    during = {k: round(wsum[k] / wtot, 1) for k in keys if wtot and allv.get(k)}
    by_heavy = {}
    for cat, ivs in heavy.items():
        st = [a for a, _ in ivs]
        ws = defaultdict(float)
        wt = 0.0
        for r in rows:
            w = _overlap(ivs, st, r["t"] - r["interval"], r["t"])
            if w:
                wt += w
                for k in ("cpu_pct", "iowait_pct", "mem_used_pct", "disk_util_pct"):
                    if isinstance(r.get(k), (int, float)):
                        ws[k] += r[k] * w
        if wt >= 60:
            by_heavy[cat] = dict({k: round(v / wt, 1) for k, v in ws.items()}, seconds=round(wt, 1))
    notes = []
    for k, lim in SAT.items():
        if sat_act.get(k, 0) >= 300:
            notes.append("%s >= %.0f during %s of this scope's autonomous time" % (k, lim, fmt_dur(sat_act[k])))
    for cat, d in by_heavy.items():
        if d.get("cpu_pct", 0) >= 85:
            notes.append("%s ran while the host CPU averaged %.0f%%: CPU-bound or contended" % (cat, d["cpu_pct"]))
        elif d.get("cpu_pct") is not None and d["cpu_pct"] < 30 and d.get("iowait_pct", 0) < 5:
            notes.append("%s ran while the host CPU averaged %.0f%%: not CPU-bound (waiting on network, locks or a single thread)" % (cat, d["cpu_pct"]))
    return {"samples": len(rows), "interval_s": rows[0]["interval"], "files": len(files), "activity_seconds_covered": round(wtot, 1),
            "host": {k: _dist(v) for k, v in allv.items()}, "during_autonomous_avg": during,
            "saturated_during_autonomous_s": {k: round(v, 1) for k, v in sat_act.items()}, "by_heavy_class": by_heavy, "findings": notes,
            "basis": "sysstat archives (sadf -j); host-wide numbers at %ss resolution, so other workloads on the same host are included" % int(rows[0]["interval"])}


# --- job records of a wrapper (queues, admission gates, memory peaks) ------------------------

_F = {"start": ("started", "start", "started_at", "t0"), "end": ("ended", "end", "ended_at", "t1"),
      "wait": ("queue_wait_s", "gate_wait_s", "wait_s", "lock_wait_s"), "peak": ("peak_bytes", "peak", "max_rss_bytes"),
      "exit": ("exit", "exit_code", "returncode"), "reason": ("reason", "kill_reason"), "name": ("name", "job", "label"), "cwd": ("cwd", "dir")}


def _get(d, key):
    for k in _F[key]:
        if k in d:
            return d[k]
    return None


def jobs(dirpath, since, until, cwds, project_dirs=()):
    """JSON job records (one file per job, or a .jsonl). Fields are matched by common names, see _F."""
    recs = []
    paths = sorted(glob.glob(os.path.join(dirpath, "*.json"))) + sorted(glob.glob(os.path.join(dirpath, "*.jsonl")))
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read()
        except OSError:
            continue
        items = []
        if p.endswith(".jsonl"):
            for line in txt.splitlines():
                try:
                    items.append(json.loads(line))
                except ValueError:
                    pass
        else:
            try:
                items.append(json.loads(txt))
            except ValueError:
                pass
        recs += [d for d in items if isinstance(d, dict)]
    if not recs:
        return {"jobs": 0, "basis": "no job records in %s" % dirpath}
    starts = [s for s in (_get(d, "start") for d in recs) if isinstance(s, (int, float))]
    oldest = min(starts) if starts else None
    cwds = [c.rstrip("/") for c in cwds if c]
    sel = []
    for d in recs:
        s, e = _get(d, "start"), _get(d, "end")
        if not isinstance(s, (int, float)):
            continue
        if (since is not None and s < since) or (until is not None and s > until):
            continue
        cwd = str(_get(d, "cwd") or "")
        # a job belongs to the scope when it ran in a session's directory, or in a per-project scratch dir that
        # carries the encoded project directory name (e.g. /tmp/<tool>/<-home-me-git-app>/<session>/...)
        if cwds and not any(cwd == c or cwd.startswith(c + "/") for c in cwds) and not any("/" + d + "/" in cwd + "/" for d in project_dirs):
            continue
        sel.append(d)
    out = {"jobs": len(sel), "records_total": len(recs), "oldest_record": oldest,
           "window_covered_from": oldest, "basis": "job records in %s, matched to this scope by working directory" % os.path.basename(dirpath.rstrip("/"))}
    if since is not None and oldest is not None and oldest > since + 3600:
        out["gap"] = "records only go back to %s; the window starts %s: older jobs were pruned, so totals are partial" % (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(oldest)), time.strftime("%Y-%m-%d %H:%M", time.localtime(since)))
    if not sel:
        return out
    runs = [(_get(d, "end") - _get(d, "start")) for d in sel if isinstance(_get(d, "end"), (int, float))]
    waits = [float(_get(d, "wait") or 0) for d in sel]
    peaks = [float(_get(d, "peak")) / 2 ** 30 for d in sel if isinstance(_get(d, "peak"), (int, float))]
    killed = [d for d in sel if _get(d, "reason") or _get(d, "exit") in (137, -9)]
    by = defaultdict(lambda: [0, 0.0, 0.0])
    for d in sel:
        k = str(_get(d, "name") or "?")[:40]
        by[k][0] += 1
        if isinstance(_get(d, "end"), (int, float)):
            by[k][1] += _get(d, "end") - _get(d, "start")
        by[k][2] += float(_get(d, "wait") or 0)
    out.update({"run_s": _dist(runs), "queue_wait_s": _dist(waits), "waited_jobs": sum(1 for w in waits if w > 0.5),
                "peak_gib": _dist(peaks), "killed": len(killed),
                "by_name": [{"name": k, "jobs": v[0], "run_s": round(v[1], 1), "wait_s": round(v[2], 1)} for k, v in sorted(by.items(), key=lambda kv: -kv[1][1])[:10]]})
    return out


# --- host snapshot ---------------------------------------------------------------------------

def host_snapshot(path="/"):
    snap = {"taken": round(time.time())}
    try:
        du = shutil.disk_usage(path)
        snap["disk_free_gb"] = round(du.free / 1e9, 1)
        snap["disk_used_pct"] = round(100.0 * du.used / max(1, du.used + du.free), 1)  # as df reports it
    except OSError:
        pass
    snap["cpus"] = os.cpu_count()
    snap["gpu"] = "nvidia-smi present" if shutil.which("nvidia-smi") else "no nvidia-smi on PATH (no NVIDIA GPU visible)"
    return snap


# --- coverage matrix -------------------------------------------------------------------------

def coverage(rep):
    t = rep.get("telemetry", {})
    env = rep.get("environment", {})
    sig = {s["id"]: s["count"] for s in env.get("signals", [])}
    ss = t.get("sysstat") or {}
    ml = rep.get("machine") or {}
    lat = t.get("latency") or {}
    jb = t.get("jobs") or {}
    me = {m["kind"] for m in (t.get("measures") or {}).get("items", [])}
    dora = t.get("dora")
    have_ss = ss.get("samples", 0) > 0
    have_ml = ml.get("samples", 0) > 0

    def row(dim, status, source, note=""):
        return {"dimension": dim, "status": status, "source": source, "note": note}

    rows = [
        row("wall-clock attribution", "measured", "transcript timestamps", "classification of each interval is rule-based"),
        row("tokens / quota", "measured", "usage fields per request", "unit weights in rules/models.json are an estimate, not price"),
        row("API latency", "measured" if lat.get("matched") else "missing", "api-request send records" if lat.get("matched") else "-",
            "first content block, not true TTFT" if lat.get("matched") else "this Claude Code version writes no send records"),
        row("waiting / polling", "measured", "tool call durations", "which call is a poll is inferred from the command"),
        row("subagents", "measured", "subagent transcripts", ""),
        row("builds / tests (local)", "measured", "tool call durations" + (" + tool-printed durations" if me & {"build", "test"} else ""), ""),
        row("cold starts", "measured" if (me & {"cold_start", "install"} or lat.get("matched")) else "inferred",
            ", ".join(x for x in (("dev-server ready lines" if "cold_start" in me else ""), ("install durations" if "install" in me else ""),
                                  ("first request of each session" if lat.get("matched") else "")) if x) or "install commands", ""),
        row("CPU", "measured" if (have_ss or have_ml) else "inferred", "sysstat" if have_ss else ("machine log" if have_ml else "error text in tool output"),
            "host-wide, includes other workloads" if have_ss else ("" if have_ml else "%d hint(s)" % sig.get("cpu_pressure", 0))),
        row("memory", "measured" if (have_ss or have_ml) else "inferred", "sysstat" if have_ss else ("machine log" if have_ml else "OOM text in tool output"),
            "%d OOM/kill hint(s) in tool output" % (sig.get("oom", 0) + sig.get("sigkill", 0))),
        row("disk I/O", "measured" if have_ss else "missing", "sysstat -d" if have_ss else "-", ""),
        row("disk space", "snapshot" if t.get("host") else "inferred", "current free space only" if t.get("host") else "ENOSPC text",
            "no history of free space; %d ENOSPC hint(s)" % sig.get("disk_full", 0)),
        row("GPU", "n/a" if "no nvidia" in str((t.get("host") or {}).get("gpu", "")) else ("measured" if have_ml else "missing"),
            str((t.get("host") or {}).get("gpu", "-")), ""),
        row("queues / locks", "measured" if jb.get("jobs") else "missing", "job records" if jb.get("jobs") else "-", jb.get("gap", "")),
        row("hook overhead", "measured" if (t.get("hooks") or {}).get("stop_hook_runs") else "missing", "stop_hook_summary records", ""),
        row("CI / DORA", ("measured" if any(str(v.get("kind", "")).startswith("measured") for v in (dora.get("metrics") or {}).values()) or dora.get("ci") else "proxy") if dora else "missing", "analyze.py dora (git + CI runs)" if dora else "-",
            "deployment data only when a deploy workflow or deploy log exists" if dora else "run `analyze.py dora --repo PATH`"),
    ]
    return rows
