"""Evaluate data-driven stack-waste rules against measured tool calls.

A rule fires only with evidence: matched events must reach evidence.min_count and
evidence.min_seconds. Savings come with their arithmetic spelled out.
"""
import re

from . import repo
from .classify import classify_bash
from .util import est_tokens, fmt_dur, match_text, norm_command, raw_text, redact, sanitize_command, scrub

_PART_SEP = re.compile(r"&&|\|\||[;|\n]")


def _fmt_tok(n):
    return "%d" % round(n)


def _example_part(rule, cmd, match, rs=None):
    """The part of a chained command the rule matched ('npx vitest run ...', not the 'pid=$(ss -ltnp ...)' that
    opens the chain), raw (numbers kept, secrets scrubbed) and without wrappers (env, nice, my-heavy ... --)."""
    for part in _PART_SEP.split(raw_text(cmd or "")):
        if part.strip() and rule["_rx"].search(match_text(part)):
            part = part.strip()
            if rs is not None:
                from .drill import _clean_part, _seg_cfg
                part = _clean_part(part, _seg_cfg(rs)["_wrappers"]) or part
            return part
    m = rule["_rx"].search(match)
    if not m:
        return cmd or ""
    seps = [x.end() for x in _PART_SEP.finditer(match, 0, m.start())]
    return match[seps[-1] if seps else 0:].strip()


def _events_command(rule, sessions, rs=None, skipped=None):
    ev = []
    d = rule["detector"]
    fb_n = (d.get("followed_by") or {}).get("within_commands", 5)
    for S in sessions:
        ctx = None
        bash = [S["tools"][t] for t in S["tool_order"] if S["tools"][t]["name"] == "Bash" and S["tools"][t]["cmd"]]
        sane = [sanitize_command(t["cmd"]) for t in bash]
        match = [match_text(t["cmd"]) for t in bash]
        if rule["_ctx"] is not None or rule["_abs"] is not None:
            ctx = "\n".join(sane) + "\n" + "\n".join(S["ctx_blob"])
        if rule["_ctx"] is not None and not rule["_ctx"].search(ctx):
            continue
        if rule["_abs"] is not None and rule["_abs"].search(ctx):
            continue
        for i, (t, s) in enumerate(zip(bash, sane)):
            if t["t1"] is None or not rule["_rx"].search(match[i]):
                continue
            if rule["_ex"] is not None and rule["_ex"].search(s):
                continue
            tgt = t
            if rule["_fb"] is not None:
                tgt = None
                for j in range(i + 1, min(len(bash), i + 1 + fb_n)):
                    if bash[j]["t1"] is not None and rule["_fb"].search(match[j]):
                        tgt = bash[j]
                        break
                if tgt is None:
                    continue
            sec = tgt["dur"]
            if rs is not None and rule["_fb"] is None:
                # compound command: the matched fragment must be what the command is (by class),
                # otherwise the wall time belongs to something else in the same command line
                m = rule["_rx"].search(match[i])
                if m and classify_bash(m.group(0), rs) != tgt["cls"]:
                    sec = min(sec, 2.0)
            if d.get("skip_if_project"):
                # the project on disk already does what the rule advises (config, artifact, version): not a finding
                why = repo.already_done(rule, t["cmd"], t.get("cwd") or S.get("cwd"))
                if why:
                    if skipped is not None:
                        k = skipped.setdefault(rule["id"], {"id": rule["id"], "events": 0, "seconds": 0.0, "reasons": {}})
                        k["events"] += 1
                        k["seconds"] = round(k["seconds"] + sec, 1)
                        k["reasons"][why] = k["reasons"].get(why, 0) + 1
                    continue
            ev.append({"tid": tgt["id"], "t": tgt, "sec": sec, "tok": est_tokens(tgt["chars"]), "sid": S["session_id"], "cmd": t["cmd"],
                       "ctx": ctx, "ex": _example_part(rule, t["cmd"], match[i], rs)})
    return ev


def _events_result(rule, sessions):
    ev = []
    for S in sessions:
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if rule["detector"].get("require_error") and not t["err"]:
                continue
            if t["t1"] is not None and rule["id"] in t.get("res_hits", ()):
                ev.append({"tid": tid, "t": t, "sec": t["dur"], "tok": est_tokens(t["chars"]), "sid": S["session_id"], "cmd": t["cmd"] or t["name"], "ctx": None})
    return ev


def _events_toolname(rule, sessions):
    rx = re.compile(rule["detector"].get("tool_regex") or rule["detector"]["regex"], re.I)
    ev = []
    for S in sessions:
        for tid in S["tool_order"]:
            t = S["tools"][tid]
            if t["t1"] is not None and rx.search(t["name"]):
                ev.append({"tid": tid, "t": t, "sec": t["dur"], "tok": est_tokens(t["chars"]), "sid": S["session_id"], "cmd": t["name"], "ctx": None})
    return ev


def _events_overlap(rule, overlaps):
    d = rule["detector"]
    want = set(d.get("classes", []))
    mo = d.get("min_overlap_seconds", 20)
    ev = []
    for o in overlaps:
        if o["seconds"] >= mo and set(o["classes"]) <= want:
            ev.append({"tid": "ov%d" % len(ev), "t": None, "sec": o["seconds"], "tok": 0, "sid": o.get("sid", ""), "cmd": "+".join(o["classes"]) + " overlap", "ctx": None})
    return ev


def evaluate(sessions, rs, overlaps, avg_turn_tokens, group_of=None, skipped=None):
    """group_of: transcript session id -> main session id, so 'sessions' counts main sessions the way the view's
    items do; 'transcripts' counts every transcript (subagents included). skipped (a dict, filled in): per rule
    id, the matched calls left out because the project already does what the rule advises (detector.skip_if_project)."""
    group_of = group_of or {}
    findings = []
    claimed = {}  # tool id -> fraction of its time already credited to an earlier rule (no double counting)
    for rule in rs.stack:
        d = rule["detector"]
        typ = d.get("type", "command")
        if typ == "signal_overlap":
            ev = _events_overlap(rule, overlaps)
        elif typ == "tool_name":
            ev = _events_toolname(rule, sessions)
        elif d.get("match_target") == "result":
            ev = _events_result(rule, sessions)
        else:
            ev = _events_command(rule, sessions, rs, skipped)
        count = len(ev)
        sec = sum(e["sec"] for e in ev)
        unit = {"signal_overlap": "overlaps", "tool_name": "calls"}.get(typ, "runs")
        ev_t = rule.get("evidence", {})
        if count < ev_t.get("min_count", 1) or sec < ev_t.get("min_seconds", 0) or count == 0:
            continue
        est = rule.get("estimate", {})
        n_eff = max(0, count - 1) if est.get("events_minus_one") else count
        frac = est.get("saving_fraction", 0.0)
        spe = est.get("seconds_per_event", 0.0)
        free = [1.0 - claimed.get(e["tid"], 0.0) for e in ev]
        eff_sec = sum(e["sec"] * f for e, f in zip(ev, free))
        res_tok = sum(e["tok"] * f for e, f in zip(ev, free))
        for e, f in zip(ev, free):
            claimed[e["tid"]] = 1.0 - (1.0 - claimed.get(e["tid"], 0.0)) * (1.0 - frac)
        s_saved = eff_sec * frac + n_eff * spe
        t_saved = res_tok * est.get("token_fraction", 0.0) + n_eff * est.get("event_fraction", 0.0) * (avg_turn_tokens if not est.get("token_per_event") else est["token_per_event"])
        parts = []
        if frac:
            shared = "" if abs(eff_sec - sec) < 0.5 else " (%s not already credited to earlier rules)" % fmt_dur(eff_sec)
            parts.append("%d %s, %s measured%s x %.2f = %s" % (count, unit, fmt_dur(sec), shared, frac, fmt_dur(eff_sec * frac)))
        if spe:
            parts.append("%d x %ss = %s" % (n_eff, spe, fmt_dur(n_eff * spe)))
        tparts = []
        if est.get("token_fraction"):
            tparts.append("%s result tokens x %.2f" % (_fmt_tok(res_tok), est["token_fraction"]))
        if est.get("event_fraction"):
            per = est.get("token_per_event") or avg_turn_tokens
            tparts.append("%d %s x %.2f x %s tok/turn" % (n_eff, unit, est["event_fraction"], _fmt_tok(per)))
        conf = rule.get("confidence", "low")
        for brx, bconf in rule.get("_boost", []):
            if brx is not None and any(e["ctx"] and brx.search(e["ctx"]) for e in ev[:20]):
                conf = bconf
        ex = sorted(ev, key=lambda e: -e["sec"])[:3]
        findings.append({
            "id": rule["id"], "stack": rule.get("stack"), "title": rule.get("title"), "kind": "stack_rule",
            "events": count, "unit": unit, "measured_seconds": round(sec, 1),
            "sessions": len({group_of.get(e["sid"], e["sid"]) for e in ev}), "transcripts": len({e["sid"] for e in ev}),
            "examples": [{"cmd": redact(norm_command(e.get("ex") or e["cmd"] or "", 110), 110),
                          "raw": scrub(re.sub(r"\s+", " ", e.get("ex") or e["cmd"] or ""), 110), "seconds": round(e["sec"], 1)} for e in ex],
            "saving_seconds": round(s_saved, 1), "saving_tokens": int(t_saved),
            "arithmetic": {"time": " + ".join(parts) or "n/a", "tokens": " + ".join(tparts) or "n/a", "assumption": est.get("assumption", "")},
            "confidence": conf, "why": rule.get("why"), "fix": rule.get("fix"), "before": rule.get("before"), "after": rule.get("after"),
            "ref": rule.get("source"),
        })
    return findings
