"""Join optional machine samples (scripts/sample_machine.py) with the attributed timeline."""
import bisect
import json
from collections import defaultdict


def load(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("t"), (int, float)):
                    rows.append(d)
    except OSError:
        return []
    rows.sort(key=lambda r: r["t"])
    return rows


def correlate(path, analyses):
    rows = load(path)
    if not rows:
        return {"samples": 0, "note": "no usable samples in %s" % path}
    ts = [r["t"] for r in rows]
    acc = defaultdict(lambda: defaultdict(list))
    covered = 0
    for a in analyses:
        for s, e, c in a["tl"]["slices"]:
            if not c:
                continue
            i = bisect.bisect_left(ts, s)
            while i < len(ts) and ts[i] <= e:
                r = rows[i]
                covered += 1
                for k in ("cpu_pct", "load1", "mem_avail_pct", "disk_free_gb", "gpu_util"):
                    if isinstance(r.get(k), (int, float)):
                        acc[c.split(":", 1)[0]][k].append(r[k])
                i += 1
    by = {}
    for cat, d in acc.items():
        by[cat] = {k: {"avg": round(sum(v) / len(v), 1), "max": round(max(v), 1), "min": round(min(v), 1)} for k, v in d.items()}
    allv = defaultdict(list)
    for r in rows:
        for k in ("cpu_pct", "mem_avail_pct", "disk_free_gb", "gpu_util"):
            if isinstance(r.get(k), (int, float)):
                allv[k].append(r[k])
    notes = []
    if allv.get("mem_avail_pct") and min(allv["mem_avail_pct"]) < 8:
        notes.append("memory available fell below 8%% (min %.1f%%): OOM signals are corroborated" % min(allv["mem_avail_pct"]))
    if allv.get("disk_free_gb") and min(allv["disk_free_gb"]) < 1.0:
        notes.append("free disk fell below 1 GB (min %.2f GB): ENOSPC signals are corroborated" % min(allv["disk_free_gb"]))
    for cat in ("build", "test", "media_gpu", "install"):
        c = by.get(cat, {}).get("cpu_pct")
        if c and c["avg"] >= 85:
            notes.append("%s ran at avg %.0f%% CPU: CPU-bound, more cores or less parallel work would help" % (cat, c["avg"]))
        elif c and c["avg"] < 30:
            notes.append("%s ran at avg %.0f%% CPU: waiting on I/O or network, not CPU" % (cat, c["avg"]))
    if allv.get("gpu_util") and max(allv["gpu_util"]) < 5 and "media_gpu" in by:
        notes.append("GPU stayed idle (max %.0f%%) during media jobs: the GPU is not being used" % max(allv["gpu_util"]))
    return {"samples": len(rows), "samples_in_activity": covered, "by_category": by, "corroboration": notes,
            "min_mem_avail_pct": min(allv["mem_avail_pct"]) if allv.get("mem_avail_pct") else None,
            "min_disk_free_gb": min(allv["disk_free_gb"]) if allv.get("disk_free_gb") else None}
