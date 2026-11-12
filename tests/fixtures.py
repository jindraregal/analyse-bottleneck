"""Synthetic transcript builder. Everything here is invented; no real data."""
import json
import os
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(sec):
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Tx(object):
    """Builds one transcript file. Times are seconds from a fixed origin."""

    def __init__(self, session_id="sess-a", sidechain=False, perm=None, cwd=None):
        self.sid = session_id
        self.cwd = cwd  # working directory stamped on each record (set it again to model a `cd` that stuck)
        self.rows = []
        self.n = 0
        self.sidechain = sidechain
        self.perm = perm

    def _id(self, p):
        self.n += 1
        return "%s%d" % (p, self.n)

    def human(self, t, text="do the thing"):
        self.rows.append({"type": "user", "timestamp": iso(t), "sessionId": self.sid, "message": {"role": "user", "content": text},
                          "origin": {"kind": "human"}, "turnOrigin": "human"})

    def assistant(self, t_end, blocks, usage=None, model="claude-opus-test", rid=None, think_ms=None, api_error=False):
        rid = rid or "%s:%s" % (self.sid, self._id("req_"))
        u = {"input_tokens": 5, "output_tokens": 50, "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 500}
        u.update(usage or {})
        for i, b in enumerate(blocks):
            row = {"type": "assistant", "timestamp": iso(t_end), "sessionId": self.sid, "requestId": rid, "uuid": "%s:%s" % (self.sid, self._id("u")),
                   "isSidechain": self.sidechain, "message": {"role": "assistant", "model": model, "content": [b], "usage": u}}
            if b.get("type") == "thinking" and think_ms:
                row["thinkingDurationMs"] = think_ms
            if api_error:
                row["isApiErrorMessage"] = True
            if self.cwd:
                row["cwd"] = self.cwd
            self.rows.append(row)
        return rid

    def tool(self, t_use, t_res, name, inp, result="ok", is_error=False, req_end=None, tur=None, usage=None, think_ms=None, perm=None, model="claude-opus-test"):
        """One request ending with a tool_use at t_use, result at t_res."""
        tid = self._id("toolu_")
        blocks = []
        if think_ms:
            blocks.append({"type": "thinking", "thinking": "..."})
        blocks.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
        self.assistant(t_use, blocks, usage=usage, think_ms=think_ms, model=model)
        self.result(t_res, tid, result, is_error, tur, perm)
        return tid

    def use_only(self, t_use, name, inp, usage=None):
        tid = self._id("toolu_")
        self.assistant(t_use, [{"type": "tool_use", "id": tid, "name": name, "input": inp}], usage=usage)
        return tid

    def result(self, t_res, tid, result="ok", is_error=False, tur=None, perm=None):
        row = {"type": "user", "timestamp": iso(t_res), "sessionId": self.sid,
               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}]}}
        if tur:
            row["toolUseResult"] = tur
        p = perm or self.perm
        if p:
            row["permissionMode"] = p
        self.rows.append(row)

    def text(self, t_end, text="done", usage=None):
        return self.assistant(t_end, [{"type": "text", "text": text}], usage=usage)

    def system(self, t, subtype, **kw):
        row = {"type": "system", "subtype": subtype, "timestamp": iso(t), "sessionId": self.sid}
        row.update(kw)
        self.rows.append(row)

    def write(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in self.rows:
                f.write(json.dumps(r) + "\n")
            f.write("this line is not json\n")
            f.write(json.dumps({"type": "api-request-blob", "hash": "x" * 50}) + "\n")
            f.write(json.dumps({"type": "future-record-type", "timestamp": iso(0), "weird": [1, 2, 3]}) + "\n")
        return path


def make_project(root, name="proj-demo"):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    return d
