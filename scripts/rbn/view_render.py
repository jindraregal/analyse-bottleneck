"""Render a run-bottleneck/view@1 dict as one self-contained HTML page: a clickable donut of agent time.

Stdlib only and import-free from the rest of the package, so it also runs as a script:

    python3 scripts/rbn/view_render.py view.json [--notes notes.json] [-o index.html]

Everything visible is computed here from the view; the page's script only switches the selected slice and
shows the hover tooltip. The legend names the slices (no labels around the ring).
"""
import copy
import html
import json
import math
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "view_template.html")

MAX_ITEMS = 6
MAX_TODO = 6
MAX_TASKS = 6  # view.build_tasks: 4, plus up to 2 bigger ones so the list stays biggest first
TASKS_SHOW_BELOW = 0.70

# Donut geometry in SVG user units; the viewBox is the ring plus room for the selected slice to pop out.
W, H = 600, 320
CX, CY = W / 2, H / 2
RO, RI = 120.0, 80.0
GAP = 2.0
POP = 7.0

# Each slice id keeps its hue in every report (blue is always Tests). Slots follow the validated palette order;
# ids that share a slot only meet when both are visible, then the later one takes the next free slot.
_SLOT_BY_ID = {"tests": 1, "deploys": 2, "poll": 3, "model": 4, "scripts": 5, "builds": 6, "network": 7, "media": 8,
               "installs": 6, "browser": 8, "servers": 7, "files": 5, "approvals": 2}
KIND_LABEL = {"deterministic": "Deterministic", "measured": "Measured", "llm": "Interpreted by AI", "missing": "Missing",
              "none": "None"}
KIND_HELP = [
    ("deterministic", "computed by a script from transcripts, same input → same output."),
    ("measured", "read from host telemetry or logs, not inferred."),
    ("llm", "written by a model reading the computed numbers; check against the items above."),
    ("missing", "not recorded, so it cannot be counted or checked."),
]
_EXCLUDED_SHORT = {"human": "waiting for you", "idle": "idle"}


# ---------- small helpers ----------

def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def num(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def fmt_h(sec):
    sec = num(sec)
    if sec >= 3570:
        return "%.1f h" % (sec / 3600)
    if sec >= 59.5:
        return "%d min" % round(sec / 60)
    if 0 < sec < 0.5:
        return "<1 s"  # a 0.1-s median is not 'median 0 s' (same as view.fmt_h)
    return "%d s" % round(sec)


def fmt_pct(p):
    p = num(p)
    if p <= 0:
        return "0%"
    if p < 0.1:
        return "<0.1%"
    if p < 1:
        return "%.1f%%" % p
    return "%d%%" % round(p)


def fmt_saving(minutes):
    m = num(minutes, None)
    if m is None or m <= 0:
        return ""
    if m >= 90:
        return "saves %.1f h" % (m / 60)
    if m >= 1:
        return "saves %d min" % round(m)
    return "saves <1 min"


def fmt_can_save(minutes):
    """The 'Can save' column, the chart centre and panel heads: '≈1.9 h', '≈25 min'; '–' without an estimate."""
    m = num(minutes)
    return ("≈" + fmt_h(m * 60)) if m >= 1 else "–"


def seg_saving_min(s):
    """A slice's possible saving: its rule estimates added up, at most the slice's own time (tips can overlap)."""
    sv = sum(num(a.get("saving_min")) for a in s.get("actions") or [] if isinstance(a, dict))
    return min(sv, num(s.get("seconds")) / 60.0)


def clip(text, limit):
    text = re.sub(r"\s+", " ", "" if text is None else str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_window(since, until):
    a, b = _dt(since), _dt(until)
    if not a or not b:
        return ""
    d = lambda t: "%d %s" % (t.day, t.strftime("%b"))  # noqa: E731
    if (b - a).total_seconds() < 36 * 3600:
        return "%s %d, %s–%s UTC" % (d(a), a.year, a.strftime("%H:%M"), b.strftime("%H:%M"))
    if a.year != b.year:
        return "%s %d – %s %d" % (d(a), a.year, d(b), b.year)
    if a.month == b.month:
        return "%d–%d %s %d" % (a.day, b.day, b.strftime("%b"), b.year)
    return "%s – %s %d" % (d(a), d(b), b.year)


def fmt_period(a, b):
    """'3–10 Nov', '28 Oct – 4 Nov', '10 Nov' (the year goes to the label)."""
    if a.date() == b.date():
        return "%d %s" % (a.day, a.strftime("%b"))
    if a.month == b.month and a.year == b.year:
        return "%d–%d %s" % (a.day, b.day, b.strftime("%b"))
    return "%d %s – %d %s" % (a.day, a.strftime("%b"), b.day, b.strftime("%b"))


def seg_kind(seg):
    sid = str(seg.get("id") or "").lower()
    if "unknown" in sid:
        return "unknown"
    if sid == "other" or sid.startswith("other"):
        return "other"
    return "hue"


def colour_classes(segs):
    """CSS class per segment index: a fixed c1..c8 per slice id (same colour in every report), co = other,
    cu = unknown (hatched); a slot already taken on this page goes to the next free one, then a neutral."""
    out, used = {}, set()
    hued = [i for i, s in enumerate(segs) if seg_kind(s) == "hue"]
    later = []
    for i in hued:
        slot = _SLOT_BY_ID.get(str(segs[i].get("id")))
        if slot and slot not in used:
            out[i] = "c%d" % slot
            used.add(slot)
        else:
            later.append(i)
    free = [k for k in range(1, 9) if k not in used]
    for i in later:
        out[i] = "c%d" % free.pop(0) if free else "cx"
    for i, s in enumerate(segs):
        if i not in out:
            out[i] = "cu" if seg_kind(s) == "unknown" else "co"
    return out


# ---------- notes ----------

def merge_notes(view, notes):
    """Return a copy of `view` with LLM notes attached to their segments (source "llm").

    The deterministic headline is never replaced; a notes headline is kept apart as `notes_headline`.
    Notes for unknown segment ids are dropped and listed in `notes_unmatched`. Merging twice does not
    duplicate notes (same title in the same segment is skipped).
    """
    out = copy.deepcopy(view if isinstance(view, dict) else {})
    if not isinstance(notes, dict):
        return out
    by_id = {str(s.get("id")): s for s in out.get("segments") or [] if isinstance(s, dict)}
    if isinstance(out.get("waiting"), dict):
        by_id.setdefault("waiting", out["waiting"])  # notes on how to wait on the person less
    for dd in out.get("deep_dives") or []:
        if isinstance(dd, dict) and dd.get("id"):
            by_id.setdefault("deepdive:%s" % dd["id"], dd)  # the deep-dive subagent's notes
    unmatched = []
    for sid, lst in (notes.get("segments") or {}).items():
        seg = by_id.get(str(sid))
        if seg is None:
            unmatched.append(str(sid))
            continue
        seg_notes = seg.get("notes")
        if not isinstance(seg_notes, list):
            seg_notes = seg["notes"] = []
        have = {n.get("title") for n in seg_notes if isinstance(n, dict)}
        for n in lst if isinstance(lst, list) else []:
            if not isinstance(n, dict) or not str(n.get("title") or "").strip():
                continue
            title = clip(n.get("title"), 70)
            if title in have:
                continue
            ev = n.get("evidence")
            ev = [clip(e, 200) for e in (ev if isinstance(ev, list) else [ev] if ev else []) if str(e).strip()][:5]
            seg_notes.append({"title": title, "detail": clip(n.get("detail"), 300), "evidence": ev, "source": "llm"})
            have.add(title)
    model = clip(notes.get("model"), 60)
    if notes.get("headline"):
        out["notes_headline"] = clip(notes.get("headline"), 160)
    if model:
        out["notes_model"] = model
    if unmatched:
        out["notes_unmatched"] = sorted(unmatched)
    basis = out.get("basis")
    if not isinstance(basis, list):
        basis = out["basis"] = []
    # the computed view says 'AI notes: none on this page'; with notes merged that row would contradict them
    basis[:] = [b for b in basis if not (isinstance(b, dict) and b.get("what") == "AI notes" and b.get("kind") in ("missing", "none"))]
    if not any(isinstance(b, dict) and b.get("kind") == "llm" for b in basis):
        basis.append({"what": "Notes marked AI", "how": "%s read this view and wrote short notes" % (model or "A model"),
                      "kind": "llm", "detail": "Not computed. Each note cites its evidence; check it against the "
                                               "items listed for that slice before acting on it."})
    return out


# ---------- donut ----------

def _pt(r, a):
    return CX + r * math.sin(a), CY - r * math.cos(a)


def _f(v):
    s = "%.2f" % v
    return s.rstrip("0").rstrip(".") if "." in s else s


def slice_path(a0, a1):
    span = a1 - a0
    if span >= 2 * math.pi - 1e-6:
        return ("M{cx},{t1}A{ro},{ro} 0 1 1 {cx},{b1}A{ro},{ro} 0 1 1 {cx},{t1}Z"
                "M{cx},{t2}A{ri},{ri} 0 1 0 {cx},{b2}A{ri},{ri} 0 1 0 {cx},{t2}Z").format(
            cx=_f(CX), ro=_f(RO), ri=_f(RI), t1=_f(CY - RO), b1=_f(CY + RO), t2=_f(CY - RI), b2=_f(CY + RI))
    po = min(GAP / 2 / RO, span * 0.25)
    pi_ = min(GAP / 2 / RI, span * 0.25)
    o0, o1 = _pt(RO, a0 + po), _pt(RO, a1 - po)
    i1, i0 = _pt(RI, a1 - pi_), _pt(RI, a0 + pi_)
    lo = 1 if (span - 2 * po) > math.pi else 0
    li = 1 if (span - 2 * pi_) > math.pi else 0
    return "M%s,%sA%s,%s 0 %d 1 %s,%sL%s,%sA%s,%s 0 %d 0 %s,%sZ" % (
        _f(o0[0]), _f(o0[1]), _f(RO), _f(RO), lo, _f(o1[0]), _f(o1[1]),
        _f(i1[0]), _f(i1[1]), _f(RI), _f(RI), li, _f(i0[0]), _f(i0[1]))


def centre_all(agent_s, waiting_s=0.0):
    """Donut centre with nothing selected: agent time, and the waiting under it."""
    return ("Agent time", fmt_h(agent_s),
            ("+ %s waiting" % fmt_h(waiting_s)) if num(waiting_s) >= 60 else "all sessions added up")


def donut_svg(segs, saves, classes, ring_s, sel=None, agent_only_s=None, waiting_s=0.0):
    """The ring: agent slices plus the hatched waiting slice. sel=None draws the 'All' state (nothing picked).
    saves[i] = minutes slice i can save (seg_saving_min); a picked slice shows it under its time."""
    total = sum(max(num(s.get("seconds")), 0) for s in segs)
    paths, a = [], 0.0
    sel_v, sel_l, sel_ct = None, None, None
    for i, s in enumerate(segs):
        sec = max(num(s.get("seconds")), 0)
        if sec <= 0 or total <= 0:
            continue
        span = 2 * math.pi * sec / total
        a0, a1 = a, a + span
        a += span
        mid = (a0 + a1) / 2
        dx, dy = POP * math.sin(mid), -POP * math.cos(mid)
        lab = s.get("label") or s.get("id")
        sv = num(saves[i]) if i < len(saves) else 0.0
        ct = _ctr_saving(sv)
        tip = "%s · %s%s" % (clip(lab, 60), fmt_h(sec), (" · " + ct) if sv >= 1 else "")
        on = sel is not None and i == sel
        paths.append(
            '<path class="slice %s%s" d="%s" data-i="%d" style="--dx:%spx;--dy:%spx" role="button" tabindex="%d" '
            'aria-pressed="%s" aria-controls="rb-p%d" aria-label="%s" data-tip="%s" data-cv="%s" data-cl="%s" data-ct="%s"></path>' % (
                classes[i], " sel" if on else "", slice_path(a0, a1), i, _f(dx), _f(dy), 0 if (on or (sel is None and not paths)) else -1,
                "true" if on else "false", i, esc(_saving_aria(lab, sec, sv)),
                esc(tip), esc(fmt_h(sec)), esc(_ctr_label(lab)), esc(ct)))
        if on:
            sel_v, sel_l, sel_ct = fmt_h(sec), _ctr_label(lab), ct
    m = RO + POP + 5
    vb = "%s %s %s %s" % (_f(CX - m), _f(CY - m), _f(2 * m), _f(2 * m))
    if not paths:
        paths.append('<circle class="empty" cx="%s" cy="%s" r="%s"></circle>' % (_f(CX), _f(CY), _f((RO + RI) / 2)))
    agent_s = agent_only_s if agent_only_s is not None else ring_s
    l0, v0, t0 = centre_all(agent_s, waiting_s)
    sel_t = sel_ct or _ctr_total(ring_s, agent_only_s)
    return (
        '<svg class="donut%s" viewBox="%s" role="group" aria-label="Agent time by cause and waiting for you, %s in all" '
        'data-cl0="%s" data-cv0="%s" data-ct0="%s" data-ct="%s">'
        '<defs><pattern id="rb-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<rect class="hb" width="6" height="6"></rect><line class="hl" x1="1.5" y1="0" x2="1.5" y2="6"></line></pattern>'
        '<pattern id="rb-hatch-w" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">'
        '<rect class="wb" width="7" height="7"></rect><line class="wl" x1="2" y1="0" x2="2" y2="7"></line></pattern></defs>'
        '<g class="slices%s">%s</g>'
        '<circle class="hole" cx="%s" cy="%s" r="%s"><title>Show all</title></circle>'
        '<text class="ctr-l" x="%s" y="%s" text-anchor="middle">%s</text>'
        '<text class="ctr-v" x="%s" y="%s" text-anchor="middle">%s</text>'
        '<text class="ctr-t" x="%s" y="%s" text-anchor="middle">%s</text></svg>' % (
            "" if sel is not None else " all", vb, esc(fmt_h(ring_s)), esc(l0), esc(v0), esc(t0), esc(sel_t),
            " has-sel" if sel is not None else "", "".join(paths),
            _f(CX), _f(CY), _f(RI - 4),
            _f(CX), _f(CY - 22), esc(sel_l or l0),
            _f(CX), _f(CY + 9), esc(sel_v or v0),
            _f(CX), _f(CY + 30), esc(sel_t if sel_v else t0)))


def _ctr_total(ring_s, agent_s):
    """'of 6.8 h agent time', or 'of 18.6 h' when the ring also holds the waiting-for-you slice."""
    if agent_s is None or abs(num(ring_s) - num(agent_s)) < 1:
        return "of %s agent time" % fmt_h(ring_s)
    return "of %s in all" % fmt_h(ring_s)


def _ctr_label(label):
    """Selected slice in the donut centre, short enough for the hole."""
    return clip(label, 20)


def _ctr_saving(minutes):
    """Under a picked slice's time: what it can save, or that no rule has an estimate."""
    return ("%s to save" % fmt_can_save(minutes)) if num(minutes) >= 1 else "no saving estimate"


def _saving_aria(label, sec, minutes):
    return "%s: %s, %s" % (label, fmt_h(sec), ("about %s to save" % fmt_h(num(minutes) * 60)) if num(minutes) >= 1
                           else "no saving estimate")


# ---------- rows: one expandable row for every list on the page ----------

def short_via(via, subagents=True, n_tasks=None):
    """'subagent: Shard e2e suite (general-purpose) · main session' -> 'by Shard e2e suite +1'; '' for a lone
    'main session' when the window has no subagents (every row would say it). `via` names at most two spawners;
    `n_tasks` (all distinct tasks that ran the item) makes it '+52' rather than '+1' for an item spread over 53."""
    parts = [p.strip() for p in str(via or "").split(" · ") if p.strip()]
    if not parts:
        return ""
    first = parts[0]
    if first == "main session":
        if not subagents and len(parts) == 1:
            return ""
        txt = "main session"
    else:
        first = re.sub(r"^(?:subagent|workflow agent|workflow):\s*", "", first)
        first = re.sub(r"\s+\((?:general-purpose|[\w-]+)\)$", "", first)
        txt = "by " + first
    more = int(n_tasks) - 1 if n_tasks else len(parts) - 1
    return txt + (" +%d" % more if more > 0 else "")


def show_median(it):
    """A median only when it fits the total: N runs x median far above the counted time means the runs overlapped
    other counted work, and the pair would look impossible."""
    cnt, p50, sec = int(num(it.get("count"), 0)), it.get("p50_s"), num(it.get("seconds"))
    return cnt > 1 and p50 is not None and cnt * num(p50) <= 1.2 * sec


def _short(it):
    """The item's 2-5 word name (view.py, rules/describe.json); older views fall back to the label."""
    return clip(it.get("short") or it.get("label") or it.get("what") or "?", 60)


_FLAG = re.compile(r"(?<![\w-])(-{1,2}[A-Za-z][\w/-]*)")


def _nobreak_flags(escaped):
    """Keep '--quiet/--silent' on one line: browsers break after a hyphen."""
    return _FLAG.sub(r'<span class="nw">\1</span>', escaped)


def _meta(value="", count=0, saving=""):
    out = ""
    if saving:
        out += '<span class="sv">%s</span>' % esc(saving)
    if value:
        out += '<span class="hv">%s</span>' % esc(value)
    if count:
        out += '<span class="ct">×%d</span>' % count
    return out


def row(title, rows, lead="", meta="", title_attr=None, cls="", title_html=None):
    """THE row: every list on the page (Do this, What exactly, By task, Deep dive, What this is based on) uses it,
    so they all open the same way. The summary is lead · title · meta · chevron; the (label, html) rows open below.
    A row with nothing to show below still opens and says so, never a dead row."""
    body = "".join('<dt>%s</dt><dd>%s</dd>' % (esc(k), v) for k, v in rows if v)
    if not body:
        body = '<dt>Details</dt><dd>nothing more was recorded</dd>'
    tt = title_html if title_html is not None else esc(title)
    ta = ' title="%s"' % esc(title_attr) if title_attr else ""
    return ('<li class="rw%s"><details><summary class="sm">%s<span class="tt"%s>%s</span>%s</summary><dl>%s</dl></details></li>'
            % (" " + cls if cls else "", lead, ta, tt, meta, body))


def rows_list(rows_html, cls=""):
    return '<ul class="rows%s">%s</ul>' % (" " + cls if cls else "", "".join(rows_html))


def _item_html(it, subagents=True):
    label = clip(it.get("label") or it.get("what") or "?", 160)
    what = clip(it.get("what"), 200)
    wf = clip(it.get("waited_for"), 90)
    short = _short(it)
    cnt = int(num(it.get("count"), 0))
    est = it.get("est_s") is not None and num(it.get("est_s")) > 0
    hv = ("≤" if est else "") + fmt_h(it.get("seconds"))
    rows = []
    if what:
        rows.append(("Command", '<code>%s</code>' % esc(what)))
    if wf:
        rows.append(("Waits for", '<code>%s</code>' % esc(wf)))
    if label and label != what and label.lower() != short.lower():
        rows.append(("Agent's note", esc(label)))
    via_full = clip(it.get("via"), 240)
    if via_full and (subagents or via_full != "main session"):
        via = short_via(it.get("via"), subagents, it.get("n_tasks")) or via_full
        rows.append(("Run by", '<span title="%s">%s</span>' % (esc(via_full), esc(via))))
    if show_median(it):
        rows.append(("Median run", esc(fmt_h(it.get("p50_s")))))
    ses = int(num(it.get("sessions"), 0))
    if ses > 1:
        rows.append(("Sessions", "%d" % ses))
    if est:
        rows.append(("Upper bound", "includes background runs that never reported their end"))
    return row(short, rows, meta=_meta(hv, cnt), title_attr=short)


def _badge(kind):
    return '<span class="bd b-rule">rule</span>' if kind == "rule" else '<span class="bd b-ai">AI</span>'


def _action_rows(a):
    return [
        ("Why", esc(clip(a.get("why"), 600))),
        ("Evidence", esc(clip(a.get("evidence"), 600))),
        ("Arithmetic", '<code>%s</code>' % esc(clip(a.get("arithmetic"), 400)) if a.get("arithmetic") else ""),
        ("Before", esc(clip(a.get("before"), 300))),
        ("After", esc(clip(a.get("after"), 300))),
        ("Confidence", esc(a.get("confidence") or "")),
        ("Source", esc(clip(a.get("ref"), 300))),
    ]


def _note_rows(n):
    ev = n.get("evidence") or []
    ev_html = "".join("<li>%s</li>" % esc(e) for e in (ev if isinstance(ev, list) else [ev]))
    return [("Detail", esc(clip(n.get("detail"), 400))), ("Evidence", '<ul class="ev">%s</ul>' % ev_html if ev_html else "")]


def todo_entries(s, n_actions=3):
    """(kind, title, saving_min, rows) for a slice's rule actions, then its AI notes."""
    out = []
    for a in (s.get("actions") or [])[:n_actions]:
        if isinstance(a, dict) and a.get("title"):
            out.append(("rule", clip(a.get("title"), 90), num(a.get("saving_min"), 0.0), _action_rows(a)))
    for n in s.get("notes") or []:
        if isinstance(n, dict) and n.get("title"):
            out.append(("llm", clip(n.get("title"), 90), 0.0, _note_rows(n)))
    return out


def _todo_row(kind, title, saving_min, rows, lead_extra="", extra_rows=()):
    return row(title, list(extra_rows) + rows, lead=lead_extra + _badge(kind), meta=_meta(saving=fmt_saving(saving_min)),
               title_html=_nobreak_flags(esc(title)))


def _todos(s):
    return [_todo_row(*e) for e in todo_entries(s)][:MAX_TODO]


def show_tasks(s, items=None):
    """'By task' only where the listed items explain less than TASKS_SHOW_BELOW of the slice (view.py decides and
    stores tasks_show; older views without it fall back to the item share)."""
    tasks = [t for t in s.get("tasks") or [] if isinstance(t, dict)]
    if not tasks:
        return False
    if "tasks_show" in s:
        return bool(s.get("tasks_show"))
    sec = num(s.get("seconds"))
    listed = sum(num(it.get("seconds")) for it in (items if items is not None else s.get("items") or []) if isinstance(it, dict))
    return sec > 0 and listed / sec < TASKS_SHOW_BELOW and (len(tasks) > 1 or num(s.get("tasks_rest_s")) > 0)


def _tasks_html(s):
    sec = num(s.get("seconds"))
    out = []
    for t in [t for t in s.get("tasks") or [] if isinstance(t, dict)][:MAX_TASKS]:
        lab = clip(t.get("label") or "?", 90)
        share = ("%s of this slice" % fmt_pct(100.0 * num(t.get("seconds")) / sec)) if sec else ""
        out.append(row(lab, [("Task", esc(lab)), ("Share", esc(share))], meta=_meta(fmt_h(t.get("seconds"))), title_attr=lab))
    rest, n, top = num(s.get("tasks_rest_s")), int(num(s.get("tasks_rest_n"), 0)), num(s.get("tasks_rest_max_s"))
    if rest >= 60:
        lab = ("%d other tasks" % n if n > 1 else "other tasks") + (", each ≤ %s" % fmt_h(top) if n > 1 and top > 0 else "")
        out.append(row(lab, [("Share", esc("%s of this slice" % fmt_pct(100.0 * rest / sec)) if sec else "")],
                       meta=_meta(fmt_h(rest)), cls="muted"))
    return rows_list(out)


# ---------- deep dives ----------

def deep_dive_html(dd, cls_by_seg=None, with_head=True):
    """A triggered deep dive (view.deep_dives): computed facts, checks on disk, coded tips, and the AI subagent's
    notes, all as the same rows."""
    rows = []
    facts = [f for f in dd.get("facts") or [] if isinstance(f, dict)]
    if facts:
        rows.append(row("What the script found", [(f.get("label") or "?", esc(clip(f.get("value"), 200))) for f in facts],
                        lead=icon("search", "ri"), meta=_meta("%d facts" % len(facts))))
    checks = [c for c in dd.get("checks") or [] if isinstance(c, dict)]
    if checks:
        bad = sum(1 for c in checks if not c.get("ok") and not c.get("minor"))
        minor = sum(1 for c in checks if c.get("minor"))
        rows.append(row("Deploy files on disk", [("OK" if c.get("ok") else "Minor" if c.get("minor") else "Not OK", esc(clip(c.get("text"), 240))) for c in checks]
                        + [("Note", "read as they are today, not as they were during the runs")],
                        lead=icon("file", "ri"), meta=_meta("%d to fix" % bad if bad else "%d minor" % minor if minor else "all ok")))
    for a in dd.get("actions") or []:
        if isinstance(a, dict) and a.get("title"):
            rows.append(_todo_row("rule", clip(a.get("title"), 90), num(a.get("saving_min"), 0.0), _action_rows(a)))
    notes = [n for n in dd.get("notes") or [] if isinstance(n, dict) and n.get("title")]
    for n in notes:
        rows.append(_todo_row("llm", clip(n.get("title"), 90), 0.0, _note_rows(n)))
    if not notes:
        rows.append(row("AI deep dive not run yet", [
            ("How", esc("The skill starts a subagent with the prompt in %s; its notes appear here with an AI badge."
                        % (dd.get("brief_file") or "the deep-dive brief")))], lead='<span class="bd b-ai">AI</span>', cls="muted"))
    head = ""
    if with_head:
        head = ('<h3 class="ddh">%sDeep dive: %s <span class="ddv">%s · %s of agent time</span></h3>' % (
            icon(dd.get("icon") if dd.get("icon") in ICONS else "search", "ic"), esc(clip(dd.get("title"), 40).lower()),
            esc(fmt_h(dd.get("seconds"))), esc(fmt_pct(dd.get("share_pct")))))
    why = '<p class="ddw">Started because %s.</p>' % esc(dd.get("trigger") or "")
    return '<div class="dd">%s%s%s</div>' % (head, why if dd.get("trigger") else "", rows_list(rows))


def _dd_for_segment(view, sid):
    return [dd for dd in view.get("deep_dives") or [] if isinstance(dd, dict) and sid in (dd.get("segments") or [])]


# ---------- panels ----------

def _panel_head(i, s, cls, hidden):
    sv = seg_saving_min(s)
    return ['<section class="panel" id="rb-p%s" data-i="%s" aria-labelledby="rb-h%s"%s>' % (i, i, i, " hidden" if hidden else ""),
            '<div class="ph"><span class="dot %s" aria-hidden="true"></span><h2 id="rb-h%s">%s</h2>'
            '<span class="pv">%s%s</span></div>%s' % (
                cls, i, esc(clip(s.get("label") or s.get("id"), 60)), esc(fmt_h(s.get("seconds"))),
                (' <span class="pp">· %s to save</span>' % esc(fmt_can_save(sv))) if sv >= 1 else "", _panel_tools(True))]


def _panel_tools(back):
    return ('<div class="pt">%s<button type="button" class="tb xall" aria-pressed="false">Expand all</button></div>' % (
        '<button type="button" class="tb back" data-i="all">%sShow all</button>' % icon("all", "ic") if back else ""))


def _panel_html(i, s, cls, hidden, subagents=True, view=None):
    sec = num(s.get("seconds"))
    items = [it for it in (s.get("items") or []) if isinstance(it, dict)][:MAX_ITEMS]
    out = _panel_head(i, s, cls, hidden)
    todos = _todos(s)
    out.append('<h3>Do this</h3>')
    out.append(rows_list(todos) if todos else '<p class="none">No rule matched this slice.</p>')
    for dd in _dd_for_segment(view or {}, s.get("id")):
        out.append(deep_dive_html(dd))
    out.append('<h3>What exactly</h3>')
    if items:
        out.append(rows_list([_item_html(it, subagents) for it in items]))
    else:
        out.append('<p class="none">No single command or tool stands out.</p>')
    rest = num(s.get("rest_s"))
    if items and rest >= 60 and (sec <= 0 or rest / sec >= 0.01):
        n = int(num(s.get("rest_n"), 0)) + max(0, len(s.get("items") or []) - len(items))
        out.append('<p class="rest">+ %s in %s</p>' % (esc(fmt_h(rest)), "%d smaller items" % n if n > 1 else "smaller items"))
    if show_tasks(s, items):
        out.append('<h3>By task</h3>%s' % _tasks_html(s))
    out.append("</section>")
    return "".join(out)


def waiting_for(a):
    """Who acts on a waiting tip: 'you' (settings, CLAUDE.md, notifications, handing over work) or 'agent'."""
    return "agent" if a.get("for") == "agent" else "you"


def _waiting_todos(w):
    """(tips for you, tips for the agent, AI notes) of the waiting slice, as rows."""
    you, agent = [], []
    for a in w.get("actions") or []:
        if isinstance(a, dict) and a.get("title"):
            (agent if waiting_for(a) == "agent" else you).append(
                _todo_row("rule", clip(a.get("title"), 90), num(a.get("saving_min"), 0.0), _action_rows(a)))
    notes = [_todo_row("llm", clip(n.get("title"), 90), 0.0, _note_rows(n)) for n in w.get("notes") or []
             if isinstance(n, dict) and n.get("title")]
    return you, agent, notes


def _waiting_panel_html(i, w, cls, hidden):
    """The hatched 'Waiting for you' slice: tips for you and for the agent (rules/waiting.json), then the kinds of
    waiting as rows."""
    out = _panel_head(i, w, cls, hidden)
    you, agent, notes = _waiting_todos(w)
    if you:
        out.append('<h3>What you can do</h3>' + rows_list(you))
    if agent:
        out.append('<h3>What the agent can do</h3>' + rows_list(agent))
    if notes:
        out.append('<h3>Notes</h3>' + rows_list(notes))
    if not (you or agent or notes):
        out.append('<h3>Do this</h3><p class="none">No rule matched this waiting.</p>')
    out.append('<h3>What exactly</h3>')
    rows = []
    for p in [p for p in w.get("parts") or [] if isinstance(p, dict)]:
        det = [("What", esc(clip(p.get("help"), 200))),
               ("Median", esc(fmt_h(p.get("p50_s"))) if int(num(p.get("count"), 0)) > 1 else ""),
               ("Longest", esc(fmt_h(p.get("longest_s"))) if int(num(p.get("count"), 0)) > 1 else ""),
               ("Ended on a question", esc("%d of %d stops, %s" % (int(num(p.get("questions"))), int(num(p.get("count"))), fmt_h(p.get("questions_s"))))
                if num(p.get("questions")) > 0 else ""),
               ("Most often", "<ul class=\"ev\">%s</ul>" % "".join(
                   '<li>%s ×%d%s</li>' % (esc(clip(t.get("short") or t.get("what"), 40)), int(num(t.get("count"))),
                                          "" if str(t.get("what") or "").lower() == str(t.get("short") or "").lower()
                                          else ' <code>%s</code>' % esc(clip(t.get("what"), 80)))
                   for t in p.get("top") or [] if isinstance(t, dict)) if p.get("top") else ""),
               ("Sessions", "%d" % int(num(p.get("sessions"))) if num(p.get("sessions")) > 1 else "")]
        lab = clip(p.get("label") or p.get("id"), 40)
        rows.append(row(lab, det, meta=_meta(fmt_h(p.get("seconds")), int(num(p.get("count"), 0))), title_attr=lab))
    out.append(rows_list(rows))
    parked = num(w.get("parked_s"))
    out.append('<p class="rest">Not agent time. Added up per session: sessions that wait at the same time each count.%s</p>' % (
        esc(" Parked sessions (%s) and idle time are left out." % fmt_h(parked)) if parked >= 60 else ""))
    out.append("</section>")
    return "".join(out)


def top_todos(segs, waiting, deep_dives, classes, limit=6, n_rules=4):
    """'Do this first' in the All view: the n_rules rules with the biggest saving, then the deep dive's AI notes and
    the first AI note of each slice (biggest slice first), then the other rules with a saving, the deep-dive tips,
    the remaining notes, rules without an estimate and the waiting tips. The first waiting tip for you always
    makes the list (it takes the last place when it would not). Each row says where it comes from."""
    saving_rules, first_notes, later = [], [], []  # (sort key, html)
    for i, s in enumerate(segs):
        dot = '<span class="dot %s" aria-hidden="true" title="%s"></span>' % (classes[i], esc(s.get("label") or s.get("id")))
        src = [("Slice", esc("%s, %s" % (s.get("label") or s.get("id"), fmt_h(s.get("seconds")))))]
        seen_note = False
        for kind, title, saving, rows in todo_entries(s):
            html_ = _todo_row(kind, title, saving, rows, dot, src)
            if kind == "rule" and saving >= 1:
                saving_rules.append((-saving, html_))
            elif kind == "llm" and not seen_note:
                first_notes.append(((1, i), html_))
                seen_note = True
            else:
                later.append(((2 if kind == "llm" else 3, i), html_))
    dd_rules = []
    for dd in deep_dives or []:
        tag = '<span class="dot cdd" aria-hidden="true" title="Deep dive"></span>'
        src = [("Deep dive", esc("%s, %s" % (dd.get("title"), fmt_h(dd.get("seconds")))))]
        for a in dd.get("actions") or []:
            if isinstance(a, dict) and a.get("title"):
                dd_rules.append(_todo_row("rule", clip(a.get("title"), 90), 0.0, _action_rows(a), tag, src))
        for k, nt in enumerate(n for n in dd.get("notes") or [] if isinstance(n, dict) and n.get("title")):
            first_notes.append(((0, k), _todo_row("llm", clip(nt.get("title"), 90), 0.0, _note_rows(nt), tag, src)))
    first_you = None
    if waiting:
        dot = '<span class="dot cw" aria-hidden="true" title="Waiting for you"></span>'
        k = 0
        for a in waiting.get("actions") or []:
            if isinstance(a, dict) and a.get("title"):
                who = waiting_for(a)
                html_ = _todo_row("rule", clip(a.get("title"), 90), num(a.get("saving_min"), 0.0), _action_rows(a), dot,
                                  [("Slice", "Waiting for you"), ("For", "you" if who == "you" else "the agent")])
                later.append(((4, k), html_))
                k += 1
                if who == "you" and first_you is None:
                    first_you = html_
        for n in waiting.get("notes") or []:
            if isinstance(n, dict) and n.get("title"):
                later.append(((4, k), _todo_row("llm", clip(n.get("title"), 90), 0.0, _note_rows(n), dot, [("Slice", "Waiting for you")])))
                k += 1
    saving_rules.sort(key=lambda r: r[0])
    first_notes.sort(key=lambda r: r[0])
    later.sort(key=lambda r: r[0])
    ordered = ([h for _, h in saving_rules[:n_rules]] + [h for _, h in first_notes] + [h for _, h in saving_rules[n_rules:]]
               + dd_rules + [h for _, h in later])
    shown = ordered[:limit]
    if first_you and first_you not in shown:
        shown = shown[:limit - 1] + [first_you]
    return shown, max(0, len(ordered) - len(shown))


def _all_panel_html(view, segs, waiting, classes, ring_s, hidden):
    out = ['<section class="panel" id="rb-pall" data-i="all" aria-labelledby="rb-hall"%s>' % (" hidden" if hidden else ""),
           '<div class="ph">%s<h2 id="rb-hall">All slices</h2><span class="pv">%s</span></div>%s' % (
               icon("all", "ic hi"), esc(fmt_h(ring_s)), _panel_tools(False))]
    todos, more = top_todos(segs, waiting, view.get("deep_dives"), classes)
    out.append('<h3>Do this first</h3>')
    out.append(rows_list(todos) if todos else '<p class="none">No rule matched these runs.</p>')
    if more:
        out.append('<p class="rest">+ %d more in the slices: pick one in the chart</p>' % more)
    for dd in view.get("deep_dives") or []:
        if isinstance(dd, dict):
            out.append(deep_dive_html(dd))
    out.append("</section>")
    return "".join(out)


def _legend_cells(sec, minutes):
    return '<span class="lh">%s</span><span class="lsv%s">%s</span>' % (
        esc(fmt_h(sec)), "" if num(minutes) >= 1 else " none", esc(fmt_can_save(minutes)))


def _legend_html(i, s, cls, pressed):
    """One row of the overview: the slice, its time and what it can save (rule estimates, see seg_saving_min)."""
    lab = s.get("label") or s.get("id")
    sv = seg_saving_min(s)
    return ('<li><button type="button" class="lg" data-i="%d" aria-pressed="%s" aria-controls="rb-p%d" aria-label="%s">'
            '<span class="dot %s" aria-hidden="true"></span><span class="ll">%s</span>%s</button></li>' % (
                i, "true" if pressed else "false", i, esc(_saving_aria(lab, s.get("seconds"), sv)), cls,
                esc(clip(lab, 60)), _legend_cells(s.get("seconds"), sv)))


def _legend_all(ring_s, save_min, pressed):
    return ('<li class="lhd" aria-hidden="true"><span></span><span></span><span>Time</span><span>Can save</span></li>'
            '<li><button type="button" class="lg lall" data-i="all" aria-pressed="%s" aria-controls="rb-pall" aria-label="%s">'
            '%s<span class="ll">All slices</span>%s</button></li>' % (
                "true" if pressed else "false", esc(_saving_aria("All slices", ring_s, save_min)), icon("all", "dot ic"),
                _legend_cells(ring_s, save_min)))


def _basis_html(rows):
    out, kinds = [], []
    for b in rows:
        if not isinstance(b, dict):
            continue
        k = b.get("kind") if b.get("kind") in KIND_LABEL else "missing"
        kinds.append(k)
        out.append(row(clip(b.get("what"), 80), [("How", esc(clip(b.get("how"), 200))), ("Detail", esc(clip(b.get("detail"), 800)))],
                       meta='<span class="bd k-%s">%s</span>' % (k, KIND_LABEL[k])))
    legend = "".join('<li><span class="bd k-%s">%s</span> %s</li>' % (k, KIND_LABEL[k], esc(text))
                     for k, text in KIND_HELP if k in ("deterministic", "llm") or k in kinds)
    return rows_list(out), legend


# Simple 16px line icons (stroke = currentColor), so each number in the header reads at a glance.
ICONS = {
    "agent": '<circle cx="8" cy="8" r="6.2"/><path d="M8 4.6V8l2.4 1.6"/>',
    "waiting": '<path d="M4.5 2h7M4.5 14h7M5.2 2v2.2c0 1.5 2.8 2.6 2.8 3.8S5.2 10.3 5.2 11.8V14M10.8 2v2.2c0 1.5-2.8 2.6-2.8 3.8s2.8 2.3 2.8 3.8V14"/>',
    "saving": '<path d="M2 4.5l4.2 4.2 2.6-2.6L14 11.3"/><path d="M14 7.8v3.5h-3.5"/>',
    "tips": '<path d="M5.8 11.2h4.4M6.4 13.6h3.2M8 1.8a4.2 4.2 0 0 0-2.6 7.5c.4.3.6.8.6 1.3v.6h4v-.6c0-.5.2-1 .6-1.3A4.2 4.2 0 0 0 8 1.8z"/>',
    "period": '<rect x="2" y="3.2" width="12" height="10.8" rx="1.6"/><path d="M2 6.6h12M5.2 1.6v3M10.8 1.6v3"/>',
    "sessions": '<path d="M2.5 5.5L8 2.6l5.5 2.9L8 8.4z"/><path d="M2.5 8.2L8 11.1l5.5-2.9M2.5 10.9L8 13.8l5.5-2.9"/>',
    "star": '<path d="M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z"/>',
    "link": '<path d="M6.6 9.4l2.8-2.8M7 4.6l1.3-1.3a2.6 2.6 0 0 1 3.7 3.7L10.7 8.3M9 11.4l-1.3 1.3a2.6 2.6 0 0 1-3.7-3.7l1.3-1.3"/>',
    "info": '<circle cx="8" cy="8" r="6.2"/><path d="M8 7.2v4M8 4.9v.1"/>',
    "all": '<circle cx="8" cy="8" r="5.6"/><circle cx="8" cy="8" r="2.4"/>',
    "search": '<circle cx="7" cy="7" r="4.4"/><path d="M10.3 10.3L14 14"/>',
    "deploy": '<path d="M8 13.5V5M4.6 8.2L8 4.8l3.4 3.4"/><path d="M3 2.5h10"/>',
    "file": '<path d="M4 1.8h5l3 3v9.4H4z"/><path d="M9 1.8v3h3"/>',
}
REPO_URL = "https://github.com/jindraregal/analyse-bottleneck"
AUTHOR_URL = "https://jindraregal.com"
TOOL_NAME = "analyse-bottleneck"


def icon(name, cls="ic"):
    return ('<svg class="%s" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">%s</svg>' % (cls, ICONS[name]))


def _stat(name, value, label, title=""):
    return ('<li class="st"%s>%s<span class="sv2">%s</span><span class="sl">%s</span></li>' % (
        ' title="%s"' % esc(title) if title else "", icon(name), esc(value), esc(label)))


def _saving_min(ring):
    """What the whole ring can save: the slices' savings added up (the 'All slices' row and the header stat)."""
    return sum(seg_saving_min(s) for s in ring)


def _counts(segs, waiting, deep_dives):
    groups = segs + ([waiting] if waiting else []) + [dd for dd in deep_dives or [] if isinstance(dd, dict)]
    rules = sum(len([a for a in x.get("actions") or [] if isinstance(a, dict) and a.get("title")]) for x in groups)
    ai = sum(len([n for n in x.get("notes") or [] if isinstance(n, dict) and n.get("title")]) for x in groups)
    return rules, ai


def page_stats(view, segs, waiting):
    """The header strip: possible saving, recommendations, period, sessions (agent time and waiting are in the
    chart's centre). Small inline numbers with icons; 'How to read' explains each one. All computed here."""
    win = view.get("window") or {}
    saving = _saving_min(segs + ([waiting] if waiting else []))
    rules, ai = _counts(segs, waiting, view.get("deep_dives"))
    tips = rules + ai
    ses, sub = int(num(win.get("sessions"), 0)), int(num(win.get("subagent_sessions"), 0))
    out = [_stat("saving", ("≈" + fmt_h(saving * 60)) if saving >= 1 else "–", "to save",
                 "Sum of the 'Can save' column: rule estimates, each capped at its slice's time")]
    out.append(_stat("tips", str(tips), "recommendations" if tips != 1 else "recommendation",
                     ("%d rules, %d AI notes" % (rules, ai)) if ai else "from coded rules"))
    a, b = _dt(win.get("since")), _dt(win.get("until"))
    if a and b:
        days = max(1, round((b - a).total_seconds() / 86400.0))
        out.append(_stat("period", fmt_period(a, b), "%d, %s" % (b.year, "%d days" % days if days > 1 else "1 day"),
                         fmt_window(win.get("since"), win.get("until"))))
    out.append(_stat("sessions", str(ses), ("sessions, %d subagents" % sub) if sub else ("sessions" if ses != 1 else "session")))
    return '<ul class="stats" aria-label="Summary">%s</ul>' % "".join(out)


def page_lede(view, agent_s, waiting):
    """One plain sentence under the title: what the page shows and how to use it (no AI text up here)."""
    w = num((waiting or {}).get("seconds"))
    first = "Agents worked %s on their own" % fmt_h(agent_s)
    if w >= 60:
        first += " and waited %s for you (the striped slice)" % fmt_h(w)
    return '<p class="lede">%s. Pick a slice to see what ran and what to change; pick it again to see all.</p>' % esc(first)


def page_about(view, segs, waiting):
    """'How to read this': one line per number on the page, the same icons as the strip."""
    win = view.get("window") or {}
    rules, ai = _counts(segs, waiting, view.get("deep_dives"))
    ses, sub = int(num(win.get("sessions"), 0)), int(num(win.get("subagent_sessions"), 0))
    lines = [
        ("agent", "Agent time", "time agents worked on their own: commands, model turns, waits on their own jobs. All sessions and subagents are added up, so parallel work counts more than clock time."),
        ("waiting", "Waiting for you", "the striped slice: main sessions waited for your next message, an approval or an answer. Not agent time; parked sessions and idle gaps are left out. Its tips are split into what you can do and what the agent can do."),
        ("saving", "To save", "the 'Can save' column next to the chart added up. Per slice: the coded rules' estimates, capped at the slice's time; for the striped slice it is your waiting time. '–' means no rule has an honest estimate there; AI notes carry none."),
        ("tips", "Recommendations", "%d from coded rules (badge rule), %d notes written by a model and checked by a second one (badge AI)." % (rules, ai)),
        ("period", "Period", fmt_window(win.get("since"), win.get("until")) or "the window of the transcripts read"),
        ("sessions", "Sessions", "%d main sessions and %d subagent transcripts read." % (ses, sub)),
    ]
    if view.get("deep_dives"):
        lines.append(("search", "Deep dive", "when one cause takes a big share of agent time, a second analysis looks only at it: facts from the transcripts, the project's files, and a subagent's notes."))
    body = "".join('<li>%s<span><b>%s</b> %s</span></li>' % (icon(ic), esc(t), esc(d)) for ic, t, d in lines)
    return ('<details class="about"><summary>%s<span>How to read this</span></summary><ul class="abl">%s</ul></details>' % (icon("info"), body))


def render_body(view):
    segs = sorted([s for s in view.get("segments") or [] if isinstance(s, dict)],
                  key=lambda s: -num(s.get("seconds")))
    totals = view.get("totals") or {}
    agent_s = num(totals.get("agent_s")) or sum(num(s.get("seconds")) for s in segs)
    waiting = view.get("waiting") if isinstance(view.get("waiting"), dict) and num((view.get("waiting") or {}).get("seconds")) > 0 else None
    ring = segs + ([waiting] if waiting else [])
    waiting_s = num(waiting.get("seconds")) if waiting else 0.0
    ring_s = agent_s + waiting_s
    saves = [seg_saving_min(s) for s in ring]
    classes = colour_classes(segs)
    if waiting:
        classes[len(segs)] = "cw"
    win = view.get("window") or {}
    subagents = bool(num(win.get("subagent_sessions"), 0))
    basis_rows, kind_legend = _basis_html(view.get("basis") or [])
    gen = _dt(view.get("generated"))
    panels = []
    if segs:
        panels.append(_all_panel_html(view, segs, waiting, classes, ring_s, False))
    panels += [_panel_html(i, s, classes[i], True, subagents, view) for i, s in enumerate(segs)]
    if waiting:
        panels.append(_waiting_panel_html(len(segs), waiting, "cw", True))
    return {
        "TITLE": esc(clip(view.get("title") or "Agent time", 80)),
        "LEDE": page_lede(view, agent_s, waiting),
        "STATS": page_stats(view, segs, waiting),
        "ABOUT": page_about(view, segs, waiting),
        "SVG": donut_svg(ring, saves, classes, ring_s, None, agent_s, waiting_s),
        "LEGEND": (_legend_all(ring_s, sum(saves), True) if ring else "") + "".join(_legend_html(i, s, classes[i], False) for i, s in enumerate(ring)),
        "PANELS": "".join(panels) or '<p class="none">No agent time in this window.</p>',
        "BASIS_ROWS": basis_rows,
        "KIND_LEGEND": kind_legend,
        "FOOTER": esc("Generated %s · %s" % ("%d %s UTC" % (gen.day, gen.strftime("%b %Y %H:%M")) if gen else "?", TOOL_NAME)),
        "LINKS": ('<a class="star" href="%s" target="_blank" rel="noopener">%s<span>Star on GitHub</span></a>'
                  '<a href="%s" target="_blank" rel="noopener">%s<span>github.com/jindraregal/analyse-bottleneck</span></a>'
                  '<a href="%s" target="_blank" rel="noopener">%s<span>jindraregal.com</span></a>' % (
                      REPO_URL, icon("star"), REPO_URL, icon("link"), AUTHOR_URL, icon("link"))),
    }


def json_for_script(obj):
    """JSON safe inside <script type="application/json">: no '<', '>' or '&' survive literally."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_html(view, notes=None):
    """The whole page as a string. `notes` (LLM notes file) is merged first if given."""
    v = merge_notes(view, notes) if notes else (view if isinstance(view, dict) else {})
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    fields = render_body(v)
    fields["DATA"] = json_for_script(v)
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: fields.get(m.group(1), ""), tpl)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Render a analyse-bottleneck view.json as a single HTML page.")
    ap.add_argument("view")
    ap.add_argument("--notes", help="LLM notes JSON to merge (shown with an AI badge)")
    ap.add_argument("-o", "--out", help="output .html (default: stdout)")
    a = ap.parse_args(argv)
    with open(a.view, encoding="utf-8") as f:
        view = json.load(f)
    notes = None
    if a.notes:
        with open(a.notes, encoding="utf-8") as f:
            notes = json.load(f)
    page = render_html(view, notes)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(page)
    else:
        sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
