"""Stream one transcript file (line by line) into a compact session dict.

Tolerant by design: unknown record types, missing fields and version drift are skipped,
never fatal. Nothing but small aggregates is kept in memory (no raw tool output).
"""
import json
import os
import re
import zlib
from collections import Counter

from .classify import classify_tool, is_read_only_bash
from .util import match_text, parse_ts, raw_text, redact

SCAN_HEAD, SCAN_TAIL = 6000, 3000
SKIP_PREFIX = ('{"type":"api-request',)
API_SEND_PREFIX = '{"type":"api-request",'  # one per request sent: its timestamp is the send time
_TS_FIELD = re.compile(r'"timestamp":"([^"]+)"')
_QSRC_FIELD = re.compile(r'"querySource":"([^"]+)"')
_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "min": 60.0}
_NOTIF_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
_NOTIF_TASK = re.compile(r"<task-id>([^<]+)</task-id>")
_NOTIF_STATUS = re.compile(r"<status>([^<]+)</status>")
_NOTIF_BLOCK = re.compile(r"<task-notification>(.*?)(?:</task-notification>|$)", re.S)
_END_STATUS = ("completed", "failed", "killed", "stopped", "cancelled", "error")
# PIDs a command printed ('launched PID 4242', 'pid=123', '[1] 4567'): a later 'kill -0 <pid>' poll names what it waits for
_PID_OUT = re.compile(r"(?i)\bpid\b\s*[:=#]?\s*(\d{2,8})\b|^\[\d+\]\s+(\d{2,8})\s*$", re.M)
_TASK_OUT = re.compile(r"/tasks/([A-Za-z0-9_-]+)\.output\b")
_EXIT = re.compile(r"^Exit code (\d+)")
MACHINE_PREFIX = ("<task-notification", "<system-reminder", "<local-command", "<command-name",
                  "<command-message", "This session is being continued", "Base directory", "Caveat:")
SCAN_TOOLS_SKIP = {"Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit", "LS", "ToolSearch", "Skill"}


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    return ""


def _result_stats(content):
    """-> (chars, n_images)"""
    if isinstance(content, str):
        return len(content), 0
    chars = imgs = 0
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "image":
                imgs += 1
            elif t == "text":
                chars += len(b.get("text") or "")
            elif t == "tool_reference":
                chars += 60
    return chars, imgs


_DUMP_CMD = re.compile(r"(^|;|&&)\s*(cat|sed|head|tail|grep|egrep|rg|less|awk|git\s+(show|log|diff)|jq)\b")


def scan_signals(rs, text, tool, cmd, is_error):
    """Regex-scan a bounded slice of a tool result. Returns list of (id, confidence, snippet)."""
    if not text:
        return []
    if len(text) > SCAN_HEAD + SCAN_TAIL:
        text = text[:SCAN_HEAD] + "\n" + text[-SCAN_TAIL:]
    found = []
    for s in rs.signals:
        rx = s.get("_rx")
        if rx is None:
            continue
        m = rx.search(text)
        if m:
            conf = s.get("confidence_error", "medium") if is_error else s.get("confidence_ok", "low")
            if cmd and _DUMP_CMD.search(match_text(cmd)):
                conf = "low"  # the command prints file/log/diff content: the hit may be text, not a failure
            a = max(0, m.start() - 40)
            snippet = re.sub(r"\s+", " ", text[a:m.end() + 60])[:140]
            found.append((s["id"], conf, snippet))
    return found


def scan_measures(S, rs, text, t, ts):
    """Durations the tool printed about itself (rules/measures.json). Bounded slice, at most 5 hits per rule."""
    if len(text) > SCAN_HEAD + SCAN_TAIL:
        text = text[:SCAN_HEAD] + "\n" + text[-SCAN_TAIL:]
    for m in rs.measures:
        rx = m.get("_rx")
        if rx is None:
            continue
        for i, hit in enumerate(rx.finditer(text)):
            if i >= 5:
                break
            g = hit.groupdict()
            try:
                v = float(g.get("v"))
            except (TypeError, ValueError):
                continue
            sec = v * _UNIT_S.get(g.get("u") or m.get("unit", "s"), 1.0) + 60.0 * float(g.get("m") or 0)
            S["measures"].append({"id": m["id"], "kind": m.get("kind", "timing"), "label": m.get("label", m["id"]), "sec": sec, "ts": ts, "cls": t["cls"], "tid": t["id"]})


def is_human_prompt(d, content, is_sub):
    if is_sub:
        return False
    org = d.get("origin")
    kind = org.get("kind") if isinstance(org, dict) else None
    if kind:
        return kind == "human"
    to = d.get("turnOrigin")
    if to == "human":
        return True
    if to in ("task_notification", "sdk"):
        return False
    text = _text_of(content).lstrip()
    if not text:
        return False
    return not text.startswith(MACHINE_PREFIX)


CMD_KEEP = 700
QUOTE_KEEP, QUOTE_LONG = 20, 60  # a quoted argument longer than QUOTE_LONG keeps its first QUOTE_KEEP chars
_SEPS = ("&&", "||", ";", "|", "&", "\n")


def _scan(cmd):
    """Quote-aware scan -> (quoted spans [(start, end)], top-level separator positions)."""
    spans, seps, q, start, i, n = [], [], None, 0, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            if c == "\\" and q == '"':
                i += 2
                continue
            if c == q:
                spans.append((start, i + 1))
                q = None
        elif c in ("'", '"'):
            q, start = c, i
        elif c == "\\":
            i += 2
            continue
        else:
            op = next((o for o in _SEPS if cmd.startswith(o, i)), None)
            if op and not (op == "&" and (cmd[i - 1:i] in (">", "<") or cmd[i + 1:i + 2] == ">")) and not (op == "|" and cmd[i - 1:i] == ">"):
                seps.append(i)
                i += len(op)
                continue
        i += 1
    return spans, seps


def _keep_cmd(cmd):
    """Stored command text (<= 700 chars). A long command loses data first, so the command that runs at its end
    ('sed -i '<long script>' f; echo '<file body>' > g; npx vitest run g') survives the cut: heredoc bodies,
    then long quoted arguments (shortened to their first 20 chars), then whole middle parts (cut at a
    top-level ';', '&&', '|' ... and marked ' … ')."""
    if not cmd:
        return None
    if len(cmd) > CMD_KEEP and "<<" in cmd:
        short = raw_text(cmd)
        if len(short) < len(cmd):
            cmd = short
    if len(cmd) > CMD_KEEP:
        spans, _ = _scan(cmd)
        out, prev = [], 0
        for a, b in spans:
            if b - a > QUOTE_LONG:
                out.append(cmd[prev:a + 1 + QUOTE_KEEP] + "…" + cmd[a])
                prev = b
        cmd = "".join(out) + cmd[prev:]
    if len(cmd) > CMD_KEEP:
        _, seps = _scan(cmd)
        head = max((p for p in seps if 0 < p <= CMD_KEEP // 2), default=None)
        if head is not None:
            room = CMD_KEEP - head - 2
            tail = min((p for p in seps if p > head and len(cmd) - p <= room), default=None)
            if tail is not None:
                cmd = cmd[:head] + " …" + cmd[tail:]
    return cmd[:CMD_KEEP]


def parse_session(path, rs, win=(None, None), is_sub=False, seen=None):
    """seen: optional set of record uuids shared across files; a record whose uuid is already in it was copied
    from another transcript (forked or resumed session, forked subagent) and is skipped, so its time is counted
    once. An assistant record whose request id another file recorded first is skipped too (same API request)."""
    since, until = win
    S = {
        "file": path, "is_sub": is_sub,
        "session_id": os.path.basename(path).replace(".jsonl", ""),
        "first": None, "last": None,
        "requests": {}, "tools": {}, "tool_order": [], "human_ts": [], "away_ts": [], "compactions": [],
        "signals": [], "api_errors": 0, "api_error_texts": Counter(), "cost_state": None, "turn_ms": [],
        "perm_modes": Counter(), "cwd": None, "version": None, "models": Counter(), "efforts": Counter(),
        "reads": [], "writes": [], "records": 0, "bad_lines": 0, "images": 0, "system_subtypes": Counter(),
        "ctx_blob": [], "meta": None, "api_sends": [], "hooks": [], "measures": [], "dup_records": 0,
        "task_ids": {},
    }
    last_user = None
    own_rids = set()  # request ids this file recorded first (seen: also ('req', id) of every file so far)
    reqs, tools = S["requests"], S["tools"]
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return S
    with f:
        for line in f:
            if line.startswith(SKIP_PREFIX) or len(line) < 8:
                if line.startswith(API_SEND_PREFIX):
                    head = line[:600]
                    m = _TS_FIELD.search(head)
                    ts = parse_ts(m.group(1)) if m else None
                    if ts is not None and (since is None or ts >= since) and (until is None or ts <= until):
                        q = _QSRC_FIELD.search(head)
                        S["api_sends"].append((ts, q.group(1) if q else ""))
                continue
            try:
                d = json.loads(line)
            except ValueError:
                S["bad_lines"] += 1
                continue
            if not isinstance(d, dict):
                continue
            typ = d.get("type")
            if typ == "api-request":  # same record as the fast path above, written with other JSON spacing
                ts = parse_ts(d.get("timestamp"))
                if ts is not None and (since is None or ts >= since) and (until is None or ts <= until):
                    S["api_sends"].append((ts, str(d.get("querySource") or "")))
                continue
            if typ == "cost-state":
                S["cost_state"] = {k: d.get(k) for k in ("totalCostUSD", "totalAPIDuration", "totalToolDuration", "totalDuration", "totalLinesAdded", "totalLinesRemoved", "modelUsage")}
                continue
            if typ in ("attachment", "queue-operation"):
                # completion notices of background commands and async agents: in subagent transcripts they only arrive
                # as attachments, in main sessions the queue records them when they arrive (the user record that
                # carries them may come much later, after the turn ends)
                if "task-notification" in line:
                    ts = parse_ts(d.get("timestamp"))
                    a = d.get("attachment") if isinstance(d.get("attachment"), dict) else {}
                    text = (a.get("prompt") if a.get("type") == "queued_command" else None) if typ == "attachment" else \
                        (d.get("content") if d.get("operation") == "enqueue" else None)
                    if ts is not None and isinstance(text, str):
                        _notifications(S, text, ts)
                continue
            if typ not in ("assistant", "user", "system"):
                continue  # other attachments, future record types: ignored
            if seen is not None:
                u = d.get("uuid")
                if u:
                    if u in seen:
                        S["dup_records"] += 1
                        continue
                    seen.add(u)
                rq = d.get("requestId") if typ == "assistant" else None
                if rq:
                    # a fork re-records the parent agent's spawning message under a new uuid: same API request
                    if ("req", rq) in seen and rq not in own_rids:
                        S["dup_records"] += 1
                        continue
                    seen.add(("req", rq))
                    own_rids.add(rq)
            ts = parse_ts(d.get("timestamp"))
            if ts is None:
                continue
            if typ == "user" and "task-notification" in line:
                m0 = d.get("message") if isinstance(d.get("message"), dict) else {}
                c0 = m0.get("content")
                if not (isinstance(c0, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c0)):
                    _notifications(S, _text_of(c0), ts)  # a notice, not a tool output quoting one; also after the window
            if (since is not None and ts < since) or (until is not None and ts > until):
                continue
            S["records"] += 1
            if S["first"] is None or ts < S["first"]:
                S["first"] = ts
            if S["last"] is None or ts > S["last"]:
                S["last"] = ts
            if not S["cwd"] and d.get("cwd"):
                S["cwd"] = d.get("cwd")
            if d.get("version"):
                S["version"] = d.get("version")
            if typ == "assistant":
                _assistant(S, rs, d, ts, last_user)
            elif typ == "user":
                last_user = max(ts, last_user) if last_user else ts
                _user(S, rs, d, ts, is_sub)
            elif typ == "system":
                st = d.get("subtype")
                S["system_subtypes"][str(st)] += 1
                if st in ("bridge_status", "away_summary"):
                    S["away_ts"].append(ts)  # session parked / user away: the gap before it is not autonomous time
                if st == "compact_boundary":
                    md = d.get("compactMetadata") or {}
                    S["compactions"].append({"ts": ts, "pre_tokens": md.get("preTokens"), "dur_ms": md.get("durationMs"), "trigger": md.get("trigger")})
                elif st == "stop_hook_summary":
                    infos = d.get("hookInfos") if isinstance(d.get("hookInfos"), list) else []
                    ms = [h.get("durationMs") for h in infos if isinstance(h, dict) and isinstance(h.get("durationMs"), (int, float))]
                    S["hooks"].append({"ts": ts, "n": len(infos), "ms_sum": sum(ms), "ms_max": max(ms) if ms else 0,
                                       "errors": len(d.get("hookErrors") or []), "prevented": bool(d.get("preventedContinuation"))})
                elif st == "turn_duration" and isinstance(d.get("durationMs"), (int, float)):
                    S["turn_ms"].append(d["durationMs"])
                elif st and ("error" in str(st) or "retry" in str(st)):
                    S["api_errors"] += 1
                    txt = d.get("content") if isinstance(d.get("content"), str) else ""
                    for sid, conf, sn in scan_signals(rs, txt, "system", "", True):
                        S["signals"].append({"id": sid, "conf": conf, "ts": ts, "tool": "system", "cls": "api", "snippet": sn, "sec": 0.0})
    # close: requests as ordered list
    S["req_list"] = sorted(reqs.values(), key=lambda r: r["start"])
    for t in tools.values():
        # effective duration: backgrounded commands run until their completion notification
        if t["t1"] is None:
            t["dur"] = None
        elif t["bg"] and t["bg_end"] and t["bg_end"] > t["t0"]:
            t["dur"] = t["bg_end"] - t["t0"]
        else:
            t["dur"] = t["t1"] - t["t0"]
    return S


def _assistant(S, rs, d, ts, last_user):
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    rid = d.get("requestId") or d.get("uuid") or ("r%d" % len(S["requests"]))
    model = msg.get("model") or d.get("requestedModel")
    r = S["requests"].get(rid)
    if r is None:
        start = last_user if (last_user is not None and last_user <= ts) else ts
        r = {"id": rid, "start": start, "first": ts, "end": ts, "model": model, "usage": {}, "think_ms": 0.0, "n_tools": 0,
             "tool_ids": [], "text_chars": 0, "sidechain": bool(d.get("isSidechain")), "effort": d.get("effort"), "api_error": False}
        S["requests"][rid] = r
    if ts > r["end"]:
        r["end"] = ts
    if model and model != "<synthetic>":
        r["model"] = model
        S["models"][model] += 1
    if d.get("effort"):
        r["effort"] = d.get("effort")
    td = d.get("thinkingDurationMs")
    if isinstance(td, (int, float)):
        r["think_ms"] += td
    u = msg.get("usage")
    if isinstance(u, dict):
        cu = r["usage"]
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            v = u.get(k)
            if isinstance(v, (int, float)) and v > cu.get(k, 0):
                cu[k] = v
        cc = u.get("cache_creation")
        if isinstance(cc, dict):
            for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
                v = cc.get(k)
                if isinstance(v, (int, float)) and v > cu.get(k, 0):
                    cu[k] = v
    if d.get("isApiErrorMessage"):
        r["api_error"] = True
        S["api_errors"] += 1
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            t = b.get("text") or ""
            r["text_chars"] += len(t)
            if t.strip():
                r["ends_q"] = t.rstrip().rstrip("*_`)\"' ").endswith("?")  # the turn's last text asks the user something
            if d.get("isApiErrorMessage"):
                S["api_error_texts"][re.sub(r"\d{4,}|req_\w+", "#", t[:90])] += 1
                for sid, conf, sn in scan_signals(rs, t, "api", "", True):
                    S["signals"].append({"id": sid, "conf": "high", "ts": ts, "tool": "api", "cls": "api", "snippet": sn, "sec": 0.0})
        elif bt == "tool_use":
            name = b.get("name") or "?"
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            cls, detail = classify_tool(name, inp, rs)
            tid = b.get("id") or ("t%d" % len(S["tools"]))
            cmd = inp.get("command") if name == "Bash" and isinstance(inp.get("command"), str) else None
            path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
            t = {"id": tid, "name": name, "cls": cls, "detail": detail, "t0": ts, "t1": None, "err": False, "chars": 0,
                 "imgs": 0, "cmd": _keep_cmd(cmd), "path": path if isinstance(path, str) else None,
                 "req": rid, "bg": bool(inp.get("run_in_background")), "bg_end": None, "timed_out": False,
                 "interrupted": False, "agent_id": None, "is_async": False, "exit": None, "pm": None,
                 "ro": bool(cmd and is_read_only_bash(cmd, rs)), "desc": (inp.get("description") or "")[:200] if isinstance(inp.get("description"), str) else "",
                 "target": next((inp[k][:200] for k in ("url", "query", "pattern", "skill", "selector", "subagent_type") if isinstance(inp.get(k), str)), None),
                 "persisted": 0, "file": S["file"], "rng": (inp.get("offset"), inp.get("limit")),
                 "cwd": d.get("cwd") if isinstance(d.get("cwd"), str) else S["cwd"]}
            S["tools"][tid] = t
            S["tool_order"].append(tid)
            r["ends_q"] = False
            r["n_tools"] += 1
            r["tool_ids"].append(tid)
            if name == "Read" and t["path"]:
                S["reads"].append(tid)
            elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and t["path"]:
                S["writes"].append(tid)
            if cmd and len(S["ctx_blob"]) < 4000:
                S["ctx_blob"].append(cmd[:200])
            elif t["path"] and len(S["ctx_blob"]) < 4000 and name in ("Read", "Edit", "Write"):
                S["ctx_blob"].append(t["path"])


def _user(S, rs, d, ts, is_sub):
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    content = msg.get("content")
    pm = d.get("permissionMode")
    if pm:
        S["perm_modes"][pm] += 1
    blocks = content if isinstance(content, list) else None
    has_result = False
    tur = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
    n_results = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result") if blocks else 0
    if blocks:
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            has_result = True
            t = S["tools"].get(b.get("tool_use_id"))
            if t is None:
                continue
            t["t1"] = ts
            t["pm"] = pm
            c = b.get("content")
            chars, imgs = _result_stats(c)
            t["chars"], t["imgs"] = chars, imgs
            S["images"] += imgs
            t["err"] = bool(b.get("is_error"))
            text = c if isinstance(c, str) else _text_of(c)
            t["rhash"] = zlib.crc32((text[:2000] + "|" + text[-500:] + str(len(text))).encode("utf-8", "replace"))
            m = _EXIT.match(text[:20]) if text else None
            if m:
                t["exit"] = int(m.group(1))
                if t["exit"] != 0:
                    t["err"] = True
            if n_results == 1 and tur:
                if tur.get("interrupted"):
                    t["interrupted"] = True
                if tur.get("timedOutAfterMs"):
                    t["timed_out"] = True
                if tur.get("backgroundTaskId"):
                    t["bg"] = True
                    S["task_ids"][str(tur["backgroundTaskId"])] = t["id"]
                ps = tur.get("persistedOutputSize")
                if isinstance(ps, (int, float)):
                    t["persisted"] = int(ps)
                if tur.get("agentId"):
                    t["agent_id"] = tur.get("agentId")
                    S["task_ids"][str(tur["agentId"])] = t["id"]
                if tur.get("runId") and isinstance(tur.get("runId"), str):
                    t["run_id"] = tur.get("runId")  # Workflow tool: its agents live in subagents/workflows/<runId>/
                    t["wf_name"] = str(tur.get("workflowName") or "")[:80]
                if tur.get("isAsync") or tur.get("status") == "async_launched":
                    t["is_async"] = True
            if t["name"] not in SCAN_TOOLS_SKIP and not t["ro"] and text and not t["cls"].startswith(("mcp:", "web", "subagent", "harness", "other_tool")):
                for sid, conf, sn in scan_signals(rs, text, t["name"], t["cmd"] or "", t["err"]):
                    S["signals"].append({"id": sid, "conf": conf, "ts": ts, "tool": t["name"], "cls": t["cls"], "snippet": sn, "sec": 0.0, "tid": t["id"]})
            if text and t["name"] == "Bash" and not t["ro"] and rs.measures:
                scan_measures(S, rs, text, t, ts)
            if text and t["name"] not in SCAN_TOOLS_SKIP:
                head = text[:SCAN_HEAD] + text[-SCAN_TAIL:] if len(text) > SCAN_HEAD + SCAN_TAIL else text
                for rr in rs.result_rules:
                    if rr["_rx"].search(head):
                        t.setdefault("res_hits", []).append(rr["id"])
            if t["name"] == "Bash" and text:
                head = text[:3000]
                pids = [a or b for a, b in _PID_OUT.findall(head)][:8]
                if pids:
                    t["pids"] = pids
                tk = _TASK_OUT.findall(t["cmd"] or "")
                if tk:
                    t["reads_tasks"] = tk[:4]  # 'cat .../tasks/<id>.output': prints what that background command printed
            if t["name"] in ("Bash",) and text and len(S["ctx_blob"]) < 4200:
                # short output lines help rules that need environment context (GPU present, etc.)
                if re.search(r"nvidia-smi|NVIDIA-SMI|nvenc|vaapi", text[:4000], re.I):
                    S["ctx_blob"].append("nvidia-smi " + text[:200])
    if has_result:
        return
    if is_human_prompt(d, content, is_sub):
        S["human_ts"].append(ts)


def _notifications(S, text, ts):
    """End times from <task-notification> blocks (any number per record, anywhere in the text). A block names the
    tool by tool-use id, or only by task id (background task id or agent id). The earliest end notice wins: the
    same task may notify again later (a resumed agent), and the queue sees a notice before the user record does."""
    if not text or "<task-notification" not in text:
        return
    for m in _NOTIF_BLOCK.finditer(text):
        blk = m.group(1)
        st = _NOTIF_STATUS.search(blk)
        if st and st.group(1).strip() not in _END_STATUS:
            continue
        t = None
        mid = _NOTIF_ID.search(blk)
        if mid:
            t = S["tools"].get(mid.group(1).strip())
        if t is None:
            mt = _NOTIF_TASK.search(blk)
            tid = S["task_ids"].get(mt.group(1).strip()) if mt else None
            t = S["tools"].get(tid) if tid else None
        if t is not None and (t["bg_end"] is None or ts < t["bg_end"]):
            t["bg_end"] = ts
