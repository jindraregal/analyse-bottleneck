"""Watch mode: summarise the newest session, append to a history JSONL, print only threshold warnings.

Opt-in by design: nothing here edits settings files. The hook is switched with
`analyze.py watch-ctl on|off` (a flag file) or by removing it from settings; the env var
ANALYSE_BOTTLENECK_WATCH=off disables it for one shell.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

from . import __version__
from .util import fmt_dur, parse_when


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "analyse-bottleneck")


def default_history():
    return os.path.join(state_dir(), "history.jsonl")


def disabled(state):
    flag = os.environ.get("ANALYSE_BOTTLENECK_WATCH") or os.environ.get("RUN_BOTTLENECK_WATCH", "")  # the old name still works
    return flag.lower() in ("0", "off", "false", "no") or os.path.exists(os.path.join(state, "disabled"))


def set_disabled(state, off):
    os.makedirs(state, exist_ok=True)
    p = os.path.join(state, "disabled")
    if off:
        with open(p, "w") as f:
            f.write("disabled by watch-ctl off at %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    elif os.path.exists(p):
        os.remove(p)


def log_error(state, text):
    state = state or state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "errors.log"), "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), text[:500]))


def should_skip(history, tpath, min_interval):
    sid = os.path.basename(tpath)[:-6]
    last = [h for h in history if h.get("session") == sid]
    if not last:
        return False
    size = os.path.getsize(tpath)
    if last[-1].get("size") == size:
        return True
    # adaptive: never spend more than ~10% of one core; a 50 s analysis runs at most every 500 s
    cost = (last[-1].get("audit") or {}).get("analysis_s") or 0
    gap = max(min_interval or 0, 10.0 * cost)
    return bool(gap and time.time() - last[-1].get("t", 0) < gap)


def spawn_detached(argv):
    """Start the analysis in its own session at low priority; the hook returns immediately."""
    pre = []
    if hasattr(os, "nice"):
        pre = ["nice", "-n", "15"] if _which("nice") else []
    try:
        subprocess.Popen(pre + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError:
        pass


def _which(cmd):
    import shutil
    return shutil.which(cmd)


def try_lock(state):
    os.makedirs(state, exist_ok=True)
    f = open(os.path.join(state, "watch.lock"), "w")
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        return f  # no flock (Windows): run unlocked
    except OSError:
        f.close()
        return None
    return f


def time_limit(seconds):
    try:
        import signal
        if seconds <= 0:
            signal.alarm(0)  # cancel
            return
        signal.signal(signal.SIGALRM, lambda *a: sys.exit(0))
        signal.alarm(max(1, int(seconds)))
    except (ImportError, AttributeError, ValueError):
        pass


def take_pending(state):
    p = os.path.join(state, "pending.json")
    try:
        with open(p, encoding="utf-8") as f:
            msgs = json.load(f)
        os.remove(p)
        return [m for m in msgs if isinstance(m, str)][:10]
    except (OSError, ValueError):
        return []


def put_pending(state, msgs):
    os.makedirs(state, exist_ok=True)
    old = take_pending(state)
    tmp = os.path.join(state, "pending.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump((old + msgs)[-10:], f)
    os.replace(tmp, os.path.join(state, "pending.json"))


def rules_hash(rs):
    h = hashlib.sha256()
    for d in rs.dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                with open(os.path.join(d, name), "rb") as f:
                    h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()[:16]


def audit(rep, rs, tpath, t0):
    """Enough to re-run the same analysis and check every number: source file, its size and mtime, tool and rules version, own cost."""
    a = {"tool_version": __version__, "rules_sha": rules_hash(rs), "analysis_s": round(time.time() - t0, 2), "python": sys.version.split()[0]}
    if tpath and os.path.isfile(tpath):
        st = os.stat(tpath)
        a.update({"transcript": tpath, "transcript_bytes": st.st_size, "transcript_mtime": round(st.st_mtime)})
    try:
        import resource
        a["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except (ImportError, AttributeError):
        pass
    a["recompute"] = "analyze.py --session %s --format json" % (tpath or "latest")
    return a


def hook_snippet(analyze_path):
    cmd = "python3 %s watch --hook-json --detach --min-interval 300" % analyze_path
    return json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]}}, indent=2) + (
        "\n\n# Merge this into the existing \"Stop\" list of ~/.claude/settings.json (or a project .claude/settings.json) by hand;"
        "\n# do not replace hooks that are already there. Undo: remove the entry, or run 'analyze.py watch-ctl off'.")


def status(state):
    hist = read_history(os.path.join(state, "history.jsonl"), limit=100000)
    off = disabled(state)
    L = ["analyse-bottleneck watch: %s (state dir %s)" % ("OFF" if off else "on (runs only if a hook calls it)", state)]
    L.append("history: %d analyses" % len(hist))
    if hist:
        au = [h.get("audit", {}).get("analysis_s") for h in hist if h.get("audit", {}).get("analysis_s") is not None]
        if au:
            au.sort()
            L.append("own cost: median %.2fs, max %.2fs per analysis (in a background process)" % (au[len(au) // 2], au[-1]))
        L.append("last: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(hist[-1].get("t", 0))))
    errs = os.path.join(state, "errors.log")
    if os.path.exists(errs):
        L.append("errors logged: %s" % errs)
    return "\n".join(L)


def report(history, since=None):
    lo = parse_when(since) if since else 0
    rows = [h for h in history if h.get("t", 0) >= lo]
    if not rows:
        return "No watch history in this window.\n"
    latest = {}
    for h in rows:
        latest[h.get("session")] = h  # the last line per session is its final state
    sess = list(latest.values())
    auto = sum(h.get("autonomous_s", 0) for h in sess)
    cats = {}
    for h in sess:
        for k, v in (h.get("cat_s") or {}).items():
            cats[k] = cats.get(k, 0) + v
    warn = {}
    for h in rows:
        for k in (h.get("warned") or {}):
            warn.setdefault(k, set()).add(h.get("session"))
    L = ["# analyse-bottleneck watch report", "",
         "%d sessions, %d analyses, autonomous %s, %s units." % (len(sess), len(rows), fmt_dur(auto), "{:,}".format(sum(h.get("units", 0) for h in sess)))]
    L.append("")
    L.append("Time by class (autonomous classes only):")
    for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:10]:
        if k in ("human_wait", "idle_gap"):
            continue
        L.append("- %s %s (%.0f%%)" % (k, fmt_dur(v), 100.0 * v / auto if auto else 0))
    if warn:
        L.append("")
        L.append("Recurring warnings (sessions):")
        for k, v in sorted(warn.items(), key=lambda kv: -len(kv[1]))[:10]:
            L.append("- %s: %d" % (k, len(v)))
    au = sorted(h.get("audit", {}).get("analysis_s") for h in rows if h.get("audit", {}).get("analysis_s") is not None)
    if au:
        L.append("")
        L.append("Own overhead: %d background analyses, median %.2fs, total %s." % (len(au), au[len(au) // 2], fmt_dur(sum(au))))
    return "\n".join(L) + "\n"


def read_history(path, limit=400):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows[-limit:]


def summary_line(rep, session_id, file_size):
    w = rep["wall_clock"]
    q = rep["quota"]
    env = rep["environment"]
    auto = w["autonomous_s"]
    top = [r for r in w["categories"] if not r["excluded_from_autonomous"]][:1]
    cats = {r["category"].split(":", 1)[0]: 0.0 for r in w["categories"]}
    for r in w["categories"]:
        cats[r["category"].split(":", 1)[0]] += r["seconds"]
    return {
        "t": round(time.time()), "session": session_id, "size": file_size, "span_s": w["span_s"], "autonomous_s": auto, "idle_s": w["idle_s"],
        "human_s": w["human_s"], "units": q["total_units"], "requests": q["requests"], "cache_hit_rate": q["cache_hit_rate"],
        "cache_misses": q["cache_misses"]["count"], "oversized": q["oversized_results"]["count"], "repeat_reads": q["repeated_reads"]["repeat_reads"],
        "retry_loops": q["retry_loops"]["count"], "polling_s": q["polling"]["seconds"],
        "cat_s": {k: round(v, 1) for k, v in cats.items()},
        "signals": {s["id"]: s["count"] for s in env["signals"] if s["confidence"] != "low"},
        "top_saving": [{"id": c["id"], "min": c["saving_minutes"]} for c in rep["ranked"][:5]],
        "top_class": top[0]["category"] if top else None,
    }


def warnings_for(line, th, history):
    out = []
    auto_min = line["autonomous_s"] / 60.0
    if auto_min < th.get("min_autonomous_minutes_to_judge", 5):
        return out
    span = line["span_s"] or 1
    auto = line["autonomous_s"] or 1

    def add(key, magnitude, text):
        prev = [h.get("warned", {}).get(key) for h in history if h.get("session") == line["session"]]
        prev = [p for p in prev if p is not None]
        if prev and magnitude <= max(prev) * 1.25:
            return
        out.append((key, magnitude, text))

    if line["idle_s"] / span > th.get("idle_gap_share_of_span", 0.15):
        add("idle", line["idle_s"], "idle gaps %s (%.0f%% of span): stalled on a prompt, rate limit or sleeping host?" % (fmt_dur(line["idle_s"]), 100 * line["idle_s"] / span))
    if line["cat_s"].get("poll_wait", 0) / auto > th.get("poll_wait_share_of_autonomous", 0.10):
        add("poll", line["cat_s"]["poll_wait"], "polling/sleep is %.0f%% of autonomous time: use one blocking wait" % (100 * line["cat_s"]["poll_wait"] / auto))
    for cls, sec in line["cat_s"].items():
        if cls in ("inference", "thinking", "human_wait", "idle_gap", "overhead", "background_wait", "subagent_wait"):
            continue
        if sec / auto > th.get("single_tool_class_share_of_autonomous", 0.45):
            add("class:" + cls, sec, "%s takes %.0f%% of autonomous time (%s)" % (cls, 100 * sec / auto, fmt_dur(sec)))
    chr_ = line.get("cache_hit_rate")
    if chr_ is not None and line["requests"] >= 20 and chr_ < th.get("cache_hit_rate_min", 0.8):
        add("cache_rate", 1.0 - chr_, "cache hit rate %.0f%% (expected >= %.0f%%)" % (100 * chr_, 100 * th.get("cache_hit_rate_min", 0.8)))
    if line["cache_misses"] > th.get("cache_miss_events_max", 3):
        add("cache_miss", line["cache_misses"], "%d prompt-cache rebuilds" % line["cache_misses"])
    if line["oversized"] > th.get("oversized_results_max", 5):
        add("oversized", line["oversized"], "%d oversized tool outputs in context" % line["oversized"])
    if line["repeat_reads"] > th.get("repeat_read_max", 6):
        add("repeat_reads", line["repeat_reads"], "%d repeated file reads" % line["repeat_reads"])
    if line["retry_loops"] > th.get("retry_loops_max", 1):
        add("retry", line["retry_loops"], "%d failing retry loops" % line["retry_loops"])
    for sid, mx in th.get("signal_counts_max", {}).items():
        n = line["signals"].get(sid, 0)
        if n > mx:
            add("signal:" + sid, n, "%s x%d (inferred from tool output)" % (sid, n))
    for t in line["top_saving"]:
        if t["min"] >= th.get("rule_saving_minutes_min", 3.0):
            add("saving:" + t["id"], t["min"], "%s could save ~%.0f min (run: analyze.py --session latest)" % (t["id"], t["min"]))
    return out


def append(path, line, warned):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = dict(line)
    row["warned"] = {k: m for k, m, _ in warned}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
