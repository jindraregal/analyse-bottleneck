"""DORA-style delivery metrics from git history and, optionally, CI runs or a deploy log.

Nothing here is assumed to exist. Each metric says what it was computed from, and a metric
with no source is reported as missing instead of being approximated silently:

- deployment frequency: successful runs of a deploy workflow (regex on the workflow name),
  or a deploy log (--deploys FILE: JSONL/JSON list of {"time": epoch|ISO, "ok": bool}).
  Without either, only the mainline change rate is given, labelled as a proxy.
- lead time for changes: first commit of a merged branch -> merge on the mainline (merge
  commits), or author time -> commit time for direct/squash commits. A proxy: it ends at the
  merge, not at the deploy, unless a deploy source exists.
- change failure rate: failed deploys / deploys; otherwise reverts / mainline changes (proxy).
- time to restore: failed deploy (or failed mainline CI run) -> next success of the same workflow.
"""
import json
import re
import subprocess
import time
from collections import defaultdict

from .util import parse_ts

DEPLOY_RX_DEFAULT = r"(?i)\b(deploy|release|publish|cd|production|prod)\b"


def _git(repo, *args):
    try:
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _q(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return round(v[min(len(v) - 1, int(round(p / 100.0 * (len(v) - 1))))], 1)


def default_branch(repo):
    ref = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
    if ref:
        return ref
    for b in ("origin/main", "origin/master", "main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", b).strip():
            return b
    return "HEAD"


def mainline(repo, branch, since):
    out = _git(repo, "log", "--first-parent", branch, "--since=@%d" % int(since), "--format=%H%x09%ct%x09%at%x09%P%x09%s")
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 5:
            continue
        h, ct, at, parents, subj = parts
        rows.append({"sha": h, "ct": int(ct), "at": int(at), "parents": parents.split(), "subject": subj})
    return rows


def lead_times(repo, rows):
    out = []
    for r in rows:
        if len(r["parents"]) >= 2:
            first = _git(repo, "log", "--format=%at", "%s..%s" % (r["parents"][0], r["parents"][1])).split()
            if first:
                out.append(r["ct"] - min(int(x) for x in first))
        else:
            out.append(max(0, r["ct"] - r["at"]))
    return out


def ci_runs(repo, since, limit=1000):
    """GitHub Actions runs via the gh CLI (read-only). Returns [] when gh is missing or not authenticated."""
    day = time.strftime("%Y-%m-%d", time.gmtime(since))
    try:
        out = subprocess.run(["gh", "run", "list", "-L", str(limit), "--created", ">=" + day,
                              "--json", "workflowName,conclusion,status,headBranch,event,createdAt,startedAt,updatedAt"],
                             cwd=repo, capture_output=True, text=True, timeout=180).stdout
        data = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    runs = []
    for d in data:
        c, s, u = parse_ts(d.get("createdAt")), parse_ts(d.get("startedAt")), parse_ts(d.get("updatedAt"))
        if c is None or u is None:
            continue
        runs.append({"wf": d.get("workflowName") or "?", "ok": d.get("conclusion") == "success", "concl": d.get("conclusion") or d.get("status"),
                     "branch": d.get("headBranch") or "", "event": d.get("event") or "", "created": c, "started": s or c, "ended": u})
    return runs


def load_deploys(path):
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    try:
        items = json.loads(txt)
        if isinstance(items, dict):
            items = items.get("deploys", [])
    except ValueError:
        items = [json.loads(line) for line in txt.splitlines() if line.strip()]
    out = []
    for d in items:
        t = d.get("time")
        t = float(t) if isinstance(t, (int, float)) else parse_ts(t)
        if t is not None:
            e = d.get("end")
            e = float(e) if isinstance(e, (int, float)) else parse_ts(e)
            out.append({"t": t, "ok": bool(d.get("ok")), "dur": (e - t) if e is not None and e >= t else None})
    return sorted(out, key=lambda d: d["t"])


def restore_times(events):
    """events: sorted [(t, ok)] of one stream; failure -> next success."""
    out, fail_at = [], None
    for t, ok in events:
        if not ok and fail_at is None:
            fail_at = t
        elif ok and fail_at is not None:
            out.append(t - fail_at)
            fail_at = None
    return out


def run(repo, since, until=None, branch=None, use_ci=True, deploy_rx=None, deploys_path=None):
    until = until or time.time()
    days = max(1e-9, (until - since) / 86400.0)
    branch = branch or default_branch(repo)
    rows = [r for r in mainline(repo, branch, since) if r["ct"] <= until]
    reverts = [r for r in rows if re.match(r"(?i)revert\b", r["subject"])]
    lt = lead_times(repo, rows)
    rep = {"repo_name": repo.rstrip("/").split("/")[-1], "branch": branch, "window_days": round(days, 2),
           "mainline_changes": len(rows), "merges": sum(1 for r in rows if len(r["parents"]) >= 2), "reverts": len(reverts),
           "metrics": {}, "ci": None, "missing": []}
    m = rep["metrics"]
    m["lead_time_for_changes_h"] = {"p50": _q([x / 3600.0 for x in lt], 50), "p90": _q([x / 3600.0 for x in lt], 90), "n": len(lt),
                                    "source": "git: first commit of merged branch (or author time) -> mainline commit", "kind": "proxy (ends at merge, not deploy)"}
    deploys = None
    if deploys_path:
        deploys = load_deploys(deploys_path)
        deploys = [d for d in deploys if since <= d["t"] <= until]
        src = "deploy log %s" % deploys_path.rstrip("/").split("/")[-1]
    runs = ci_runs(repo, since) if use_ci else []
    runs = [r for r in runs if r["created"] <= until]
    if runs:
        dur = [r["ended"] - r["started"] for r in runs if r["concl"] not in ("skipped", "cancelled")]
        qw = [max(0.0, r["started"] - r["created"]) for r in runs]
        by = defaultdict(lambda: {"runs": 0, "failed": 0, "dur": []})
        for r in runs:
            b = by[r["wf"]]
            b["runs"] += 1
            b["failed"] += 0 if r["ok"] or r["concl"] in ("skipped", "cancelled") else 1
            b["dur"].append(r["ended"] - r["started"])
        main_name = branch.split("/")[-1]
        main_runs = sorted([r for r in runs if r["branch"] == main_name and r["concl"] not in ("skipped", "cancelled")], key=lambda r: r["created"])
        streams = defaultdict(list)
        for r in main_runs:
            streams[r["wf"]].append((r["ended"], r["ok"]))
        rest = [x for ev in streams.values() for x in restore_times(ev)]
        rep["ci"] = {"runs": len(runs), "failed": sum(1 for r in runs if not r["ok"] and r["concl"] not in ("skipped", "cancelled")),
                     "duration_min": {"p50": _q([d / 60 for d in dur], 50), "p90": _q([d / 60 for d in dur], 90), "sum_h": round(sum(dur) / 3600.0, 2)},
                     "queue_s": {"p50": _q(qw, 50), "p90": _q(qw, 90)},
                     "mainline_red_to_green_h": {"p50": _q([x / 3600 for x in rest], 50), "n": len(rest)},
                     "by_workflow": sorted([{"workflow": k, "runs": v["runs"], "failed": v["failed"], "p50_min": _q([d / 60 for d in v["dur"]], 50)} for k, v in by.items()],
                                           key=lambda x: -x["runs"])[:12],
                     "source": "GitHub Actions runs (gh run list)"}
        if deploys is None:
            rx = re.compile(deploy_rx or DEPLOY_RX_DEFAULT)
            dr = [r for r in main_runs if rx.search(r["wf"])]
            if dr:
                deploys = [{"t": r["ended"], "ok": r["ok"]} for r in dr]
                src = "CI workflows matching %s on %s" % (rx.pattern, main_name)
    else:
        rep["missing"].append("CI runs (gh not available, not a GitHub repo, or no runs in the window)")
    if deploys:
        ok = [d for d in deploys if d["ok"]]
        m["deployment_frequency_per_day"] = {"value": round(len(ok) / days, 2), "n": len(ok), "source": src, "kind": "measured"}
        m["change_failure_rate"] = {"value": round(1 - len(ok) / len(deploys), 3), "n": len(deploys), "source": src, "kind": "measured (failed deploy runs, not incidents)"}
        rt = restore_times([(d["t"], d["ok"]) for d in deploys])
        dd = [d["dur"] / 60.0 for d in deploys if d.get("dur") is not None]
        if dd:
            m["deploy_duration_min"] = {"p50": _q(dd, 50), "p90": _q(dd, 90), "sum_h": round(sum(dd) / 60.0, 2), "n": len(dd), "source": src, "kind": "measured"}
        m["time_to_restore_h"] = {"p50": _q([x / 3600 for x in rt], 50), "n": len(rt), "source": src, "kind": "proxy (failed deploy -> next good deploy)"}
    else:
        m["deployment_frequency_per_day"] = {"value": round(len(rows) / days, 2), "n": len(rows), "source": "git mainline commits", "kind": "proxy (no deploy source found)"}
        m["change_failure_rate"] = {"value": round(len(reverts) / len(rows), 3) if rows else None, "n": len(rows), "source": "git reverts / mainline changes", "kind": "proxy (no deploy source found)"}
        m["time_to_restore_h"] = {"p50": None, "n": 0, "source": "-", "kind": "missing (needs deploy or incident data)"}
        rep["missing"].append("deploy events (no deploy workflow matched and no --deploys log): DORA deploy metrics are proxies or missing")
    rep["missing"].append("incidents / production failures: not in git or CI; change failure rate counts failed deploys or reverts only")
    return rep


def render(rep):
    L = ["# Delivery metrics (DORA-style) — %s, %s, last %.1f days" % (rep["repo_name"], rep["branch"], rep["window_days"]), ""]
    L.append("Mainline changes %d (merges %d, reverts %d)." % (rep["mainline_changes"], rep["merges"], rep["reverts"]))
    L.append("")
    L.append("| metric | value | kind | source |")
    L.append("| --- | --- | --- | --- |")
    m = rep["metrics"]
    lt = m["lead_time_for_changes_h"]
    L.append("| lead time for changes | p50 %s h, p90 %s h (n=%d) | %s | %s |" % (lt["p50"], lt["p90"], lt["n"], lt["kind"], lt["source"]))
    for k, label, unit in (("deployment_frequency_per_day", "deployment frequency", "/day"), ("change_failure_rate", "change failure rate", "")):
        d = m[k]
        L.append("| %s | %s%s (n=%d) | %s | %s |" % (label, d["value"], unit, d["n"], d["kind"], d["source"]))
    if m.get("deploy_duration_min"):
        dd = m["deploy_duration_min"]
        L.append("| deploy duration | p50 %s min, p90 %s min, %s h total (n=%d) | measured | %s |" % (dd["p50"], dd["p90"], dd["sum_h"], dd["n"], dd["source"]))
    tr = m["time_to_restore_h"]
    L.append("| time to restore | %s | %s | %s |" % (("p50 %s h (n=%d)" % (tr["p50"], tr["n"])) if tr["p50"] is not None else "-", tr["kind"], tr["source"]))
    ci = rep.get("ci")
    if ci:
        L.append("")
        L.append("CI: %d runs, %d failed, duration p50 %s min / p90 %s min (%s h total), queue p50 %ss / p90 %ss, mainline red->green p50 %s h (n=%d)." % (
            ci["runs"], ci["failed"], ci["duration_min"]["p50"], ci["duration_min"]["p90"], ci["duration_min"]["sum_h"], ci["queue_s"]["p50"], ci["queue_s"]["p90"],
            ci["mainline_red_to_green_h"]["p50"], ci["mainline_red_to_green_h"]["n"]))
        for w in ci["by_workflow"][:6]:
            L.append("- %s: %d runs, %d failed, p50 %s min" % (w["workflow"], w["runs"], w["failed"], w["p50_min"]))
    if rep["missing"]:
        L.append("")
        L.append("Missing: " + "; ".join(rep["missing"]))
    return "\n".join(L) + "\n"
