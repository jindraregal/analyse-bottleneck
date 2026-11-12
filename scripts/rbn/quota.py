"""Quota waste: where tokens (weighted into 'units') go that did not need to go."""
import bisect
from collections import defaultdict

from .util import est_tokens, norm_command, template_command, redact


def req_units(rs, r):
    u = r["usage"]
    w = rs.weights
    inp = u.get("input_tokens", 0)
    out = u.get("output_tokens", 0)
    cr = u.get("cache_read_input_tokens", 0)
    cc = u.get("cache_creation_input_tokens", 0)
    c1h = u.get("ephemeral_1h_input_tokens", 0)
    c5 = u.get("ephemeral_5m_input_tokens", cc - c1h if cc >= c1h else cc)
    if "ephemeral_5m_input_tokens" not in u and "ephemeral_1h_input_tokens" not in u:
        c5, c1h = cc, 0
    tier, cost = rs.tier_of(r.get("model"))
    raw = inp * w["input"] + out * w["output"] + cr * w["cache_read"] + c5 * w["cache_write_5m"] + c1h * w["cache_write_1h"]
    return raw * cost, tier, cost


def collect_requests(sessions, rs):
    seen = {}
    chains = []
    for S in sessions:
        chain = []
        for r in S["req_list"]:
            if r["model"] is None and not r["usage"]:
                continue
            if r["id"] in seen:
                continue
            if r.get("model") == "<synthetic>":
                continue
            r["units"], r["tier"], r["cost"] = req_units(rs, r)
            seen[r["id"]] = r
            chain.append(r)
        chains.append((S, chain))
    return seen, chains


def tokens_by_model(reqs):
    out = {}
    for r in reqs.values():
        m = r.get("model") or "unknown"
        d = out.setdefault(m, {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "units": 0.0, "tier": r["tier"]})
        u = r["usage"]
        d["requests"] += 1
        d["input"] += u.get("input_tokens", 0)
        d["output"] += u.get("output_tokens", 0)
        d["cache_read"] += u.get("cache_read_input_tokens", 0)
        d["cache_create"] += u.get("cache_creation_input_tokens", 0)
        d["units"] += r["units"]
    for d in out.values():
        tot = d["input"] + d["cache_read"] + d["cache_create"]
        d["cache_hit_rate"] = round(d["cache_read"] / tot, 3) if tot else None
        d["units"] = round(d["units"])
    return out


def _has_between(sorted_ts, lo, hi):
    i = bisect.bisect_left(sorted_ts, lo)
    return i < len(sorted_ts) and sorted_ts[i] <= hi


def cache_misses(chains, rs, comp_ts):
    w = rs.weights
    events = []
    causes = defaultdict(lambda: [0, 0.0])
    for S, chain in chains:
        comps = sorted(c["ts"] for c in S["compactions"])
        humans = sorted(S["human_ts"])
        for prev, r in zip(chain, chain[1:]):
            pu, u = prev["usage"], r["usage"]
            pctx = pu.get("cache_read_input_tokens", 0) + pu.get("cache_creation_input_tokens", 0) + pu.get("input_tokens", 0)
            cc, cr = u.get("cache_creation_input_tokens", 0), u.get("cache_read_input_tokens", 0)
            if pctx < 4000 or cc < max(4000, 0.5 * pctx) or cr > 0.3 * pctx:
                continue
            gap = r["start"] - prev["end"]
            ttl = rs.cache_ttl["1h"] if pu.get("ephemeral_1h_input_tokens", 0) > 0 else rs.cache_ttl["5m"]
            i = bisect.bisect_left(comps, prev["end"] - 1)
            if i < len(comps) and comps[i] <= r["end"] + 1:
                cause = "compaction"
            elif gap > 60 and _has_between(humans, prev["end"] - 1, r["start"] + 3):
                cause = "human_idle"  # a person was away or typing: the rebuild is not the run's fault
            elif prev.get("model") != r.get("model"):
                cause = "model_switch"
            elif gap > ttl:
                cause = "idle_over_ttl"
            else:
                cause = "prefix_changed"
            waste = cc * (w["cache_write_5m"] - w["cache_read"]) * r["cost"]
            if cause in ("compaction", "human_idle"):
                waste = 0.0  # expected, not waste
            causes[cause][0] += 1
            causes[cause][1] += waste
            events.append({"cause": cause, "gap_s": round(gap), "rebuilt_tokens": cc, "waste_units": round(waste), "ts": r["start"], "file": S["file"], "model": r.get("model")})
    events.sort(key=lambda e: -e["waste_units"])
    return {"count": sum(1 for e in events if e["cause"] not in ("compaction", "human_idle")), "by_cause": {k: {"count": v[0], "waste_units": round(v[1])} for k, v in causes.items()},
            "waste_units": round(sum(v[1] for v in causes.values())), "top": events[:5], "max_gap_s": max([e["gap_s"] for e in events if e["cause"] == "idle_over_ttl"] or [0])}


def _after_counts(chains):
    idx = {}
    for S, chain in chains:
        starts = [r["start"] for r in chain]
        comps = sorted(c["ts"] for c in S["compactions"])
        idx[S["file"]] = (starts, comps, sum(r["cost"] for r in chain) / len(chain) if chain else 1.0)
    return idx


def oversized(chains, sessions, rs):
    idx = _after_counts(chains)
    res = []
    for S in sessions:
        starts, comps, avgcost = idx.get(S["file"], ([], [], 1.0))
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            ch = max(t["chars"], t["persisted"])
            if ch < rs.oversized_chars or t["t1"] is None:
                continue
            tok = est_tokens(ch, rs.cpt)
            i = bisect.bisect_right(starts, t["t1"])
            j = bisect.bisect_right(comps, t["t1"])
            end_idx = bisect.bisect_right(starts, comps[j]) if j < len(comps) else len(starts)
            n_after = max(0, end_idx - i)
            carry = tok * n_after * rs.weights["cache_read"] * avgcost
            res.append({"tool": t["name"], "cls": t["cls"], "cmd": redact(t["cmd"] or t["path"] or t["desc"] or "", 110), "tokens": tok,
                        "turns_carried": n_after, "carry_units": round(carry), "ts": t["t0"], "file": S["file"]})
    res.sort(key=lambda x: -x["carry_units"])
    tot_tok = sum(x["tokens"] for x in res)
    return {"count": len(res), "total_tokens": tot_tok, "carry_units": sum(x["carry_units"] for x in res), "top": res[:5],
            "saving_assumption": "truncating/summarising at the source (head, rg -m, jq filters, --quiet) removes about 80% of these tokens"}


def repeated_reads(sessions, rs):
    res = {}
    wasted_tok = 0
    for S in sessions:
        seq = []
        for tid in S["reads"]:
            seq.append((S["tools"][tid]["t0"], "r", tid))
        for tid in S["writes"]:
            seq.append((S["tools"][tid]["t0"], "w", tid))
        seq.sort()
        last = {}
        for ts, kind, tid in seq:
            t = S["tools"][tid]
            key = t["path"]
            if kind == "w":
                last.pop(key, None)
                for k in [k for k in last if k[0] == key]:
                    last.pop(k, None)
                continue
            k = (key, t["rng"])
            if k in last:
                tok = est_tokens(t["chars"], rs.cpt)
                d = res.setdefault(key, {"path": redact(key, 100), "repeats": 0, "tokens": 0})
                d["repeats"] += 1
                d["tokens"] += tok
                wasted_tok += tok
            last[k] = ts
    top = sorted(res.values(), key=lambda d: -d["tokens"])
    return {"files": len(top), "repeat_reads": sum(d["repeats"] for d in top), "wasted_tokens": wasted_tok, "top": top[:5]}


def repeated_commands(sessions, rs):
    groups = defaultdict(list)
    for S in sessions:
        writes = sorted(S["tools"][t]["t0"] for t in S["writes"])
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["name"] != "Bash" or not t["cmd"] or t["cls"] == "poll_wait" or t["t1"] is None:
                continue
            groups[(S["file"], norm_command(t["cmd"]))].append((t, writes))
    out = []
    for (f, key), items in groups.items():
        if len(items) < 3:
            continue
        redundant = 0
        sec = tok = 0.0
        for (a, wr), (b, _) in zip(items, items[1:]):
            i = bisect.bisect_right(wr, a["t0"])
            if i < len(wr) and wr[i] < b["t0"]:
                continue  # an edit happened in between: legitimate re-verification
            if a.get("rhash") != b.get("rhash"):
                continue  # different output: external state changed (log, process), the rerun was informative
            redundant += 1
            sec += b["dur"]
            tok += est_tokens(b["chars"], rs.cpt)
        if redundant >= 2:
            out.append({"cmd": redact(key, 110), "runs": len(items), "redundant": redundant, "seconds": round(sec), "tokens": int(tok), "cls": items[0][0]["cls"]})
    out.sort(key=lambda d: -d["seconds"])
    return {"count": len(out), "redundant_runs": sum(d["redundant"] for d in out), "seconds": sum(d["seconds"] for d in out), "tokens": sum(d["tokens"] for d in out), "top": out[:5]}


def retry_loops(sessions, rs):
    loops = []
    for S in sessions:
        run = []
        def flush():
            if len(run) >= 3 and sum(1 for t in run if t["err"]) >= 2:
                sec = sum(t["dur"] for t in run[1:] if t["t1"])
                loops.append({"cmd": redact(norm_command(run[0]["cmd"] or ""), 110), "attempts": len(run), "failures": sum(1 for t in run if t["err"]),
                              "seconds": round(sec), "tokens": int(sum(est_tokens(t["chars"], rs.cpt) for t in run[1:])), "ts": run[0]["t0"], "file": S["file"]})
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["name"] != "Bash" or not t["cmd"] or t["t1"] is None:
                continue
            key = template_command(t["cmd"])
            if len(key) < 25:
                flush()
                run[:] = []
                continue
            if run and template_command(run[-1]["cmd"]) == key and t["t0"] - run[-1]["t1"] < 900:
                run.append(t)
            else:
                flush()
                run[:] = [t]
        flush()
    loops.sort(key=lambda d: -d["seconds"])
    return {"count": len(loops), "seconds": sum(l["seconds"] for l in loops), "tokens": sum(l["tokens"] for l in loops), "top": loops[:5]}


def polling(sessions, reqs, rs):
    n = sec = 0
    units = tok = 0.0
    top = defaultdict(lambda: [0, 0.0])
    for S in sessions:
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["cls"] != "poll_wait" or t["t1"] is None:
                continue
            n += 1
            d = t["dur"]
            sec += d
            r = reqs.get(t["req"])
            if r:
                u = r["usage"]
                units += r["units"]
                tok += u.get("output_tokens", 0) + u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            k = redact(norm_command(t["cmd"] or t["name"]), 70)
            top[k][0] += 1
            top[k][1] += d
    tl = sorted(top.items(), key=lambda kv: -kv[1][1])[:5]
    return {"polls": n, "seconds": round(sec), "units": round(units), "context_tokens_reprocessed": int(tok), "top": [{"cmd": k, "n": v[0], "seconds": round(v[1])} for k, v in tl]}


def model_fit(chains, rs):
    mech = set(rs.commands["mechanical_classes"])
    items = []
    top_units = tot_units = 0.0
    low_cost = min([c for _, _, c in rs.tiers] or [1.0])
    for S, chain in chains:
        for r in chain:
            tot_units += r["units"]
            if r["tier"] != "top":
                continue
            top_units += r["units"]
            if r["think_ms"] > 0 or not r["tool_ids"] or r["usage"].get("output_tokens", 0) > 400 or r["text_chars"] > 600:
                continue
            tools = [S["tools"][t] for t in r["tool_ids"] if t in S["tools"]]
            if tools and all(t["cls"] in mech for t in tools):
                items.append(r)
    units = sum(r["units"] for r in items)
    topc = max([c for _, _, c in rs.tiers] or [1.0])
    save = units * (1 - low_cost / topc) if topc else 0
    return {"top_tier_units": round(top_units), "total_units": round(tot_units), "mechanical_requests": len(items), "mechanical_units": round(units),
            "potential_saving_units": round(save),
            "assumption": "only if these turns ran on a low-tier model (delegate to a cheaper-model subagent or a script); switching the main model mid-session rebuilds the cache, so prefer delegation"}


def batchable(chains, rs):
    seqs = []
    for S, chain in chains:
        run = []
        for r in chain:
            ok = (len(r["tool_ids"]) == 1 and r["think_ms"] == 0 and r["usage"].get("output_tokens", 0) < 400
                  and S["tools"].get(r["tool_ids"][0], {}).get("cls") in ("file_ops",)
                  and (S["tools"][r["tool_ids"][0]]["name"] in ("Read", "Grep", "Glob", "LS") or S["tools"][r["tool_ids"][0]].get("ro")))
            if ok:
                run.append(r)
            else:
                if len(run) >= 4:
                    seqs.append(list(run))
                run = []
        if len(run) >= 4:
            seqs.append(list(run))
    items = []
    for run in seqs:
        n = len(run)
        units = sum(r["units"] for r in run)
        sec = sum(r["end"] - r["start"] for r in run)
        items.append({"turns": n, "units": round(units), "seconds": round(sec), "saving_units": round(units * (n - 1) / n), "saving_seconds": round(sec * (n - 1) / n)})
    items.sort(key=lambda d: -d["saving_units"])
    return {"sequences": len(items), "turns": sum(i["turns"] for i in items), "saving_units": sum(i["saving_units"] for i in items),
            "saving_seconds": sum(i["saving_seconds"] for i in items), "top": items[:3],
            "assumption": "N independent read-only calls issued one per turn could be one parallel batch (or one script): saves (N-1)/N of those turns"}


def templated_loops(sessions, reqs, rs):
    groups = defaultdict(list)
    for S in sessions:
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["name"] == "Bash" and t["cmd"] and t["cls"] not in ("poll_wait",):
                groups[(S["file"], template_command(t["cmd"]))].append(t)
    items = []
    for (f, key), ts in groups.items():
        rids = {t["req"] for t in ts}
        if len(key) < 25 or len(ts) < 6 or len(rids) < 6:
            continue
        rr = [reqs[i] for i in rids if i in reqs]
        if not rr:
            continue
        units = sum(r["units"] for r in rr)
        sec = sum(r["end"] - r["start"] for r in rr)
        n = len(rr)
        items.append({"template": redact(key, 100), "turns": n, "units": round(units), "saving_units": round(units * (n - 1) / n), "saving_seconds": round(sec * (n - 1) / n)})
    items.sort(key=lambda d: -d["saving_units"])
    return {"count": len(items), "saving_units": sum(i["saving_units"] for i in items), "saving_seconds": sum(i["saving_seconds"] for i in items), "top": items[:3],
            "assumption": "the same command shape run in 6+ separate turns is a loop; one script or one for-loop call does it in 1 turn"}


def fan_out(sessions, reqs, rs):
    by_file = {S["file"]: S for S in sessions}
    spawns = []
    for S in sessions:
        if S["is_sub"]:
            continue
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["cls"] != "subagent_wait" or not t.get("agent_id"):
                continue
            sub = next((x for x in sessions if x["is_sub"] and x["session_id"] == "agent-" + str(t["agent_id"])), None)
            if not sub:
                continue
            rr = [reqs[r["id"]] for r in sub["req_list"] if r["id"] in reqs]
            if not rr:
                continue
            units = sum(r["units"] for r in rr)
            tok = sum(r["usage"].get("output_tokens", 0) + r["usage"].get("input_tokens", 0) + r["usage"].get("cache_creation_input_tokens", 0) for r in rr)
            cold = rr[0]["usage"].get("cache_creation_input_tokens", 0) * rs.weights["cache_write_5m"] * rr[0]["cost"]
            spawns.append({"small": len(rr) <= 3, "desc": redact(t["desc"] or "", 60), "units": round(units), "fresh_tokens": int(tok), "requests": len(rr), "model": rr[0].get("model"),
                           "seconds": round((sub["last"] or 0) - (sub["first"] or 0)), "cold_start_units": round(cold)})
    spawns.sort(key=lambda d: -d["units"])
    n = len(spawns)
    return {"spawns": n, "units": sum(s["units"] for s in spawns), "cold_start_units": sum(s["cold_start_units"] for s in spawns),
            "small_spawns": sum(1 for s in spawns if s["small"]), "small_cold_start_units": sum(s["cold_start_units"] for s in spawns if s["small"]), "top": spawns[:5]}


def screenshots(sessions, rs):
    shots = snaps = imgs = 0
    for S in sessions:
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            nm = t["name"].lower()
            if "screenshot" in nm:
                shots += 1
            elif "snapshot" in nm:
                snaps += 1
            imgs += t["imgs"]
    return {"screenshot_calls": shots, "snapshot_calls": snaps, "images_returned": imgs, "image_tokens_estimate": imgs * rs.image_tokens}


def analyze_quota(sessions, rs):
    reqs, chains = collect_requests(sessions, rs)
    total_units = sum(r["units"] for r in reqs.values())
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    for r in reqs.values():
        u = r["usage"]
        tot["input"] += u.get("input_tokens", 0)
        tot["output"] += u.get("output_tokens", 0)
        tot["cache_read"] += u.get("cache_read_input_tokens", 0)
        tot["cache_create"] += u.get("cache_creation_input_tokens", 0)
    denom = tot["input"] + tot["cache_read"] + tot["cache_create"]
    return {
        "requests": len(reqs), "total_units": round(total_units), "tokens": tot,
        "cache_hit_rate": round(tot["cache_read"] / denom, 3) if denom else None,
        "by_model": tokens_by_model(reqs),
        "cache_misses": cache_misses(chains, rs, None),
        "oversized_results": oversized(chains, sessions, rs),
        "repeated_reads": repeated_reads(sessions, rs),
        "repeated_commands": repeated_commands(sessions, rs),
        "retry_loops": retry_loops(sessions, rs),
        "polling": polling(sessions, reqs, rs),
        "model_fit": model_fit(chains, rs),
        "batchable_turns": batchable(chains, rs),
        "templated_loops": templated_loops(sessions, reqs, rs),
        "subagent_fan_out": fan_out(sessions, reqs, rs),
        "screenshots": screenshots(sessions, rs),
        "avg_fresh_tokens_per_request": round((tot["input"] + tot["output"] + tot["cache_create"]) / len(reqs)) if reqs else 0,
    }
