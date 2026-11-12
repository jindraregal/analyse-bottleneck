"""Load data-driven rule files from rules/*.json (extensible: drop in more files)."""
import json
import os
import re

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "rules")


class RuleSet(object):
    def __init__(self, rules_dir=None, extra_dirs=()):
        self.dirs = [rules_dir or os.environ.get("RB_RULES_DIR") or DEFAULT_DIR] + list(extra_dirs)
        self.commands = {"rules": [], "tool_classes": {}, "mcp_name_classes": [], "read_only_bash": None, "mechanical_classes": []}
        self.signals = []
        self.measures = []
        self.advice = {}
        self.stack = []
        self.models = {}
        self.thresholds = {}
        self.segments = {}
        self.describe = {"specific": [], "fallback": []}
        self.waiting = {"parts": [], "rules": []}
        self.deepdive = []
        self.warnings = []
        for d in self.dirs:
            self._load_dir(d)
        self._compile()

    def _load_dir(self, d):
        if not os.path.isdir(d):
            self.warnings.append("rules dir missing: %s" % d)
            return
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            p = os.path.join(d, name)
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                self.warnings.append("skip %s: %s" % (name, e))
                continue
            kind = data.get("kind")
            if kind == "command_classes":
                c = self.commands
                c["rules"] += data.get("rules", [])
                c["tool_classes"].update(data.get("tool_classes", {}))
                c["mcp_name_classes"] += data.get("mcp_name_classes", [])
                c["read_only_bash"] = data.get("read_only_bash") or c["read_only_bash"]
                c["mechanical_classes"] = data.get("mechanical_classes") or c["mechanical_classes"]
            elif kind == "measures":
                self.measures += data.get("measures", [])
            elif kind == "env_signals":
                self.signals += data.get("signals", [])
            elif kind == "stack_rules":
                self.stack += data.get("rules", [])
            elif kind == "advice":
                self.advice.update(data.get("advice", {}))
            elif kind == "models":
                self.models.update(data)
            elif kind == "watch_thresholds":
                self.thresholds.update(data)
            elif kind == "intents":
                pass  # read by rbn.calls
            elif kind == "describe":
                for k in ("specific", "fallback"):
                    self.describe[k] += data.get(k, [])
                for k in ("max_words", "max_chars"):
                    if k in data:
                        self.describe[k] = data[k]
            elif kind == "waiting":
                self.waiting["parts"] += data.get("parts", [])
                self.waiting["rules"] += data.get("rules", [])
            elif kind == "deepdive":
                self.deepdive += data.get("deep_dives", [])
            elif kind == "segments":
                self.segments.update({k: v for k, v in data.items() if k not in ("kind", "_doc")})
            else:
                self.warnings.append("skip %s: unknown kind %r" % (name, kind))

    def _rx(self, pat, flags=0, where=""):
        try:
            return re.compile(pat, flags)
        except re.error as e:
            self.warnings.append("bad regex in %s: %s" % (where, e))
            return None

    def _compile(self):
        for r in self.commands["rules"]:
            r["_rx"] = self._rx(r.get("regex", "(?!x)x"), re.I, "command class " + r.get("class", "?"))
            r["_unless"] = self._rx(r["unless"], re.I, "command class unless") if r.get("unless") else None
        for k in ("specific", "fallback"):
            for r in self.describe[k]:
                r["_rx"] = self._rx(r.get("regex", "(?!x)x"), re.I, "describe " + r.get("text", "?"))
        for r in self.commands["mcp_name_classes"]:
            r["_rx"] = self._rx(r.get("regex", "(?!x)x"), re.I, "mcp class")
        ro = self.commands.get("read_only_bash")
        self.read_only_rx = self._rx(ro, re.I, "read_only_bash") if ro else None
        for s in self.signals:
            s["_rx"] = self._rx(s.get("regex", "(?!x)x"), 0, "signal " + s.get("id", "?"))
        for m in self.measures:
            m["_rx"] = self._rx(m.get("regex", "(?!x)x"), 0, "measure " + m.get("id", "?"))
        good = []
        for r in self.stack:
            d = r.get("detector") or {}
            r["_rx"] = self._rx(d["regex"], re.I, "rule " + r.get("id", "?")) if d.get("regex") else None
            r["_ex"] = self._rx(d["exclude_regex"], re.I, "rule exclude " + r.get("id", "?")) if d.get("exclude_regex") else None
            r["_ctx"] = self._rx(d["requires_context"], re.I, "rule ctx") if d.get("requires_context") else None
            r["_abs"] = self._rx(d["requires_absent"], re.I, "rule absent") if d.get("requires_absent") else None
            fb = d.get("followed_by")
            r["_fb"] = self._rx(fb["regex"], re.I, "rule followed_by") if fb else None
            r["_boost"] = [(self._rx(b["context_regex"], re.I, "boost"), b["confidence"]) for b in r.get("confidence_boost", [])]
            if r.get("id") and (r["_rx"] is not None or d.get("type") in ("signal_overlap",)):
                good.append(r)
        self.stack = sorted(good, key=lambda r: r.get("priority", 50))
        self.result_rules = [r for r in self.stack if (r.get("detector") or {}).get("match_target") == "result" and r["_rx"] is not None]
        m = self.models
        self.weights = m.get("weights", {"input": 1, "output": 5, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2})
        self.tiers = [(str(t["match"]).lower(), t.get("tier", "mid"), float(t.get("cost", 1))) for t in m.get("tiers", [])]
        dt = m.get("default_tier", {"tier": "mid", "cost": 1.0})
        self.default_tier = (dt.get("tier", "mid"), float(dt.get("cost", 1.0)))
        self.cache_ttl = m.get("cache_ttl_seconds", {"5m": 300, "1h": 3600})
        self.image_tokens = m.get("image_tokens_estimate", 1500)
        self.oversized_chars = m.get("oversized_result_chars", 20000)
        self.cpt = m.get("chars_per_token", 4)

    def tier_of(self, model):
        ml = (model or "").lower()
        for match, tier, cost in self.tiers:
            if match in ml:
                return tier, cost
        return self.default_tier
