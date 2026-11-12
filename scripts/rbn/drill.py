"""Leaf attribution: what exactly every second of autonomous time was spent on.

The parent's timeline only says "waiting on a subagent" or "waiting on a background task". Here each
of those seconds is resolved into what was really running at that moment:

- a background Bash wait becomes the background command itself (its class and tool id);
- a subagent/workflow wait becomes what the children were doing then, recursively: all child
  transcripts of the session (nested subagents and workflow agents included) are swept together with
  the timeline's priority order, a child's own waits are transparent, and the earliest-started
  interval wins ties (deterministic);
- whatever cannot be resolved (no child transcript, child idle between steps, a nested wait with
  no transcript) becomes the leaf "unknown_wait", never something else.

Every second of autonomous time ends in exactly one leaf (category, reference, session), so the leaves
sum to the autonomous time. A leaf reference names a tool call, a model request, an unresolved wait or
a gap. `describe()` turns a reference into a groupable "what" (command signature, model, tool name).
"""
import bisect
import json
import os
import re
import shlex

from .util import scrub
from .classify import classify_bash, is_read_only_bash, mcp_parts
from .timeline import RANK, build_intervals, classify_gaps, rank_of, sweep
from .util import raw_text

UNKNOWN = "unknown_wait"
DEPLOY = "deploy"
DEPLOY_KEEPS = ("build", "test", "install", "media_gpu")  # a heavy whole-command class wins over a deploy part
MODEL_CATS = ("thinking", "inference", "api_wait")
R_WAIT, R_IDLE = 98, 99  # transparent child slices: lose to any real activity of any child
BG_CAP = 1800.0  # timeline.build_intervals default: a background command with no completion notice counts this long
REASONS = {"none": "no subagent transcript covers this wait", "idle": "subagent idle between steps",
           "nested": "nested subagent without transcript"}


def excluded(cat):
    return cat is None or cat.startswith("human_wait") or cat == "idle_gap"


# ---------------------------------------------------------------- attribution

def _clip(lo, hi, s, e):
    return max(lo, s), min(hi, e)


def _child_intervals(S, rs, spans, win):
    """Leaf-ranked intervals of one child transcript for the union sweep."""
    lo, hi = S["first"], S["last"]
    if win[0] is not None:
        lo = max(lo, win[0])
    if win[1] is not None:
        hi = min(hi, win[1])
    out = []
    if lo is None or hi is None or hi <= lo:
        return out
    sid = S["session_id"]
    ivs, _ = build_intervals(S, rs, spans)
    for iv in ivs:
        s, e = _clip(lo, hi, iv[0], iv[1])
        if e <= s:
            continue
        cat, tid = iv[2], iv[4]
        if cat == "subagent_wait":
            out.append((s, e, "_wait", R_WAIT, None, ("wait", sid, tid)))
        elif cat.startswith("background_wait:"):
            leaf = cat.split(":", 1)[1]
            # a background run with an end notice ranks as what it is; one without (counted up to the cap) ranks as
            # a background wait, so an invented tail never outranks a sibling's real work
            r = rank_of(leaf) if (S["tools"].get(tid) or {}).get("bg_end") else RANK["background_wait"]
            out.append((s, e, leaf, r, None, ("tool", sid, tid)))
        elif cat in MODEL_CATS:
            out.append((s, e, cat, RANK[cat], None, ("req", sid, iv[5][1] if len(iv) > 5 else None)))
        else:
            out.append((s, e, cat, rank_of(cat), None, ("tool", sid, tid)))
    out.append((lo, hi, "_idle", R_IDLE, None, ("idle", sid, None)))
    return out


def drill_group(a, rs, win=(None, None), idle_threshold=180.0):
    """-> {"leaves": {(cat, ref): seconds}, "autonomous": s, "by_cat": {...}, "unresolved": {reason: s}}

    ref is one of ("tool", sid, tid), ("req", sid, rid), ("unres", sid, wait_tid, reason), ("gap", sid, None).
    """
    main = a["main"]
    tl = a["tl"]
    res = {"leaves": {}, "autonomous": 0.0, "by_cat": {}, "unresolved": {}}
    if not tl.get("span") or tl.get("lo") is None:
        return res
    lo, hi = tl["lo"], tl["hi"]
    spans = {s["session_id"][len("agent-"):]: (s["first"], s["last"]) for s in a["subs"]}
    msid = main["session_id"]
    ivs, human_sorted = build_intervals(main, rs, spans, fill=a.get("fill"))
    ps = classify_gaps(sweep(ivs, lo, hi, payload=True), human_sorted, idle_threshold, sorted(main["away_ts"]))
    cu = []
    for S in a["subs"]:
        cu += _child_intervals(S, rs, spans, win)
    union = sweep(cu, lo, hi, payload=True) if cu else []
    union = [u for u in union if u[2] is not None]
    ustarts = [u[0] for u in union]
    leaves = res["leaves"]
    by_cat = res["by_cat"]

    def add(cat, ref, sec):
        if sec > 0:
            leaves[(cat, ref)] = leaves.get((cat, ref), 0.0) + sec

    for s, e, cat, p in ps:
        d = e - s
        by_cat[cat] = by_cat.get(cat, 0.0) + d
        if excluded(cat):
            continue
        res["autonomous"] += d
        if cat == "subagent_wait":
            _resolve(s, e, p, msid, union, ustarts, add, res["unresolved"])
        elif cat.startswith("background_wait:"):
            add(cat.split(":", 1)[1], ("tool", msid, p), d)
        elif cat in MODEL_CATS:
            add(cat, ("req", msid, p[1] if isinstance(p, tuple) else None), d)
        elif cat == "overhead" or p is None:
            add("overhead", ("gap", msid, None), d)
        else:
            add(cat, ("tool", msid, p), d)
    return res


def _resolve(s, e, wait_tid, msid, union, ustarts, add, unresolved):
    def unknown(reason, sec):
        add(UNKNOWN, ("unres", msid, wait_tid, reason), sec)
        unresolved[reason] = unresolved.get(reason, 0.0) + sec

    cur = s
    i = max(0, bisect.bisect_right(ustarts, s) - 1)
    while i < len(union) and union[i][0] < e:
        us, ue, ucat, up = union[i]
        a0, a1 = max(s, us), min(e, ue)
        if a1 > a0:
            if a0 > cur:
                unknown("none", a0 - cur)
            if ucat == "_wait":
                unknown("nested", a1 - a0)
            elif ucat == "_idle":
                unknown("idle", a1 - a0)
            else:
                add(ucat, up, a1 - a0)
            cur = a1
        i += 1
    if e > cur:
        unknown("none", e - cur)


# ---------------------------------------------------------------- who ran it

def _meta(path):
    p = path[:-len(".jsonl")] + ".meta.json"
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _short(text, n=60):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def spawner_labels(a):
    """child session id -> 'subagent: <description> (<type>)' / 'workflow agent: <label> (<workflow>)'."""
    tools = {}
    by_agent = {}
    by_run = {}
    for S in a["sessions"]:
        for tid, t in S["tools"].items():
            tools[tid] = t
            if t.get("agent_id"):
                by_agent[t["agent_id"]] = t
            if t.get("run_id"):
                by_run[t["run_id"]] = t
    out = {a["main"]["session_id"]: "main session"}
    for S in a["subs"]:
        sid = S["session_id"]
        aid = sid[len("agent-"):]
        m = _meta(S["file"])
        parts = S["file"].replace(os.sep, "/").split("/")
        run = parts[-2] if len(parts) > 3 and parts[-3] == "workflows" else None
        if run or m.get("agentType") == "workflow-subagent":
            wf = by_run.get(run) if run else None
            name = (wf or {}).get("wf_name") or ""
            lab = "workflow agent: " + _short(m.get("description") or aid[:8], 50)
            out[sid] = lab + (" (%s)" % _short(name, 30) if name else "")
            continue
        t = tools.get(m.get("toolUseId")) or by_agent.get(aid)
        desc = m.get("description") or (t or {}).get("desc") or ""
        typ = m.get("agentType") or ((t or {}).get("target") if t and t.get("name") in ("Agent", "Task") else None)
        if desc:
            out[sid] = "subagent: %s%s" % (_short(desc, 50), " (%s)" % _short(typ, 24) if typ else "")
        else:
            out[sid] = "subagent %s (spawner unknown)" % aid[:8]
    return out


def wait_label(t):
    """Label of the tool the parent was waiting on (Agent/Task/Workflow)."""
    if t is None:
        return "unknown wait"
    if t.get("name") == "Workflow" or t.get("run_id"):
        return "workflow: " + _short(t.get("wf_name") or t.get("desc") or "unnamed", 50)
    desc = t.get("desc") or ""
    typ = t.get("target") if t.get("name") in ("Agent", "Task") else None
    if desc:
        return "subagent: %s%s" % (_short(desc, 50), " (%s)" % _short(typ, 24) if typ else "")
    return "%s call" % t.get("name", "?")


# ---------------------------------------------------------------- what exactly

_SPLIT_OPS = ("&&", "||", ";", "|", "&", "\n")
_CTRL_PREFIX = re.compile(r"^(?:(?:if|then|else|elif|do|while|until|time|!|\{|\()\s+)+")
_CTRL_ONLY = re.compile(r"^(?:fi|done|then|else|do|esac|break|continue|\}|\)|\{|\(|;;)$")
# 'case "$v" in a*) cmd', '*) echo x', 'done|ok) break': the arm's command is what runs
_CASE_ARM = re.compile(r"^(?:case\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s+in\s+)?\(?(?:[\w*.?|\[\]$-]+|\"[^\"]*\"|'[^']*')\)\s+")
_FOR_HEAD = re.compile(r"^(?:for|select|case)\b")
# 'until grep -q done log; do sleep 10; done': the condition is what the loop waits for
_POLL_COND = re.compile(r"(?:^|[;&|(\s'\"])(until|while)\s+(.+?)\s*;\s*do\b", re.S)
# 'for i in $(seq 1 30); do grep -q done log && break; sleep 5; done' is the same wait: until <condition>
_FOR_BREAK = re.compile(r"(?:^|[;&|(\s'\"])for\s[^;]*;\s*do\s+(.+?)\s*(&&|\|\|)\s*break\b", re.S)


def _poll_cond(text):
    """-> (keyword, condition) of a poll loop's wait condition, or None. 'for ...; do C || break' waits while C."""
    m = _POLL_COND.search(text)
    if m:
        return m.group(1), m.group(2)
    m = _FOR_BREAK.search(text)
    if not m:
        return None
    cond = re.sub(r"\s*(?:>\s*/dev/null|2>&1|&>\s*/dev/null)+\s*$", "", m.group(1))
    return ("until" if m.group(2) == "&&" else "while"), cond
# a PID in a poll condition says nothing in a label and splits identical loops into separate items
_PID_ARG = re.compile(r"(\bkill\s+-(?:0|s\s*0)\s+|\bps\s+(?:-\w+\s+)*-p\s*|--pid[= ]|/proc/|\bwait\s+)\d+")
_TRIVIAL_COND = re.compile(r"^(?:true|:|\[\s*1\s*\]|\(\(\s*1\s*\)\))$")
# Claude Code's per-session temp dir (/tmp/claude-<uid>/<project>/<session uuid>/) says nothing in a label
_SESSION_TMP = re.compile(r"/tmp/claude-\d+/[^/\s'\"]+/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/")
_SESSION_UUID = re.compile(r"/tmp/claude-\d+/[^/\s'\"]+/([0-9a-f]{8})(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/")


def _disp(text, limit=90):
    return scrub(_SESSION_TMP.sub("…/", re.sub(r"\s+", " ", text.strip())), limit)
_REDIR = re.compile(r"\s+(?:\d?>>?|&>>?|\d?<(?!<))\s*(?:&\d|[^\s)]+)|\s+\d?>&\d")  # '... > x.log)' keeps the ')'
_ASSIGN_ONLY = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|[^\s$]\S*|\$\{?\w+\}?\S*|\$\(\(\S*)?\s*)+$")
_ASSIGN_SUBST = re.compile(r"^(?:local\s+|export\s+)?[A-Za-z_][A-Za-z0-9_]*=[\"']?\$\((?!\()(.*)\)[\"']?$", re.S)  # not $((n+1))
_BASH_C = re.compile(r"^(?:bash|sh|zsh)\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*c\s+(['\"])(.*)\1\s*$", re.S)
_INTERP = re.compile(r"^(?:python(?:\d(?:\.\d+)?)?|node|deno|tsx|ts-node|ruby|perl|php|bash|sh|zsh|Rscript|julia)$")
_RUNNERS = {"npx", "pnpx", "bunx", "pnpm", "npm", "yarn", "bun"}
_RUNNER_SKIP = {"exec", "dlx", "x"}
_VALUE_FLAGS = {"-C", "-c", "-f", "--prefix", "--filter", "--dir", "--cwd", "-F", "--file", "-R", "--repo", "-w", "--workspace", "-m", "--project-directory"}
_WORD = re.compile(r"^[A-Za-z@][\w:.+@-]*$")
_SUBWORD = re.compile(r"^[A-Za-z][A-Za-z:_-]*$")  # a subcommand: no digits, dots or paths
_SCRIPT = re.compile(r"\.(?:py|js|mjs|cjs|ts|mts|sh|bash|rb|pl|php)$")
_URL = re.compile(r"https?://([^/\s:'\"]+)(:\d+)?")
_NETPROG = {"curl", "wget", "http", "https", "xh", "httpie"}
_SCRIPT_HOSTS = {"node", "tsx", "ts-node", "deno", "bun", "python", "python3", "ruby", "perl", "php", "bash", "sh"}
_MAX_WORDS = {"git": 1, "cargo": 1, "go": 1, "make": 1, "terraform": 1, "pytest": 0, "jest": 0, "vitest": 1, "tsc": 0, "eslint": 0}
_NO_SUB = {"ffmpeg", "ffprobe", "rm", "cp", "mv", "ls", "cat", "grep", "rg", "find", "fd", "sed", "awk", "head", "tail", "wc",
           "sort", "uniq", "cut", "tr", "chmod", "chown", "ln", "tar", "zip", "unzip", "jq", "xargs", "kill", "pkill",
           "pgrep", "ps", "du", "stat", "diff", "file", "readlink", "realpath", "convert", "magick", "sqlite3",
           "psql", "rsync", "scp", "lsof", "tree", "base64", "sha256sum", "md5sum", "identify", "sox", "yt-dlp"}
_GIT_MULTI = {"worktree", "stash", "remote", "submodule", "lfs", "bisect", "notes"}
_SSHPROG = {"ssh", "scp", "sftp", "rsync", "mosh"}
_BIN_SHIM = re.compile(r"^(?:\S*/)?node_modules/\.bin/([\w@.-]+)$")


def split_parts(cmd, depth=0):
    """Quote-aware split of a shell command into simple-command parts (&&, ||, ;, |, &, newline)."""
    parts, buf, q, i, par = [], [], None, 0, 0
    case_par = []  # paren depth at each open 'case': a pattern's ')' there closes no subshell
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            buf.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in ("'", '"'):
            q = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c + cmd[i + 1])
            i += 2
            continue
        if c in "ce" and (i == 0 or cmd[i - 1] in " \t\n;&|(") and cmd[i:i + 4] in ("case", "esac") and (i + 4 >= n or cmd[i + 4] in " \t\n;&|)"):
            if cmd[i] == "c":
                case_par.append(par)
            elif case_par:
                case_par.pop()
        if c == "(":
            par += 1
        elif c == ")" and not (case_par and case_par[-1] == par):
            par = max(0, par - 1)
        if par == 0:
            op = next((o for o in _SPLIT_OPS if cmd.startswith(o, i)), None)
            if op and not (op == "&" and (cmd[i - 1:i] in (">", "<") or cmd[i + 1:i + 2] == ">")) and not (op == "|" and cmd[i - 1:i] == ">"):
                parts.append("".join(buf))
                buf = []
                i += len(op)
                continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    out = []
    for p in parts:
        p = _REDIR.sub("", " " + p).strip()
        if not p:
            continue
        inner = None
        if depth < 3:
            q = re.sub(r"^(?:(?:if|then|else|elif|do|while|until|!)\s+)+", "", p)  # 'do (cd $d && npm ci)'
            m = _BASH_C.match(q)
            ma = _ASSIGN_SUBST.match(q)
            if m:
                inner = m.group(2)
            elif ma:
                inner = ma.group(1)
            elif q.startswith(("(", "$(")) and q.endswith(")") and not q.startswith(("((", "$((")):
                inner = q[2:-1] if q.startswith("$(") else q[1:-1]
        if inner is not None:
            out += split_parts(inner, depth + 1)
        else:
            out.append(p)
    return out


def _clean_part(p, wrappers):
    p = p.strip()
    for _ in range(6):
        before = p
        p = _CTRL_PREFIX.sub("", p).strip()
        p = _CASE_ARM.sub("", p).strip()
        for rx in wrappers:
            p = rx.sub("", p, count=1).strip()
        m = _BASH_C.match(p)
        if m:
            p = m.group(2).strip()
        if p == before:
            break
    p = _REDIR.sub("", " " + p).strip()
    p = re.sub(r"[;)}]+$", "", p).strip()
    return p


def _tokens(p):
    try:
        return shlex.split(p, comments=False, posix=True)
    except ValueError:
        return p.split()


def _host(tok):
    m = _URL.search(tok)
    if m:
        h = m.group(1)
        h = h + (m.group(2) or "") if h in ("localhost", "127.0.0.1", "0.0.0.0") else h
        path = tok[m.end():].split("?", 1)[0].strip("/").split("/")
        return h + ("/" + path[0] if path and path[0] and len(path[0]) <= 24 else "")
    m = re.match(r"^(\$\{?\w+\}?)(/[^?\s]*)?", tok)
    if m:
        path = (m.group(2) or "").strip("/").split("/")[:3]
        return m.group(1) + ("/" + "/".join(path) if path and path[0] else "")
    return None


def command_key(part):
    """Short, stable name of one simple command: program + subcommands (+ script or host)."""
    toks = [t for t in _tokens(part) if t]
    if not toks:
        return ""
    m = _BIN_SHIM.match(toks[0])
    if m:  # './node_modules/.bin/vitest run' is 'npx vitest run'
        toks = ["npx", m.group(1)] + toks[1:]
    prog = toks[0]
    if prog.startswith(("/", "~")) and not _SCRIPT.search(prog):
        prog = prog.rsplit("/", 1)[-1]
    elif prog.startswith(("/", "~")):
        prog = "/".join(prog.split("/")[-2:])
    rest = toks[1:]
    base = prog.rsplit("/", 1)[-1]
    if _SCRIPT.search(base):
        # a script's own arguments are data ('./rec.sh t753-01', '$S/final.sh v6'); only plain words read as a
        # subcommand ('./ops.sh run db:check'), so one script is one item
        words = [prog]
        for t in rest:
            if t.startswith("-") or not _SUBWORD.match(t) or len(words) > 2:
                break
            words.append(t)
        return " ".join(words)
    if base in _NETPROG:
        h = next((_host(t) for t in rest if "://" in t), None)
        if not h:
            h = next((_host(t) for t in rest if re.match(r"^\$\{?\w+\}?/|^\$\{?\w*(?:URL|HOST|ENDPOINT|API)\w*\}?", t, re.I)), None)
        return "%s %s" % (base, h) if h else base
    if base in _SSHPROG:
        tgt = next((t for t in rest if not t.startswith("-") and not re.match(r"^\d+$", t)), "")
        tgt = re.sub(r"^[^@\s]+@", "", tgt).split(":", 1)[0]
        return ("%s %s" % (base, tgt)).strip()
    if _INTERP.match(base) or prog.startswith("$"):
        words = [base if not prog.startswith("$") else prog]
        i = 0
        while i < len(rest):
            t = rest[i]
            if t in ("-m",) and i + 1 < len(rest):
                return "%s -m %s" % (words[0], rest[i + 1])
            if t in ("-c", "-e", "-"):
                return "%s %s (inline script)" % (words[0], t)
            if t.startswith("-"):
                i += 1
                continue
            return "%s %s" % (words[0], t)
        return words[0]
    words = [prog]
    n_after = 0
    limit = 0 if base in _NO_SUB else _MAX_WORDS.get(base, 2)
    i = 0
    while i < len(rest) and n_after < limit:
        t = rest[i]
        if t.startswith("-"):
            if t in _VALUE_FLAGS:
                i += 1
            i += 1
            continue
        if base in _RUNNERS and t in _RUNNER_SKIP and n_after == 0:
            i += 1
            continue
        if _SCRIPT.search(t) and words[-1].rsplit("/", 1)[-1] in _SCRIPT_HOSTS:
            words.append(t)
            break
        if not _WORD.match(t) or "/" in t:
            break
        words.append(t)
        n_after += 1
        if base == "git" and n_after == 1 and t in _GIT_MULTI:
            limit = 2
        elif base in _RUNNERS and n_after == 1 and t in _MAX_WORDS:
            limit = 1 + _MAX_WORDS[t]  # 'npx tsc --incremental false' is tsc, 'npx vitest run' keeps its verb
        elif base in _RUNNERS and n_after == 1 and t in ("tsx", "ts-node", "node"):
            limit = 1  # npx tsx <script>: the script names it
            j = i + 1
            while j < len(rest) and rest[j].startswith("-"):
                j += 1
            if j < len(rest) and _SCRIPT.search(rest[j]):
                words.append(rest[j])
            break
        i += 1
    return " ".join(words)


def _seg_cfg(rs):
    cfg = getattr(rs, "segments", None) or {}
    if "_wrappers" not in cfg:
        rx = []
        for pat in cfg.get("command_wrappers", []):
            try:
                rx.append(re.compile(pat))
            except re.error:
                pass
        cfg["_wrappers"] = rx
        cfg["_noise"] = set(cfg.get("noise_commands", []))
        dp = []
        for pat in cfg.get("deploy_parts", []):
            try:
                dp.append(re.compile(pat))
            except re.error:
                pass
        cfg["_deploy"] = dp
    return cfg


def signature(cmd, rs, cls=None):
    """-> (key, part, part_class, matched): the part of a chained/wrapped command that explains its time.

    Wrappers (env X=Y, nice, timeout N, '<x>-heavy ... --', bash -c '...', subshells, X=$(...)) are
    removed and chains are split. Noise (cd, sleep, echo, plain assignments) and read-only parts (git
    status, ls, grep ...) never win when anything else is there, so a poll loop names what it polls; an
    until/while poll is named by its condition ('until [ -f /tmp/vt/DONE ]').
    A poll loop (cls poll_wait) is named by its condition first: 'while pgrep -f deploy; do sleep 15; done; deploy'
    spent its time in the loop. A deploy part (rules/segments.json deploy_parts) wins unless `cls` is
    build/test/install/media; its class is 'deploy'. Among the rest the first part whose class equals `cls` wins (matched=True), else the part with
    the highest-priority class (matched=False: the whole-command class came from a light part)."""
    cfg = _seg_cfg(rs)
    text = raw_text(cmd or "")
    parts = []
    for p in split_parts(text):
        c = _clean_part(p, cfg["_wrappers"])
        # a wrapper hid a chain ('my-heavy ... -- sh -c "npx vitest run; echo $?"'): split what it ran
        subs = [_clean_part(q, cfg["_wrappers"]) for q in split_parts(c, 1)] if c and c != p.strip() else [c]
        for c in subs:
            if not c or _CTRL_ONLY.match(c) or _FOR_HEAD.match(c) or _ASSIGN_ONLY.match(c):
                continue
            parts.append(c)
    if not parts:
        return "", "", None, False
    heavy = [p for p in parts if (_tokens(p) or [""])[0].rsplit("/", 1)[-1] not in cfg["_noise"]]
    cand = [p for p in heavy if not is_read_only_bash(p, rs)] or heavy or parts
    classes = [classify_bash(p, rs) for p in cand]
    k = None
    if cls == "poll_wait":
        m = _poll_cond(text)
        cond = _clean_part(m[1], cfg["_wrappers"]) if m else ""
        if cond and not _TRIVIAL_COND.match(cond):
            part = _PID_ARG.sub(r"\1<pid>", _disp(m[0] + " " + _REDIR.sub("", " " + m[1]).strip()))
            return part, part, "poll_wait", True
    if cls not in DEPLOY_KEEPS:
        # a deploy step (my-deploy --wait, gcloud run deploy ...) is where a chained deploy command spends its
        # time; the curl health check or git pull around it only gave the whole command its class
        d = next((i for i, p in enumerate(cand) if any(rx.search(p) for rx in cfg.get("_deploy", ()))), None)
        if d is not None:
            return _disp(command_key(cand[d])), _disp(cand[d]), DEPLOY, False
    if cls and cls != "poll_wait":
        k = next((i for i, c in enumerate(classes) if c == cls), None)
    matched = k is not None
    if k is None:
        k = min(range(len(cand)), key=lambda i: (rank_of(classes[i]), i))
    return _disp(command_key(cand[k])), _disp(cand[k]), classes[k], matched


def capped(t):
    """A background command the timeline extends without an end notice (counted until session end or the cap)."""
    return bool(t.get("bg") and not t.get("bg_end") and t.get("name") == "Bash" and t.get("cls") != "service")


def _eff_dur(t, S):
    """Duration the timeline used: a background command without a completion notice ran until the cap."""
    d = t.get("dur")
    if capped(t) and t.get("t1") is not None and S and S.get("last"):
        d = max(d or 0.0, min(S["last"], t["t1"] + BG_CAP) - t["t0"])
    return d


_WAIT_PID = re.compile(r"\bkill\s+-(?:0|s\s*0)\s+(\d+)|\bps\s+(?:-\w+\s+)*-p\s*(\d+)|/proc/(\d+)|\bwait\s+(\d+)")
_WAIT_FILE = re.compile(r"(?:\[\[?\s*!?\s*-[efsdr]\s+|\btest\s+!?\s*-[efsdr]\s+|\bls\s+(?:-\w+\s+)*)(['\"]?)([^\s'\"\]]{4,}|\$\{?\w+\}?[^\s'\"\]]*)\1")
_WAIT_PGREP = re.compile(r"\bpgrep\s+(?:-\w+\s+)*(?:'([^']{4,})'|\"([^\"]{4,})\"|([^\s'\"]{4,}))")
_FALLBACK_CLS = ("test", "build", "lint_typecheck", "install", "media_gpu")  # a pgrep pattern that itself names such a run
_TASK_FILE = re.compile(r"/tasks/([A-Za-z0-9_-]+)\.output\b")
_WAIT_GREP = re.compile(r"\b[ef]?grep\s+(?:-\w+\s+)*(?:'[^']*'|\"(?:[^\"\\]|\\.)*\"|[^\s'\"]+)\s+(['\"]?)([^\s'\"]{4,}|\$\{?\w+\}?[^\s'\"]*)\1")


def _expand_var(needle, text):
    """'$f' or '${D}/x.log' -> the value assigned earlier in the same command ('f=/tmp/x/task.output; ...')."""
    m = re.match(r"^\$\{?(\w+)\}?(.*)$", needle or "")
    if not m:
        return needle
    a = re.search(r"(?:^|[\s;&(])(?:export\s+|local\s+)?%s=(['\"]?)([^\s'\";&|]+)\1" % re.escape(m.group(1)), text)
    return a.group(2) + m.group(2) if a else needle


def heavy_key(cmd, rs):
    """Signature of the highest-priority part of a whole command text, heredoc bodies included (a generated
    run.sh whose loop runs the tests is named by the tests)."""
    cfg = _seg_cfg(rs)
    best = None
    for p in split_parts(cmd or ""):
        c = _clean_part(p, cfg["_wrappers"])
        if not c or _CTRL_ONLY.match(c) or _FOR_HEAD.match(c) or _ASSIGN_ONLY.match(c):
            continue
        if (_tokens(c) or [""])[0].rsplit("/", 1)[-1] in cfg["_noise"] or is_read_only_bash(c, rs):
            continue
        if any(rx.search(c) for rx in cfg.get("_deploy", ())):
            return _disp(command_key(c), 60)  # a deploy step is what a deploy-and-check command waits for
        r = rank_of(classify_bash(c, rs))
        if best is None or r < best[0]:
            best = (r, c)
    return _disp(command_key(best[1]), 60) if best else None


def waited_for(t, S, rs, cache=None):
    """What a poll loop waited on: the earlier command of the same transcript that started it. Found by the PID
    in the condition ('kill -0 <pid>': the command that printed it, or the background command whose output
    file was read to get it), else by the file or process name ('[ -f /tmp/x/DONE ]', 'grep -q done /tmp/x.log',
    'pgrep -f name': the latest earlier command that mentions it; a pgrep pattern that is itself a test/build run
    names it when no earlier command does). -> its signature, or None."""
    ck = ("wf", t["id"])
    if cache is not None and ck in cache:
        return cache[ck]
    res = proc = None
    m = _poll_cond(raw_text(t.get("cmd") or ""))
    if m:
        cond = m[1]
        earlier = [u for u in (S["tools"][x] for x in S["tool_order"]) if u["t0"] < t["t0"] and u["name"] == "Bash" and u.get("cmd")]
        src = None
        pm = _WAIT_PID.search(cond)
        if pm:
            pid = next(g for g in pm.groups() if g)
            for u in reversed(earlier):
                if pid in (u.get("pids") or ()):
                    for tk in u.get("reads_tasks") or ():
                        src = src or S["tools"].get(S["task_ids"].get(tk))
                    if src is None and not u.get("ro"):
                        src = u
                    break
        else:
            fm = _WAIT_FILE.search(cond)
            pg = None if fm else _WAIT_PGREP.search(cond)
            fm = fm or (None if pg else _WAIT_GREP.search(cond))
            needle = _expand_var(fm.group(2), raw_text(t.get("cmd") or "")) if fm else None
            if pg:
                needle = proc = next(g for g in pg.groups() if g)
            if needle and needle.startswith("$") and len(needle) < 4:
                needle = None  # an unresolved '$f' would match anything
            tk = _TASK_FILE.search(needle or "")
            if tk:  # a background command's own output file: the command that started that task
                src = S["tools"].get(S["task_ids"].get(tk.group(1)) or "")
                needle = None if src else needle
            if needle:
                # the command that writes the file ('> x.log', 'tee x.log', 'touch DONE'), whatever its class (a
                # '(npx vitest run > x.log; echo done) & sleep 5' launcher is itself a poll); else the latest
                # earlier non-poll command that mentions it
                wr = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?|\btouch\s+)\s*['\"]?" + re.escape(needle) + r"(?![\w.-])")
                src = next((u for u in reversed(earlier) if wr.search(u["cmd"])), None)
                base = needle.rstrip("/").rsplit("/", 1)[-1]
                if src is None and base != needle and len(base) >= 6 and not proc:
                    # the poll names /abs/path/out/x.log, the writer wrote 'out/x.log' from its own directory
                    wr = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?|\btouch\s+)\s*['\"]?(?:[^\s'\";&|]*/)?" + re.escape(base) + r"(?![\w.-])")
                    src = next((u for u in reversed(earlier) if wr.search(u["cmd"])), None)
            for u in reversed(earlier) if needle and src is None else ():
                if u["cls"] != "poll_wait" and not u.get("ro") and needle in u["cmd"]:
                    src = u
                    break
            if src is None and proc and classify_bash(proc, rs) in _FALLBACK_CLS:
                res = _disp(command_key(proc), 60)  # 'while pgrep -f "vitest run src/a"': started elsewhere, still named
        if src is not None:
            res = heavy_key(src["cmd"], rs)
    if cache is not None:
        cache[ck] = res
    return res


_TMP_DIR = r"(?:/tmp/|/var/tmp/|/dev/shm/|…/|\S*/scratchpad(?:/|\b)|scratchpad/)"
_SCRATCH_PATH = re.compile(r"(?:^|[\s'\"=])" + _TMP_DIR)
_CD_SCRATCH = re.compile(r"\bcd\s+['\"]?" + _TMP_DIR)
_VAR_SCRIPT = re.compile(r"(?:^|\s)['\"]?\$\{?(\w+)\}?/\S*\.(?:py|js|mjs|cjs|ts|mts|sh|bash|rb|pl|php)\b")


def scratch_script(cmd, key, part=""):
    """True for a one-off script: inline code ('python3 - <<EOF', 'node -e') or a script file under a temp or
    scratchpad directory (by its path, a 'cd' into such a directory, or a variable set to one)."""
    if key.endswith("(inline script)"):
        return True
    if not any(_SCRIPT.search(w) for w in key.split()):
        return False
    if _SCRATCH_PATH.search(part or key):
        return True
    text = (cmd or "")[:4000]
    if _CD_SCRATCH.search(text):
        return True
    m = _VAR_SCRIPT.search(part or key)
    return bool(m and re.search(r"\b%s=['\"]?%s" % (re.escape(m.group(1)), _TMP_DIR), text))


def describe(cat, ref, sess, labels, rs, cache=None):
    """-> dict(key, what, label, inst, dur, via) for one leaf, or None."""
    kind, sid = ref[0], ref[1]
    S = sess.get(sid)
    via = labels.get(sid, "subagent")
    if kind == "gap":
        return {"key": "gaps", "what": "no tool or request open", "label": "Gaps between steps", "inst": None, "dur": None, "via": via}
    if kind == "unres":
        t = S["tools"].get(ref[2]) if S else None
        lab = wait_label(t)
        return {"key": "unres|%s|%s" % (lab, ref[3]), "what": REASONS.get(ref[3], ref[3]), "label": lab,
                "inst": (sid, ref[2]), "dur": t.get("dur") if t else None, "via": via}
    if kind == "req":
        r = S["requests"].get(ref[2]) if S else None
        model = (r or {}).get("model") or "unknown model"
        where = "main session" if via == "main session" else "subagents"
        lab = "%s in %s" % (model, where)
        return {"key": "model|" + lab, "what": model, "label": lab, "inst": (sid, ref[2]),
                "dur": (r["end"] - r["start"]) if r else None, "via": via}
    t = S["tools"].get(ref[2]) if S else None
    if t is None:
        return {"key": "tool|?", "what": "unknown tool", "label": "unknown tool", "inst": None, "dur": None, "via": via}
    name = t["name"]
    desc = scrub(re.sub(r"\s+", " ", t.get("desc") or ""), 80)
    if name == "Bash" and t.get("cmd"):
        ck = (t["cmd"], cat)
        if cache is not None and ck in cache:
            sig = cache[ck]
        else:
            sig = signature(t["cmd"], rs, cat)
            if cache is not None:
                cache[ck] = sig
        key, part, pcls, matched = sig
        key = key or "shell command"
        # without a description the label is the command's signature; the raw part is noise ('curl -s -u ... -A ...')
        out = {"key": "bash|" + key, "what": key, "label": desc or key, "inst": (sid, ref[2]), "dur": _eff_dur(t, S), "via": via,
               "desc": desc, "est": capped(t)}
        if scratch_script(t["cmd"], key, part):
            out["scratch"] = True
        if "…/" in key:  # two sessions' 'scratchpad/build.sh' are two different scripts
            m = _SESSION_UUID.search(t["cmd"])
            if m:
                out["key"] = "bash|%s\t%s" % (key, m.group(1))
        if pcls and not matched and cat != "poll_wait" and not cat.startswith("human_wait") and pcls != cat:
            out["cat"] = pcls  # e.g. 'git status; ./long_job.sh' was classed git by its first word; the job is the time
        if cat == "poll_wait" and S:
            wf = waited_for(t, S, rs, cache)
            if not wf and pcls in _FALLBACK_CLS and not key.startswith(("until ", "while ")):
                wf = key  # launched a test/build and slept on it: the wait is for that run
            if wf:
                out["waited_for"] = wf
                out["key"] = "poll|" + wf  # loops with different conditions that wait for the same run are one item
        return out
    if name.startswith("mcp__"):
        server, tool = mcp_parts(name)
        what = "%s %s" % (re.sub(r"^claude_ai_", "", server), tool)
    elif name in ("WebFetch",) and t.get("target"):
        h = _host(t["target"])
        what = "WebFetch %s" % h if h else "WebFetch"
    else:
        what = name
    what = scrub(what, 90)
    return {"key": "tool|" + what, "what": what, "label": desc or what, "inst": (sid, ref[2]), "dur": _eff_dur(t, S), "via": via, "desc": desc,
            "est": capped(t)}


def run_key(cat, key):
    return "%s\t%s" % (cat, key)


def drill_analyses(analyses, rs, win=(None, None), idle_threshold=180.0):
    """All groups -> list of described leaves + totals. Each leaf: dict(cat, seconds, key, what, label, inst, dur, via, group).

    runs: run_key(cat, key) -> durations of EVERY run with that key and leaf category in the window (tools by
    their class, model requests under 'inference'), not only the runs that won seconds: an item's run count
    and median come from here. runs_by_via: the same per spawner (run_key + '\t' + via), for items that group
    one task's one-off scripts. run_groups: the main sessions (groups) each run key and run key + via ran in,
    so an item's session count covers the same runs as its run count. capped_s: leaf seconds of background commands that never reported their end
    (counted until session end or the cap)."""
    out = {"leaves": [], "autonomous": 0.0, "unresolved": {}, "by_cat": {}, "runs": {}, "runs_by_via": {}, "run_groups": {}, "capped_s": 0.0}
    cache = {}
    runs = out["runs"]
    vruns = out["runs_by_via"]  # the same runs per spawner, for items that group one task's scripts
    rgroups = out["run_groups"]  # run key (or run key + via) -> main sessions where it ran
    for a in analyses:
        d = drill_group(a, rs, win, idle_threshold)
        out["autonomous"] += d["autonomous"]
        for k, v in d["unresolved"].items():
            out["unresolved"][k] = out["unresolved"].get(k, 0.0) + v
        for k, v in d["by_cat"].items():
            out["by_cat"][k] = out["by_cat"].get(k, 0.0) + v
        sess = {S["session_id"]: S for S in a["sessions"]}
        labels = spawner_labels(a)
        gid = a["group"]["session_id"]
        for (cat, ref), sec in d["leaves"].items():
            info = describe(cat, ref, sess, labels, rs, cache)
            if info is None:
                continue
            info.setdefault("cat", cat)
            info.update({"seconds": sec, "group": gid})
            if info.get("est"):
                out["capped_s"] += sec
            out["leaves"].append(info)
        for S in a["sessions"]:
            sid = S["session_id"]
            for tid in S["tool_order"]:
                t = S["tools"][tid]
                if t["name"] in ("Agent", "Task", "Workflow"):
                    continue
                info = describe(t["cls"], ("tool", sid, tid), sess, labels, rs, cache)
                if info:
                    rk = run_key(info.get("cat") or t["cls"], info["key"])
                    runs.setdefault(rk, []).append(info.get("dur"))
                    vruns.setdefault(rk + "\t" + info["via"], []).append(info.get("dur"))
                    rgroups.setdefault(rk, set()).add(gid)
                    rgroups.setdefault(rk + "\t" + info["via"], set()).add(gid)
            for rid, r in S["requests"].items():
                info = describe("inference", ("req", sid, rid), sess, labels, rs, cache)
                if info:
                    rk = run_key("inference", info["key"])
                    runs.setdefault(rk, []).append(info.get("dur"))
                    rgroups.setdefault(rk, set()).add(gid)
    return out
