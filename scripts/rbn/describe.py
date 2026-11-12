"""Short human names (2-5 words) for 'What exactly' items, from rules/describe.json and the agent's own
descriptions. Deterministic: same item, same rules -> same name. No model involved."""
import re

_STOP_END = {"a", "an", "the", "to", "of", "for", "and", "or", "with", "on", "in", "at", "by", "from", "into", "then",
             "until", "via", "is", "are", "be", "that", "this", "its", "it", "&", "-", "—", "+"}
_CLAUSE = re.compile(r",\s+|;\s+|\s+[—–]\s+|\s+-\s+|:\s+|\s+\(|\s+then\s+|\s+and then\s+|\.\s+")
_WITH = re.compile(r"\s+(?:with|and|so|because|to make|to see|after|before|while|using|via)\s+", re.I)


def _cap(text, max_words, max_chars):
    words = re.sub(r"\s+", " ", str(text or "")).strip().split(" ")
    words = [w for w in words if w]
    words = words[:max_words]
    while len(words) > 1 and words[-1].lower().strip(",.;:") in _STOP_END:
        words.pop()
    out = " ".join(words).rstrip(" ,.;:")
    while len(out) > max_chars and " " in out:  # drop whole words first: a name, not a clipped sentence
        out = out[: out.rfind(" ")].rstrip(" ,.;:")
        while out.split(" ")[-1].lower() in _STOP_END and " " in out:
            out = out[: out.rfind(" ")]
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip(" ,.;:") + "…"
    return out[:1].upper() + out[1:] if out else out


def shorten_desc(desc, max_words=5, max_chars=38):
    """'Wait for current deploy, then deploy the sign-in fix' -> 'Wait for current deploy'. First clause,
    then the part before 'with/and/...' when the clause is still longer than max_words."""
    d = re.sub(r"\s+", " ", str(desc or "")).strip().rstrip(".")
    if not d:
        return ""
    first = _CLAUSE.split(d)[0].strip() or d
    if len(first.split()) > max_words:
        head = _WITH.split(first)[0].strip()
        if len(head.split()) >= 2:
            first = head
    return _cap(first, max_words, max_chars)


def _fill(text, m, cfg, seg, depth):
    def rep(mm):
        g, f = int(mm.group(1)), mm.group(2)
        try:
            v = m.group(g) or ""
        except IndexError:
            return ""
        if f == "title":
            v = v[:1].upper() + v[1:]
        elif f == "words":
            v = re.sub(r"[_-]+", " ", v).strip()
        elif f == "desc":
            v = (_match(cfg, v, v, seg, depth + 1) or re.sub(r"^Run ", "", _match(cfg, v, v, seg, depth + 1, "fallback"))
                 or v)
            v = v[:1].lower() + v[1:] if len(v) > 1 and v[1:2].islower() else v
        return v
    return re.sub(r"\{(\d+)(?::(\w+))?\}", rep, text)


def _match(cfg, what, label, seg, depth=0, kind="specific"):
    if depth > 2:
        return ""
    for r in cfg.get(kind) or ():
        rx = r.get("_rx")
        if rx is None or (r.get("segment") and r["segment"] != seg):
            continue
        m = rx.search(label if r.get("on") == "label" else what)
        if m:
            return _fill(r.get("text") or "", m, cfg, seg, depth)
    return ""


def _same(label, what):
    """The label is the command itself (maybe cut differently), not a description."""
    a, b = label.rstrip("…").strip(), what.rstrip("…").strip()
    n = min(len(a), len(b), 24)
    return a == b or (n >= 8 and a[:n] == b[:n])


def describe(rs, what, label="", desc="", seg=""):
    """-> (short name, source): source 'rule', 'agent' (the agent's own description) or 'command'."""
    cfg = getattr(rs, "describe", None) or {}
    mw, mc = int(cfg.get("max_words", 5)), int(cfg.get("max_chars", 38))
    what, label = str(what or ""), str(label or "")
    s = _match(cfg, what, label, seg)
    if s:
        return _cap(s, mw, mc), "rule"
    own = desc or (label if label and not _same(label, what) else "")
    if own:
        return shorten_desc(own, mw, mc), "agent"
    s = _match(cfg, what, label, seg, kind="fallback")
    if s:
        return _cap(s, mw, mc), "rule"
    return _cap(" ".join(what.split()[:2]) or "?", mw, mc), "command"


def dedupe(rs, items):
    """Two items of one slice must not share a name: a later one takes its agent's description, else gets the
    first word of its command ('Typecheck (tsc) · npx')."""
    cfg = getattr(rs, "describe", None) or {}
    mw, mc = int(cfg.get("max_words", 5)), int(cfg.get("max_chars", 38))
    seen = set()
    for it in items:
        name = it.get("short") or ""
        if name.lower() in seen:
            lab, what = str(it.get("label") or ""), str(it.get("what") or "")
            alt = shorten_desc(lab, mw, mc) if lab and not _same(lab, what) else ""
            if alt and alt.lower() not in seen:
                it["short"], it["short_src"] = alt, "agent"
            else:
                it["short"] = "%s · %s" % (name, (what.split() or ["?"])[0])
        seen.add(it["short"].lower())
    return items
