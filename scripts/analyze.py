#!/usr/bin/env python3
"""analyse-bottleneck: where did the time and quota of autonomous Claude Code runs go?

  analyze.py [analyze] --project NAME | --session ID|latest|PATH | --all  [--since 24h] [--until DATE]
  analyze.py compare --before SPEC --after SPEC [--project NAME | --all]
  analyze.py watch   [--transcript FILE] [--hook-json]      (append history, print only warnings)
  analyze.py view    --project NAME --since 7d --out DIR [--notes FILE]   (one-chart report: view.json, html, brief.md)
  analyze.py render  VIEW_JSON --notes FILE --out HTML      (re-render with model notes, no re-parsing)

Reads ~/.claude/projects/<project>/*.jsonl and <session>/subagents/agent-*.jsonl in a streaming
fashion; prints compact markdown and writes the full JSON. Python standard library only.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rbn import compare as cmpmod  # noqa: E402
from rbn import core, render, watch as watchmod  # noqa: E402
from rbn.rules import RuleSet  # noqa: E402
from rbn.util import parse_when  # noqa: E402

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Reports live outside the skill folder: a plugin install sits in a versioned cache that updates replace.
REPORTS = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "analyse-bottleneck", "reports")
SUBCOMMANDS = ("analyze", "compare", "watch", "watch-ctl", "calls", "dora", "view", "render")


def add_scope(p):
    p.add_argument("--project", help="substring of the project directory name (default: the one for the current directory)")
    p.add_argument("--all", action="store_true", help="all projects")
    p.add_argument("--session", help="session id prefix, 'latest', or a path to a .jsonl")
    p.add_argument("--since", help="ISO date/time or relative (24h, 7d)")
    p.add_argument("--until", help="ISO date/time or relative")
    p.add_argument("--idle-threshold", type=float, default=180.0, help="seconds of nothing before a gap counts as idle (default 180)")
    p.add_argument("--rules-dir", help="rules directory (default: ../rules)")
    p.add_argument("--extra-rules", action="append", default=[], help="additional rules directory (repeatable)")
    p.add_argument("--machine-log", help="JSONL from sample_machine.py")
    p.add_argument("--sysstat", nargs="?", const="/var/log/sysstat", help="join measured host CPU/memory/disk from sysstat archives (default dir /var/log/sysstat; needs sadf)")
    p.add_argument("--jobs", help="directory of JSON job records of a job wrapper (queue/admission wait, runtime, memory peak); see README")
    p.add_argument("--dora-json", help="output of 'analyze.py dora --format json' to include in the report")
    p.add_argument("--no-host-snapshot", dest="host_snapshot", action="store_false", help="do not record current disk free / GPU presence")
    p.add_argument("--token-minute-equiv", type=float, default=0.0, help="units that count as one minute when ranking (default: this run's units per autonomous minute)")
    p.add_argument("--top", type=int, default=8)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("analyze", help="analyse sessions (default)")
    add_scope(a)
    a.add_argument("--format", choices=("md", "json", "both"), default="md")
    a.add_argument("--json-out", help="where to write the full JSON (default: ~/.local/share/analyse-bottleneck/reports/report-<time>.json)")
    a.add_argument("--no-json-file", action="store_true", help="do not write the JSON file")
    a.add_argument("--section", help="with --format json: comma list of top-level sections to print (e.g. ranked,wall_clock)")
    c = sub.add_parser("compare", help="before/after of two windows or sessions")
    add_scope(c)
    c.add_argument("--before", required=True, help="SPEC: 'session:<id|latest>' or 'FROM..TO' (either side may be empty)")
    c.add_argument("--after", required=True)
    c.add_argument("--format", choices=("md", "json"), default="md")
    w = sub.add_parser("watch", help="append a summary to history, print only threshold warnings (for a Stop hook)")
    add_scope(w)
    w.add_argument("--transcript", help="transcript path; default: stdin hook JSON, else newest session of the project")
    w.add_argument("--history", help="history JSONL (default: $XDG_STATE_HOME/analyse-bottleneck/history.jsonl)")
    w.add_argument("--thresholds", help="thresholds JSON (default: rules/thresholds.json)")
    w.add_argument("--hook-json", action="store_true", help='print {"systemMessage": ...} when there are warnings')
    w.add_argument("--min-interval", type=float, default=0.0, help="skip if the last run for this transcript was less than N seconds ago")
    w.add_argument("--quiet-ok", action="store_true", help="print nothing when there are no warnings (default)")
    w.add_argument("--detach", action="store_true", help="return at once and analyse in a niced background process; its warnings are shown at the next stop")
    w.add_argument("--max-seconds", type=float, default=120.0, help="the background analysis gives up after N seconds (default 120)")
    w.add_argument("--report", action="store_true", help="summarise the history file (trends, recurring warnings, own overhead) instead of analysing")
    w.add_argument("--pending", action="store_true", help=argparse.SUPPRESS)
    w.add_argument("--state-dir", help="state directory (default: $XDG_STATE_HOME/analyse-bottleneck)")
    wc = sub.add_parser("watch-ctl", help="opt-in switch for the watch hook: on, off, status, print-config (never edits settings files)")
    wc.add_argument("action", choices=("on", "off", "status", "print-config"))
    wc.add_argument("--state-dir")
    k = sub.add_parser("calls", help="ledger of every tool call and model turn: duration and what it likely did")
    add_scope(k)
    k.add_argument("--out", help="output prefix (default: ~/.local/share/analyse-bottleneck/reports/calls-<time>); writes .html and .csv")
    k.add_argument("--max-page-rows", type=int, default=12000, help="calls embedded in the HTML, longest first; the CSV keeps all")
    d = sub.add_parser("dora", help="DORA-style delivery metrics from git (+ exported GitHub Actions runs, + optional deploy log); every value says if it is measured or a proxy")
    d.add_argument("--repo", required=True, help="path to a git checkout")
    d.add_argument("--since", default="7d")
    d.add_argument("--until")
    d.add_argument("--branch", help="mainline branch (default: origin/HEAD)")
    d.add_argument("--ci-runs", help="JSON list of GitHub Actions runs you exported yourself "
                   "(fields workflowName,conclusion,status,headBranch,event,createdAt,startedAt,updatedAt; see the README)")
    d.add_argument("--no-ci", action="store_true", help=argparse.SUPPRESS)  # old flag; CI runs now come only from --ci-runs
    d.add_argument("--deploy-regex", help="workflow-name regex that marks deploy runs")
    d.add_argument("--deploys", help='deploy log: JSON list or JSONL of {"time": epoch|ISO, "ok": bool}')
    d.add_argument("--format", choices=("md", "json"), default="md")
    d.add_argument("--out", help="also write the JSON here (pass it to analyze --dora-json)")
    v = sub.add_parser("view", help="one-chart report: donut of agent time, what exactly each slice was, actions (writes view.json, .html, .brief.md)")
    add_scope(v)
    v.add_argument("--out", help="output directory (default: ~/.local/share/analyse-bottleneck/reports/)")
    v.add_argument("--name", help="file name stem (default: project label + date)")
    v.add_argument("--title", help="chart title (default: project label)")
    v.add_argument("--notes", help="notes JSON written by a model (merged and marked AI)")
    r = sub.add_parser("render", help="re-render a view.json (optionally with model notes) without re-reading transcripts")
    r.add_argument("view_json")
    r.add_argument("--notes", help="notes JSON written by a model (merged and marked AI)")
    r.add_argument("--out", help="output HTML (default: next to the view, .html)")
    return ap


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_html(view, notes, path):
    """Render with rbn.view_render when it is available; returns the path or None."""
    try:
        from rbn.view_render import render_html
    except ImportError:
        return None
    page = render_html(view, notes)
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def cmd_view(args):
    from rbn import view as viewmod
    t0 = time.time()
    rs = prepare(args)
    args.top = 100000  # every ranked finding, so each slice can get its actions
    opts = {"idle_threshold": args.idle_threshold, "token_minute_equiv": args.token_minute_equiv}
    win = (args.since_ts, args.until_ts)
    analyses = core.analyze_groups(core.find_groups(args.project, args.all, args.session, win[0]), rs, win, opts)
    if not analyses:
        sys.stderr.write("No sessions found for this scope (check --project/--all/--since; projects dir: %s)\n" % core.projects_root())
        return 2
    rep = core.build_report(analyses, rs, opts, args)
    view = viewmod.build_view(rep, analyses, {"rs": rs, "title": args.title, "since": win[0], "until": win[1],
                                              "idle_threshold": args.idle_threshold, "sysstat": bool(args.sysstat)})
    notes = _load_json(args.notes) if args.notes else None
    if notes:
        view = viewmod.merge_notes(view, notes)
    out = args.out or REPORTS
    os.makedirs(out, exist_ok=True)
    name = args.name or re.sub(r"[^\w.-]+", "-", view["title"]).strip("-") + "-" + time.strftime("%Y%m%d")
    for dd in view.get("deep_dives") or []:  # the subagent prompt for each triggered deep dive
        dpath = os.path.join(out, "%s.deepdive-%s.md" % (name, dd["id"]))
        dd["brief_file"] = os.path.basename(dpath)
        with open(dpath, "w", encoding="utf-8") as f:
            f.write(viewmod.deepdive_mod.render_brief(dd, rs, view))
    vpath = os.path.join(out, name + ".view.json")
    with open(vpath, "w", encoding="utf-8") as f:
        json.dump(view, f, indent=1, ensure_ascii=False)
    bpath = os.path.join(out, name + ".brief.md")
    with open(bpath, "w", encoding="utf-8") as f:
        f.write(viewmod.render_brief(view))
    hpath = _write_html(view, notes, os.path.join(out, name + ".html"))
    sys.stdout.write("%s\nView: %s\nBrief: %s\nPage: %s\n(%.1fs)\n" % (view["headline"], vpath, bpath, hpath or "renderer missing (view_render.py)", time.time() - t0))
    return 0


def cmd_render(args):
    from rbn import view as viewmod
    view = _load_json(args.view_json)
    notes = _load_json(args.notes) if args.notes else None
    merged = viewmod.merge_notes(view, notes) if notes else view
    out = args.out or re.sub(r"(\.view)?\.json$", "", args.view_json) + ".html"
    path = _write_html(merged, notes, out)
    if not path:
        sys.stderr.write("renderer missing (scripts/rbn/view_render.py)\n")
        return 2
    sys.stdout.write("Page: %s\n" % path)
    return 0


def cmd_dora(args):
    from rbn import dora
    since = parse_when(args.since)
    until = parse_when(args.until) if args.until else None
    rep = dora.run(os.path.abspath(args.repo), since, until, args.branch, None if args.no_ci else args.ci_runs, args.deploy_regex, args.deploys)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1)
    sys.stdout.write(json.dumps(rep, indent=1) + "\n" if args.format == "json" else dora.render(rep))
    return 0


def prepare(args):
    args.since_ts = parse_when(args.since) if args.since else None
    args.until_ts = parse_when(args.until) if args.until else None
    rs = RuleSet(args.rules_dir, args.extra_rules)
    return rs


def cmd_analyze(args):
    rs = prepare(args)
    rep = core.run(args, rs)
    if rep["meta"]["sessions"] == 0:
        sys.stderr.write("No sessions found for this scope (check --project/--all/--since; projects dir: %s)\n" % core.projects_root())
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    jpath = args.json_out or os.path.join(REPORTS, "report-%s.json" % stamp)
    if not args.no_json_file:
        os.makedirs(os.path.dirname(os.path.abspath(jpath)), exist_ok=True)
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, default=str)
    if args.format in ("md", "both"):
        sys.stdout.write(render.render(rep, args.top))
        if not args.no_json_file:
            sys.stdout.write("\nFull JSON: %s\n" % jpath)
    if args.format in ("json", "both"):
        out = rep
        if args.section:
            out = {k: rep[k] for k in args.section.split(",") if k in rep}
        sys.stdout.write(json.dumps(out, indent=1, default=str) + "\n")
    return 0


def _spec_args(args, spec):
    ns = argparse.Namespace(**vars(args))
    ns.session = None
    ns.since_ts = ns.until_ts = None
    if spec.startswith("session:"):
        ns.session = spec.split(":", 1)[1]
    elif ".." in spec:
        a, b = spec.split("..", 1)
        ns.since_ts = parse_when(a) if a else None
        ns.until_ts = parse_when(b) if b else None
    else:
        raise SystemExit("bad SPEC %r (use session:<id> or FROM..TO)" % spec)
    return ns


def cmd_compare(args):
    rs = RuleSet(args.rules_dir, args.extra_rules)
    reps = []
    for spec in (args.before, args.after):
        ns = _spec_args(args, spec)
        rep = core.run(ns, rs)
        if rep["meta"]["sessions"] == 0:
            sys.stderr.write("No sessions for %s\n" % spec)
            return 2
        reps.append(rep)
    cmp = cmpmod.compare(*reps)
    sys.stdout.write(json.dumps(cmp, indent=1, default=str) + "\n" if args.format == "json" else cmpmod.render(cmp, args.before, args.after))
    return 0


def _stdin_hook():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        import select
        if not select.select([sys.stdin], [], [], 0.5)[0]:
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return {}


def cmd_watch(args):
    """Stop-hook entry. Never blocks the stop, never fails it (exit 0), never edits settings."""
    try:
        return _watch(args)
    except Exception as e:  # a broken analysis must not break the user's session
        try:
            watchmod.log_error(args.state_dir, repr(e))
        except Exception:
            pass
        return 0


def _watch(args):
    state = args.state_dir or watchmod.state_dir()
    hist_path = args.history or os.path.join(state, "history.jsonl")
    if args.report:
        sys.stdout.write(watchmod.report(watchmod.read_history(hist_path, limit=100000), args.since))
        return 0
    if watchmod.disabled(state):
        return 0
    hook = {} if args.transcript else _stdin_hook()
    tpath = args.transcript or hook.get("transcript_path")
    if args.detach:
        # foreground part: deliver warnings a previous background run left behind, start a new run, return
        pending = watchmod.take_pending(state)
        if tpath and os.path.isfile(tpath) and not watchmod.should_skip(watchmod.read_history(hist_path), tpath, args.min_interval or 300):
            argv = [sys.executable, os.path.abspath(__file__), "watch", "--transcript", tpath, "--state-dir", state, "--history", hist_path,
                    "--max-seconds", str(args.max_seconds), "--pending"]
            for flag in ("rules_dir", "thresholds"):
                if getattr(args, flag, None):
                    argv += ["--" + flag.replace("_", "-"), getattr(args, flag)]
            watchmod.spawn_detached(argv)
        if pending:
            msgs = ["analyse-bottleneck: " + m for m in pending]
            sys.stdout.write((json.dumps({"systemMessage": "\n".join(msgs)}) if args.hook_json else "\n".join(msgs)) + "\n")
        return 0
    if tpath:
        args.session = tpath
    elif not args.session:
        args.session = "latest"
    lock = watchmod.try_lock(state)
    if lock is None:
        return 0  # another analysis is running: skip, the next stop catches up
    try:
        return _watch_locked(args, state, hist_path, tpath)
    finally:
        watchmod.time_limit(0)
        lock.close()


def _watch_locked(args, state, hist_path, tpath):
    watchmod.time_limit(args.max_seconds)
    t0 = time.time()
    rs = prepare(args)
    th = dict(rs.thresholds)
    if args.thresholds:
        with open(args.thresholds, encoding="utf-8") as f:
            th.update(json.load(f))
    history = watchmod.read_history(hist_path)
    if tpath and os.path.isfile(tpath) and watchmod.should_skip(history, tpath, args.min_interval):
        return 0
    args.host_snapshot = False
    rep = core.run(args, rs)
    if rep["meta"]["sessions"] == 0:
        return 0
    s = rep["sessions"][-1]
    size = os.path.getsize(tpath) if tpath and os.path.isfile(tpath) else 0
    line = watchmod.summary_line(rep, s["session"], size)
    line["audit"] = watchmod.audit(rep, rs, tpath, t0)
    warned = watchmod.warnings_for(line, th, history)
    watchmod.append(hist_path, line, warned)
    if warned:
        msgs = [t for _, _, t in warned]
        if getattr(args, "pending", False):
            watchmod.put_pending(state, msgs)
        elif args.hook_json:
            sys.stdout.write(json.dumps({"systemMessage": "\n".join("analyse-bottleneck: " + m for m in msgs)}) + "\n")
        else:
            sys.stdout.write("\n".join("analyse-bottleneck: " + m for m in msgs) + "\n")
    return 0


def cmd_watch_ctl(args):
    state = args.state_dir or watchmod.state_dir()
    if args.action == "on":
        watchmod.set_disabled(state, False)
    elif args.action == "off":
        watchmod.set_disabled(state, True)
    if args.action == "print-config":
        sys.stdout.write(watchmod.hook_snippet(os.path.abspath(__file__)) + "\n")
        return 0
    sys.stdout.write(watchmod.status(state) + "\n")
    return 0


def cmd_calls(args):
    from rbn import calls as callsmod
    rs = prepare(args)
    prefix = args.out or os.path.join(REPORTS, "calls-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(os.path.abspath(prefix)), exist_ok=True)
    data = callsmod.run(args, rs, prefix, args.max_page_rows)
    if not data["rows"]:
        sys.stderr.write("No sessions found for this scope\n")
        return 2
    sys.stdout.write(callsmod.render_md(data, args.top) + "\nPage: %s.html\nAll calls: %s.csv\n" % (prefix, prefix))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        argv = ["analyze"] + argv
    # project directory names start with '-' (-home-me-git-app): argparse would read '--project -home-...' as two flags
    for i in range(len(argv) - 1):
        if argv[i] in ("--project", "--session") and re.match(r"^-[^-]", argv[i + 1]) and len(argv[i + 1]) > 2:
            argv[i:i + 2] = [argv[i] + "=" + argv[i + 1]]
            break
    args = build_parser().parse_args(argv)
    return {"analyze": cmd_analyze, "compare": cmd_compare, "watch": cmd_watch, "calls": cmd_calls, "dora": cmd_dora, "watch-ctl": cmd_watch_ctl,
            "view": cmd_view, "render": cmd_render}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
