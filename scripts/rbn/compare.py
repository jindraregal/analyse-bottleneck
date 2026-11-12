"""Before/after comparison of two reports (two windows or two sessions)."""


def _per_hour(x, auto_s):
    return x / (auto_s / 3600.0) if auto_s > 0 else 0.0


def summarize(rep):
    w = rep["wall_clock"]
    q = rep["quota"]
    auto = w["autonomous_s"]
    cats = {}
    for r in w["categories"]:
        if not r["excluded_from_autonomous"]:
            cats[r["category"]] = r["seconds"]
    return {
        "sessions": rep["meta"]["sessions"], "autonomous_min": round(auto / 60.0, 1), "span_min": round(w["span_s"] / 60.0, 1),
        "idle_pct_of_span": round(100 * w["idle_s"] / w["span_s"], 1) if w["span_s"] else 0,
        "units": q["total_units"], "units_per_autonomous_hour": round(_per_hour(q["total_units"], auto)),
        "requests_per_autonomous_hour": round(_per_hour(q["requests"], auto), 1),
        "cache_hit_rate": q["cache_hit_rate"], "cache_miss_waste_units": q["cache_misses"]["waste_units"],
        "category_pct_of_autonomous": {k: round(100 * v / auto, 1) for k, v in cats.items()} if auto else {},
        "signals": {s["id"]: s["count"] for s in rep["environment"]["signals"]},
        "findings": {c["id"]: {"minutes": c["saving_minutes"], "units": c["saving_units"]} for c in rep["ranked"]},
        "finding_count_total": rep.get("ranked_total", 0),
    }


def _delta(a, b, lower_is_better=True, unit=""):
    if a is None or b is None:
        return {"before": a, "after": b, "verdict": "n/a"}
    d = b - a
    tol = 0.05 * abs(a) if a else 0
    verdict = "same" if abs(d) <= tol else (("improved" if d < 0 else "regressed") if lower_is_better else ("improved" if d > 0 else "regressed"))
    return {"before": a, "after": b, "delta": round(d, 3), "verdict": verdict}


def compare(rep_a, rep_b):
    A, B = summarize(rep_a), summarize(rep_b)
    out = {"before": A, "after": B, "deltas": {}}
    d = out["deltas"]
    d["units_per_autonomous_hour"] = _delta(A["units_per_autonomous_hour"], B["units_per_autonomous_hour"])
    d["requests_per_autonomous_hour"] = _delta(A["requests_per_autonomous_hour"], B["requests_per_autonomous_hour"])
    d["cache_hit_rate"] = _delta(A["cache_hit_rate"], B["cache_hit_rate"], lower_is_better=False)
    d["cache_miss_waste_units"] = _delta(A["cache_miss_waste_units"], B["cache_miss_waste_units"])
    d["idle_pct_of_span"] = _delta(A["idle_pct_of_span"], B["idle_pct_of_span"])
    cats = set(A["category_pct_of_autonomous"]) | set(B["category_pct_of_autonomous"])
    d["category_pct_of_autonomous"] = {c: _delta(A["category_pct_of_autonomous"].get(c, 0), B["category_pct_of_autonomous"].get(c, 0)) for c in sorted(cats)}
    sig = set(A["signals"]) | set(B["signals"])
    d["signals"] = {s: _delta(A["signals"].get(s, 0), B["signals"].get(s, 0)) for s in sorted(sig)}
    fixed = sorted(set(A["findings"]) - set(B["findings"]))
    new = sorted(set(B["findings"]) - set(A["findings"]))
    persisting = sorted(set(A["findings"]) & set(B["findings"]))
    out["findings"] = {"resolved": fixed, "new": new, "persisting": persisting}
    return out


def render(cmp, label_a="before", label_b="after"):
    A, B = cmp["before"], cmp["after"]
    L = ["# Before / after", "",
         "| metric | %s | %s | verdict |" % (label_a, label_b), "| --- | --- | --- | --- |"]
    for k, v in cmp["deltas"].items():
        if "verdict" in v:
            L.append("| %s | %s | %s | %s |" % (k, v["before"], v["after"], v["verdict"]))
    L.append("")
    moves = []
    for c, v in cmp["deltas"]["category_pct_of_autonomous"].items():
        if v.get("verdict") in ("improved", "regressed") and abs(v.get("delta", 0)) >= 3:
            moves.append("%s %s -> %s%% of autonomous (%s)" % (c, v["before"], v["after"], "down" if v["delta"] < 0 else "up"))
    if moves:
        L.append("Category shifts: " + "; ".join(moves))
    f = cmp["findings"]
    L.append("Findings: resolved %s; new %s; still present %s" % (", ".join(f["resolved"]) or "none", ", ".join(f["new"]) or "none", ", ".join(f["persisting"]) or "none"))
    L.append("")
    L.append("Windows are compared per autonomous hour, so different lengths are comparable; tiny samples are noisy.")
    return "\n".join(L) + "\n"
