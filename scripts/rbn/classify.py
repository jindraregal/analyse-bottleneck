"""Classify tool calls into wall-clock classes using the data-driven rule table."""
import re

from .util import match_text, raw_text

HUMAN = ("AskUserQuestion", "ExitPlanMode")


def classify_bash(cmd, rs):
    s = match_text(cmd)
    raw = None
    for r in rs.commands["rules"]:
        rx = r.get("_rx")
        if rx is None:
            continue
        if r.get("raw"):
            raw = raw if raw is not None else raw_text(cmd)
            if rx.search(raw):
                return r["class"]
        elif rx.search(s) and not (r.get("_unless") and r["_unless"].search(s)):
            return r["class"]
    return "shell_other"


def mcp_parts(name):
    # mcp__<server>__<tool>
    parts = name.split("__", 2)
    server = parts[1] if len(parts) > 1 else "unknown"
    tool = parts[2] if len(parts) > 2 else ""
    return server, tool


def classify_tool(name, inp, rs):
    """Return (class, detail). detail is the mcp server for mcp tools."""
    if name in ("Bash", "BashOutput"):
        cmd = (inp or {}).get("command", "") if name == "Bash" else ""
        return (classify_bash(cmd, rs) if cmd else "shell_other"), None
    if name.startswith("mcp__"):
        server, tool = mcp_parts(name)
        label = (server + "__" + tool)
        for r in rs.commands["mcp_name_classes"]:
            if r.get("_rx") is not None and r["_rx"].search(label):
                return r["class"], server
        return "mcp:" + re.sub(r"^claude_ai_", "", server), server
    cls = rs.commands["tool_classes"].get(name)
    if cls:
        return cls, None
    return "other_tool", None


def is_read_only_bash(cmd, rs):
    if rs.read_only_rx is None:
        return False
    return bool(rs.read_only_rx.search(match_text(cmd)))
