"""Small helpers: timestamps, text, command sanitising, formatting."""
import os
import re
from datetime import datetime, timezone

_HOME = os.path.expanduser("~")


def parse_ts(s):
    """ISO-8601 string -> epoch seconds (float) or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(spec, now=None):
    """'2026-10-30', '2026-10-30T12:00', '24h', '7d', '90m' -> epoch seconds."""
    if spec is None:
        return None
    spec = spec.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])", spec)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return (now if now is not None else datetime.now(timezone.utc).timestamp()) - float(m.group(1)) * mult
    t = parse_ts(spec if "T" in spec else spec + "T00:00:00")
    if t is None:
        raise ValueError("cannot parse time: %r" % spec)
    return t


def est_tokens(chars, cpt=4):
    return int(chars / cpt) if chars else 0


def fmt_dur(sec):
    sec = float(sec or 0)
    if sec < 90:
        return "%ds" % round(sec)
    if sec < 5400:
        return "%.1fm" % (sec / 60)
    return "%.1fh" % (sec / 3600)


def fmt_int(n):
    n = int(n or 0)
    if abs(n) >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if abs(n) >= 10_000:
        return "%.0fk" % (n / 1e3)
    return str(n)


def redact(text, limit=None):
    """Replace the home directory with ~ and optionally truncate."""
    if not isinstance(text, str):
        return text
    if _HOME and _HOME != "/":
        text = text.replace(_HOME, "~")
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)
_HEREDOC_OPEN = re.compile(r"<<-?\s*['\"]?\w+['\"]?.*$", re.S)
_DQ = re.compile(r'"(?:\\.|[^"\\])*"', re.S)
_SQ = re.compile(r"'[^']*'", re.S)


def sanitize_command(cmd):
    """Drop heredoc bodies and quoted strings so words inside them do not classify the command."""
    if not cmd:
        return ""
    c = _HEREDOC.sub(" ", cmd)
    c = _HEREDOC_OPEN.sub(" ", c)
    c = _DQ.sub('""', c)
    c = _SQ.sub("''", c)
    return c


_CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*(&&|;)\s*")
_HEX = re.compile(r"\b[0-9a-f]{7,}\b")
_NUM = re.compile(r"\d+")
_PATH = re.compile(r"(?:~|/)[\w@%+=:,.\-/]+")


def strip_cd(cmd):
    return _CD_PREFIX.sub("", cmd or "")


def norm_command(cmd, limit=160):
    """Stable key for 'same command' detection: no cd prefix, numbers/hashes masked."""
    c = strip_cd(sanitize_command(cmd)).strip()
    c = re.sub(r"\s+", " ", c)
    c = _HEX.sub("#", c)
    c = _NUM.sub("#", c)
    return c[:limit]


def template_command(cmd, limit=120):
    """Coarser key: paths, strings and numbers masked, to find 'same loop body, different argument'."""
    c = strip_cd(sanitize_command(cmd)).strip()
    c = _PATH.sub("<p>", c)
    c = _HEX.sub("#", c)
    c = _NUM.sub("#", c)
    c = re.sub(r"\s+", " ", c)
    return c[:limit]


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


_DATA_EXT = r"(?:log|txt|out|err|json|jsonl|md|csv|tsv|ya?ml|png|jpe?g|gif|webp|mp4|mp3|wav|webm|mov|m4a|pdf|html?|xml|lock|tmp|pid|sock|sql|db|zip|gz|tgz|srt|vtt)"
_DATA_WORD = re.compile(r"(?<![\w$-])[\w.~$/@%+=:,-]*\." + _DATA_EXT + r"(?=[\s;&|()<>\"']|$)", re.I)


def strip_data_paths(s):
    """Replace data-file words (e.g. /tmp/x/vitest-run.log) so tool names inside file names do not match."""
    return _DATA_WORD.sub("<f>", s)


def match_text(cmd):
    """Text that detector/classifier regexes run on: no heredocs, no quoted strings, no data-file names."""
    return strip_data_paths(strip_cd(sanitize_command(cmd)))


def raw_text(cmd):
    """Heredoc bodies removed but quoted strings kept (for loops hidden in bash -c '...')."""
    c = _HEREDOC.sub(" ", cmd or "")
    return _HEREDOC_OPEN.sub(" ", c)


# Secrets in command text and labels (shared by the call ledger, the view and stack-rule examples)
_SECRETS = [
    (re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|token|basic)?\s*)[^\s'\"]+"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1<redacted>"),
    (re.compile(r"(?i)\b([A-Z0-9_.-]*(?:token(?!s\b)|secret|passw(?:or)?d|api[_-]?key|apikey|credential)[A-Z0-9_.-]*\s*[=:]\s*)(['\"]?)[^\s'\"$][^\s'\"]*"), r"\1\2<redacted>"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_\w{12,}|xox[a-z]-[\w-]{10,}|AKIA[0-9A-Z]{12,}|AIza[\w-]{20,}|eyJ[\w-]{15,}\.[\w-]{10,}\.[\w-]*)"), "<redacted>"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s]+@"), r"\1<redacted>@"),
    (re.compile(r"(?i)(\\?[\"']?(?:password|passwd|pwd|token|secret|api_?key|code)\\?[\"']?\s*:\s*\\?[\"'])[^\"'\\]+"), r"\1<redacted>"),
    (re.compile(r"(\s-u\s+['\"]?[^\s:'\"]+:)[^\s'\"]+"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:password|passwd|pwd|token|secret|key)=)[^\s&'\"]+"), r"\1<redacted>"),
]


def scrub(text, limit=None):
    if not text:
        return ""
    text = redact(text)
    for rx, rep in _SECRETS:
        text = rx.sub(rep, text)
    return text[: limit - 1] + "…" if limit and len(text) > limit else text
