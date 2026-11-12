"""Ledger of every tool call and model turn: how long it lasted and what it likely did.

What it did comes from the description the agent gave the call when there is one, otherwise from
rules/intents.json. Durations overlap when tools or subagents run in parallel, so they do not sum to
wall-clock time; `analyze` is the view that does.
"""
import csv
import json
import os
import re
import statistics
from collections import defaultdict

from .core import claim_order, encode_dir, find_groups
from .parse import parse_session
from .util import fmt_dur, iso, match_text, redact, scrub, strip_cd, template_command  # noqa: F401 (scrub re-exported)

HERE = os.path.dirname(os.path.abspath(__file__))
STALL_S = 1800.0
def load_intents(rules_dir=None):
    path = os.path.join(rules_dir or os.path.join(HERE, "..", "..", "rules"), "intents.json")
    with open(path, encoding="utf-8") as f:
        return [(re.compile(r["regex"]), r["text"]) for r in json.load(f)["rules"]]


def infer_command(cmd, cls, intents):
    if cls == "poll_wait":
        return "wait or poll until something finishes"
    s = strip_cd(match_text(cmd))
    hits, seen = [], set()
    used_spans = []
    for rx, text in intents:  # table order is specific to general: a general rule cannot claim a span already explained
        m = rx.search(s)
        if m and text not in seen and not any(m.start() < e and b < m.end() for b, e in used_spans):
            hits.append((m.start(), text))
            seen.add(text)
            used_spans.append(m.span())
    if hits:
        return " + ".join(t for _, t in sorted(hits)[:3])
    words = s.split()
    return "run %s" % os.path.basename(words[0]) if words else "shell command"


def infer_tool(t):
    name, base = t["name"], os.path.basename(t.get("path") or "")
    target = t.get("target") or ""
    fixed = {"Read": "read file %s", "Write": "write file %s", "Edit": "edit file %s", "MultiEdit": "edit file %s",
             "NotebookEdit": "edit notebook %s"}
    if name in fixed:
        return fixed[name] % (base or "?")
    if name == "Grep":
        return "search code for %s" % target
    if name == "Glob":
        return "find files matching %s" % target
    if name == "WebFetch":
        return "fetch %s" % target
    if name == "WebSearch":
        return "web search: %s" % target
    if name == "Skill":
        return "load skill %s" % target
    if name in ("Agent", "Task", "Workflow"):
        return "run a subagent" if name != "Workflow" else "run a workflow of subagents"
    if name == "ToolSearch":
        return "load tool definitions"
    if name in ("AskUserQuestion", "ExitPlanMode"):
        return "wait for the user's answer"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        server = re.sub(r"^claude_ai_", "", parts[1] if len(parts) > 1 else "?").replace("_", " ")
        tool = parts[2].replace("_", " ") if len(parts) > 2 else ""
        return "%s: %s" % (server, tool) + (" (%s)" % target if target else "")
    return name


def project_label(encoded):
    home = encode_dir(os.path.expanduser("~"))
    s = encoded[len(home):] if encoded.startswith(home) else encoded
    return re.sub(r"^-(git-)?", "", s) or encoded


def _sub_label(path):
    try:
        with open(path[:-6] + ".meta.json", encoding="utf-8") as f:
            return json.load(f).get("description") or "subagent"
    except (OSError, ValueError):
        return "subagent"


def rows_for_session(S, project, source, rs, intents):
    sid = S["session_id"] if not S["is_sub"] else os.path.basename(os.path.dirname(os.path.dirname(S["file"])))
    out = []
    for r in S["req_list"]:
        dur = r["end"] - r["start"]
        if dur <= 0:
            continue
        toks = r["usage"].get("output_tokens", 0)
        what = "model turn: %d output tokens" % toks
        if r["think_ms"]:
            what += ", %s thinking" % fmt_dur(r["think_ms"] / 1000.0)
        out.append({"start": r["start"], "dur": dur, "project": project, "session": sid[:8], "source": source,
                    "tool": "(model) " + (r["model"] or "unknown"), "cls": "inference", "what": what, "said": False,
                    "detail": "", "exit": None, "flags": ["api error"] if r.get("api_error") else [], "out": r["text_chars"],
                    "key": "(model) " + (r["model"] or "unknown")})
    for tid in S["tool_order"]:
        t = S["tools"][tid]
        if t["t1"] is None or t.get("dur") is None:
            continue
        cmd = t.get("cmd")
        desc = (t.get("desc") or "").strip()
        inferred = infer_command(cmd, t["cls"], intents) if cmd else infer_tool(t)
        flags = []
        if t["bg"]:
            flags.append("background")
        if t["timed_out"]:
            flags.append("timed out")
        if t["interrupted"]:
            flags.append("interrupted")
        if t["err"]:
            flags.append("failed")
        if t["cls"].startswith("human_wait"):
            flags.append("waits for a person")
        if t["dur"] > STALL_S and not t["bg"] and t["cls"] != "subagent_wait":
            flags.append("possibly suspended")
        key = t["name"] + " " + template_command(cmd, 90) if cmd else t["name"]
        out.append({"start": t["t0"], "dur": t["dur"], "project": project, "session": sid[:8], "source": source,
                    "tool": t["name"], "cls": t["cls"], "what": scrub(desc or inferred, 200), "said": bool(desc),
                    "detail": scrub(cmd or t.get("path") or t.get("target") or "", 500), "exit": t["exit"], "flags": flags,
                    "out": t["chars"], "key": scrub(key), "inferred": scrub(inferred, 200)})
    return out


GENERIC = {"Read": "read files", "Write": "write files", "Edit": "edit files", "MultiEdit": "edit files", "Grep": "search code", "Glob": "find files",
           "WebSearch": "web searches", "WebFetch": "fetch web pages", "Skill": "load skills"}


def groups_of(rows):
    g = defaultdict(list)
    for r in rows:
        g[r["key"]].append(r)
    res = []
    for key, rr in g.items():
        ds = [r["dur"] for r in rr]
        # a group is named by what the rule table says, since agent descriptions differ call by call
        names = defaultdict(int)
        for r in rr:
            names[r.get("inferred") or r["what"]] += 1
        what = max(names, key=names.get)
        if rr[0]["cls"] == "inference":
            what = "model generation"
        elif rr[0]["tool"] in GENERIC:
            what = GENERIC[rr[0]["tool"]]
        elif rr[0]["tool"].startswith("mcp__"):
            what = what.split(" (")[0]
        res.append({"key": key, "tool": rr[0]["tool"], "cls": rr[0]["cls"], "what": what, "n": len(rr), "total": sum(ds),
                    "median": statistics.median(ds), "max": max(ds), "fails": sum(1 for r in rr if "failed" in r["flags"])})
    res.sort(key=lambda x: -x["total"])
    return res


def collect(args, rs):
    intents = load_intents(args.rules_dir)
    rows, n_sessions = [], 0
    win = (args.since_ts, args.until_ts)
    groups = find_groups(project=args.project, all_=args.all, session=args.session, since=args.since_ts)
    seen = set()  # records copied into forked/resumed sessions are listed once
    for g in (groups[i] for i in claim_order(groups)):
        project = project_label(g["project"])
        S = parse_session(g["main"], rs, win, seen=seen)
        if S["first"] is None:
            continue
        n_sessions += 1
        rows += rows_for_session(S, project, "main", rs, intents)
        for p in g["subs"]:
            C = parse_session(p, rs, win, is_sub=True)
            if C["first"] is not None:
                rows += rows_for_session(C, project, scrub(_sub_label(p), 70), rs, intents)
    rows.sort(key=lambda r: -r["dur"])
    return rows, n_sessions


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["start", "seconds", "project", "session", "source", "tool", "class", "what_it_did", "what_source", "command_or_target", "exit", "flags", "output_chars"])
        for r in rows:
            w.writerow([iso(r["start"]), round(r["dur"], 1), r["project"], r["session"], r["source"], r["tool"], r["cls"], r["what"],
                        "agent" if r["said"] else "inferred", r["detail"], r["exit"], ";".join(r["flags"]), r["out"]])


def page_payload(rows, groups, n_sessions, max_rows):
    shown = rows[:max_rows]
    srcs = sorted({r["source"] for r in shown})
    return {
        "sessions": n_sessions, "total_calls": len(rows), "shown": len(shown),
        "min_shown": round(shown[-1]["dur"], 1) if shown else 0,
        "from": iso(min(r["start"] for r in rows))[:10], "to": iso(max(r["start"] for r in rows))[:10],
        "sources": srcs,
        "calls": [[iso(r["start"])[:16].replace("T", " "), round(r["dur"], 1), r["project"], r["session"], srcs.index(r["source"]), r["tool"], r["cls"],
                   r["what"], 1 if r["said"] else 0, r["detail"], r["exit"], r["flags"], r["out"]] for r in shown],
        "groups": [[g["key"], g["tool"], g["cls"], g["what"], g["n"], round(g["total"], 1), round(g["median"], 1), round(g["max"], 1), g["fails"]] for g in groups[:3000]],
        "groups_total": len(groups),
    }


def write_html(payload, path):
    with open(os.path.join(HERE, "calls_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(tpl.replace("__DATA__", blob))


def run(args, rs, prefix, max_rows):
    rows, n = collect(args, rs)
    if not rows:
        return {"rows": []}
    write_csv(rows, prefix + ".csv")
    groups = groups_of(rows)
    write_html(page_payload(rows, groups, n, max_rows), prefix + ".html")
    return {"rows": rows, "groups": groups, "sessions": n}


def render_md(data, top):
    top = max(top, 15)
    L = ["# Calls: %d across %d sessions" % (len(data["rows"]), data["sessions"]), "",
         "Durations overlap when work ran in parallel.", "", "## Longest calls", "", "| Time | Tool | What it did | Flags |", "|---|---|---|---|"]
    for r in data["rows"][:top]:
        L.append("| %s | %s | %s | %s |" % (fmt_dur(r["dur"]), r["tool"], r["what"].replace("|", "/"), ", ".join(r["flags"])))
    L += ["", "## Most total time by command shape", "", "| Total | Calls | Median | Tool | What it did |", "|---|---|---|---|---|"]
    for g in data["groups"][:top]:
        L.append("| %s | %d | %s | %s | %s |" % (fmt_dur(g["total"]), g["n"], fmt_dur(g["median"]), g["tool"], g["what"].replace("|", "/")))
    return "\n".join(L) + "\n"
