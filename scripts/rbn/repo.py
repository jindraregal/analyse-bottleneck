"""Read-only look at the project on disk, so a rule does not advise what the project already does.

A stack rule matches command text only. Some advice is already in force through a config file ("incremental" in a
tsconfig, also when inherited through "extends"; prefer-offline in .npmrc) or through a version (Turbopack is the
default from Next 16). `detector.skip_if_project` names such checks; a matched call whose project passes one is
dropped. When the directory of a call is unknown or gone (other machine, deleted worktree) nothing is dropped and the
rule reports as before. Files are only read (never run), and nothing from them reaches the output except the reason.
"""
import fnmatch
import json
import os
import re

from .util import match_text, raw_text

_PART_SEP = re.compile(r"&&|\|\||[;|\n]")
_ARG = r"(\"[^\"]+\"|'[^']+'|[^\s;&|)]+)"
_CD = re.compile(r"^[\s(]*cd\s+" + _ARG)
_DIR_OPT = re.compile(r"(?:^|\s)(?:-C|--dir|--cwd|--prefix)[=\s]+" + _ARG)
_PKG_OPT = re.compile(r"(?:^|\s)(?:--filter|-F|-w|--workspace)[=\s]+" + _ARG + r"|\bworkspace\s+" + _ARG)
_TS_PROJECT = re.compile(r"(?:^|\s)(?:-p|--project)[=\s]+" + _ARG)
_TS_FILE_ARG = re.compile(r"(?:^|\s)[\w@./-]+\.(?:tsx?|mts|cts)(?=\s|$|[;&|)])")
_SKIP_DIRS = {"node_modules", "dist", "build", "out", "target", "vendor", "coverage"}
_MAX_BYTES = 1 << 20
_cache = {}


def reset():
    _cache.clear()


def _unq(s):
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" else s


def _join(base, p):
    p = os.path.expanduser(_unq(p))
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))


def _read(path):
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _strip_jsonc(txt):
    """JSON with // and /* */ comments and trailing commas (what tsconfig allows) -> plain JSON text."""
    out = []
    i, n = 0, len(txt)
    while i < n:
        c = txt[i]
        if c == '"':
            j = i + 1
            while j < n and txt[j] != '"':
                j += 2 if txt[j] == "\\" else 1
            out.append(txt[i:j + 1])
            i = j + 1
        elif txt.startswith("//", i):
            j = txt.find("\n", i)
            i = n if j < 0 else j
        elif txt.startswith("/*", i):
            j = txt.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _json(path, jsonc=False):
    key = ("json", path)
    if key not in _cache:
        txt = _read(path)
        data = None
        if txt is not None:
            try:
                data = json.loads(_strip_jsonc(txt.lstrip("﻿")) if jsonc else txt)
            except ValueError:
                data = None
        _cache[key] = data if isinstance(data, dict) else None
    return _cache[key]


def _up(d, limit=12):
    """d and its parents, up to the repository root (the directory that holds .git)."""
    for _ in range(limit):
        yield d
        if os.path.exists(os.path.join(d, ".git")):
            return
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def _packages(root):
    """package name -> directory, for the workspace packages under root (bounded walk)."""
    key = ("pkgs", root)
    if key in _cache:
        return _cache[key]
    found = {}
    todo = [(root, 0)]
    seen = 0
    while todo and seen < 3000:
        d, depth = todo.pop()
        seen += 1
        pj = _json(os.path.join(d, "package.json"))
        if pj and isinstance(pj.get("name"), str):
            found.setdefault(pj["name"], d)
        if depth >= 4:
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for nm in names:
            if nm.startswith(".") or nm in _SKIP_DIRS:
                continue
            p = os.path.join(d, nm)
            if os.path.isdir(p) and not os.path.islink(p):
                todo.append((p, depth + 1))
    _cache[key] = found
    return found


_WORKTREE = re.compile(r"^(.*?)/\.claude/worktrees/[^/]+(?:/(.*))?$")


def _existing(d):
    """(d, False) when the directory exists; (the same path in the main checkout, True) when d was inside a Claude
    Code worktree (<repo>/.claude/worktrees/<name>/...) that is gone; (None, True) otherwise."""
    if os.path.isdir(d):
        return d, False
    m = _WORKTREE.match(d)
    if m:
        p = os.path.normpath(os.path.join(m.group(1), m.group(2) or ""))
        if os.path.isdir(p):
            return p, True
    return None, True


def event_dir(cmd, rx, cwd):
    """(directory the matched tool ran in, text after the tool name, approximate?) for the first part of a chained
    command that rx matches; directory None when it is unknown. Follows `cd X &&` earlier in the chain and the
    package-manager selectors in front of the tool (-C/--dir/--cwd/--prefix DIR, --filter/-F/-w NAME, yarn workspace
    NAME). A directory that no longer exists (deleted worktree, temp clone) falls back to the same path in the main
    checkout, then to the call's own working directory: the advice is about the project's config, so approximate=True."""
    if not cwd:
        return None, "", False
    cds, hit = [], None
    for part in _PART_SEP.split(raw_text(cmd or "")):
        c = _CD.match(part)
        if c:
            cds.append(c.group(1))  # match_text drops a leading 'cd X &&', so the tool may sit in the same part
        if part.strip() and rx.search(match_text(part)):
            hit = part
            break
    if hit is None:
        return None, "", False
    m = rx.search(hit)
    before, after = (hit[:m.start()], hit[m.end():]) if m else ("", "")
    moved = cwd
    for c in cds:
        moved = _join(moved, c)
    d, approx = _existing(moved)
    if d is None:
        d = _existing(cwd)[0]
    if d is None:
        return None, "", False
    for o in _DIR_OPT.finditer(before):
        d = _existing(_join(d, o.group(1)))[0] or d
    for o in _PKG_OPT.finditer(before):
        raw = _unq(o.group(1) or o.group(2))
        name = raw if raw.startswith(("./", "../", "/")) else raw.strip(".").strip("{}")  # 'pkg...', '...pkg', '{./dir}'
        if os.path.isdir(_join(d, name)):
            d = _join(d, name)
        elif name in _packages(d):
            d = _packages(d)[name]
    return d, after, approx


# --- tsconfig ---

def _resolve_extends(spec, d):
    if not isinstance(spec, str) or not spec:
        return None
    if spec.startswith(".") or os.path.isabs(spec):
        base = _join(d, spec)
        cands = [base, base + ".json", os.path.join(base, "tsconfig.json")]
    else:
        cands = []
        u = d
        for _ in range(25):
            nm = os.path.join(u, "node_modules", spec)
            cands += [nm, nm + ".json"]
            if os.path.isdir(nm):
                pj = _json(os.path.join(nm, "package.json")) or {}
                if isinstance(pj.get("tsconfig"), str):
                    cands.append(os.path.join(nm, pj["tsconfig"]))
                cands.append(os.path.join(nm, "tsconfig.json"))
            parent = os.path.dirname(u)
            if parent == u:
                break
            u = parent
    return next((c for c in cands if os.path.isfile(c)), None)


def ts_effective(path, _seen=None):
    """compilerOptions of a tsconfig after `extends` (a string or a list; relative paths and packages in
    node_modules), the way tsc merges them: the file itself wins over what it extends, a later entry of an
    `extends` list over an earlier one. outDir and tsBuildInfoFile come back absolute (they are relative to the
    file that sets them). '_own' holds the option names the file sets itself."""
    seen = _seen if _seen is not None else set()
    path = os.path.normpath(path)
    if path in seen or len(seen) > 20:
        return {}
    seen.add(path)
    cfg = _json(path, jsonc=True)
    if cfg is None:
        return {}
    out = {}
    ext = cfg.get("extends")
    for e in (ext if isinstance(ext, list) else [ext]):
        p = _resolve_extends(e, os.path.dirname(path))
        if p:
            out.update(ts_effective(p, seen))
    co = cfg.get("compilerOptions")
    co = dict(co) if isinstance(co, dict) else {}
    for k in ("outDir", "tsBuildInfoFile"):
        if isinstance(co.get(k), str):
            co[k] = _join(os.path.dirname(path), co[k])
    out.update(co)
    out["_own"] = set(co)
    return out


def _tsconfig_of(d, after):
    """The tsconfig a `tsc` call in directory d uses: -p/--project, else the nearest tsconfig.json upwards.
    None when the call names source files (tsc then ignores tsconfig) or no config is found."""
    m = _TS_PROJECT.search(after)
    if m:
        p = _join(d, m.group(1))
        if os.path.isdir(p):
            p = os.path.join(p, "tsconfig.json")
        return p if os.path.isfile(p) else None
    if _TS_FILE_ARG.search(after):
        return None
    for u in _up(d):
        p = os.path.join(u, "tsconfig.json")
        if os.path.isfile(p):
            return p
    return None


def _has_glob(d, pat):
    try:
        return any(fnmatch.fnmatch(n, pat) for n in os.listdir(d))
    except OSError:
        return False


def _check_tsconfig(chk, d, after):
    path = _tsconfig_of(d, after)
    if not path:
        return None
    eff = ts_effective(path)
    for k in chk.get("true", []):
        if eff.get(k) is True:
            return 'tsconfig sets "%s"%s' % (k, "" if k in eff.get("_own", ()) else " (through extends)")
    cfg = _json(path, jsonc=True) or {}
    for ref in (cfg.get("references") if isinstance(cfg.get("references"), list) else []):
        rp = ref.get("path") if isinstance(ref, dict) else None
        if not isinstance(rp, str):
            continue
        rp = _join(os.path.dirname(path), rp)
        if os.path.isdir(rp):
            rp = os.path.join(rp, "tsconfig.json")
        reff = ts_effective(rp)
        for k in chk.get("true", []):
            if reff.get(k) is True:
                return 'a referenced project sets "%s"' % k
    pat = chk.get("artifact")
    if pat:
        bi = eff.get("tsBuildInfoFile")
        dirs = [os.path.dirname(path)] + ([eff["outDir"]] if isinstance(eff.get("outDir"), str) else [])
        if (isinstance(bi, str) and os.path.isfile(bi)) or any(_has_glob(x, pat) for x in dirs):
            return "a %s file exists" % pat
    return None


# --- other checks ---

def _check_file_regex(chk, d, after):
    try:
        rx = re.compile(chk["regex"], re.I | re.M)
    except (re.error, KeyError):
        return None
    for name in chk.get("files", []):
        for u in _up(d):
            txt = _read(os.path.join(u, name))
            if txt is not None and rx.search(txt):
                return "%s already sets it" % name
    return None


def _check_dep_min_major(chk, d, after):
    for name, want in chk.items():
        spec = None
        for u in _up(d):  # the nearest package.json that names the dependency decides
            pj = _json(os.path.join(u, "package.json")) or {}
            spec = next((pj[s][name] for s in ("dependencies", "devDependencies")
                         if isinstance(pj.get(s), dict) and isinstance(pj[s].get(name), str)), None)
            if spec is not None:
                break
        m = re.match(r"^[\s^~>=v]*(\d+)(?:\.|$|\s)", spec or "")
        if m and int(m.group(1)) >= want:
            return "package.json has %s %s" % (name, spec.strip())
    return None


_CHECKS = (("tsconfig", _check_tsconfig), ("file_regex", _check_file_regex), ("dep_min_major", _check_dep_min_major))


def already_done(rule, cmd, cwd):
    """Why the project already does what the rule advises for this call (a short reason), or None."""
    spec = (rule.get("detector") or {}).get("skip_if_project")
    if not isinstance(spec, dict) or rule.get("_rx") is None:
        return None
    key = ("done", rule.get("id"), cmd, cwd)
    if key not in _cache:
        reason = None
        d, after, approx = event_dir(cmd, rule["_rx"], cwd)
        if d:
            for name, fn in _CHECKS:
                if isinstance(spec.get(name), dict):
                    reason = fn(spec[name], d, after)
                    if reason:
                        reason += "; call directory is gone, the project's checkout was checked" if approx else ""
                        break
        _cache[key] = reason
    return _cache[key]
