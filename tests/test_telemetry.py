"""Tests for measured telemetry, DORA, and the opt-in watch hook. Synthetic data only."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)

from fixtures import Tx, iso, make_project  # noqa: E402
from rbn import core, dora, telemetry, watch as watchmod  # noqa: E402
from rbn.parse import parse_session  # noqa: E402
from rbn.rules import RuleSet  # noqa: E402
import analyze as cli  # noqa: E402

RS = RuleSet()


def with_sends(tx, path, sends):
    """Append api-request send records (as Claude Code writes them) to a written transcript."""
    with open(path, "a", encoding="utf-8") as f:
        for t in sends:
            f.write(json.dumps({"type": "api-request", "sessionId": tx.sid, "id": "x%d" % t, "timestamp": iso(t), "querySource": "repl_main_thread"}) + "\n")
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()


class TestLatency(Tmp):
    def test_send_to_first_block(self):
        tx = Tx()
        tx.human(0)
        tx.tool(10, 12, "Bash", {"command": "ls"}, usage={"output_tokens": 400})   # sent at 1 -> first block at 10
        tx.text(30, usage={"output_tokens": 100})                                    # sent at 13 -> 30
        p = with_sends(tx, tx.write(os.path.join(self.tmp, "s.jsonl")), [1, 13])
        S = parse_session(p, RS)
        self.assertEqual(len(S["api_sends"]), 2)
        lat = telemetry.latency([S])
        self.assertEqual(lat["matched"], 2)
        self.assertEqual(lat["first_block_s"]["max"], 17.0)
        self.assertEqual(lat["cold_start"]["n_first"], 1)

    def test_no_send_records_is_missing_not_zero(self):
        tx = Tx()
        tx.human(0)
        tx.text(5)
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        lat = telemetry.latency([S])
        self.assertEqual(lat["matched"], 0)
        self.assertIn("no api-request", lat["basis"])


class TestMeasuresAndHooks(Tmp):
    def test_tool_printed_durations(self):
        tx = Tx()
        tx.human(0)
        tx.tool(1, 40, "Bash", {"command": "npx vitest run"}, result=" Test Files  3 passed (3)\n      Tests  12 passed (12)\n   Duration  12.5s (transform 1s)")
        tx.tool(41, 60, "Bash", {"command": "pytest -q"}, result="........ \n===== 8 passed in 3.21s =====")
        tx.tool(61, 70, "Bash", {"command": "npm run dev"}, result="  ▲ Next.js 15\n ✓ Ready in 850ms")
        tx.tool(71, 80, "Bash", {"command": "time make"}, result="done\nreal\t1m2.500s\nuser\t0m1s")
        tx.system(81, "stop_hook_summary", hookInfos=[{"command": "a", "durationMs": 120}, {"command": "b", "durationMs": 30}], hookErrors=[])
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        got = {m["id"]: round(m["sec"], 2) for m in S["measures"]}
        self.assertEqual(got["vitest_duration"], 12.5)
        self.assertEqual(got["pytest_time"], 3.21)
        self.assertEqual(got["next_ready"], 0.85)
        self.assertEqual(got["shell_time"], 62.5)
        hk = telemetry.hooks([S])
        self.assertEqual(hk["stop_hook_runs"], 1)
        self.assertEqual(hk["ms_per_stop"]["max"], 150)

    def test_read_only_output_not_measured(self):
        tx = Tx()
        tx.human(0)
        tx.tool(1, 2, "Bash", {"command": "cat old-test.log"}, result="===== 8 passed in 3.21s =====")
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        self.assertEqual(S["measures"], [])


class TestJobs(Tmp):
    def test_queue_and_scope(self):
        d = os.path.join(self.tmp, "jobs")
        os.makedirs(d)
        now = 1_800_000_000
        recs = [{"name": "build", "cwd": "/w/app", "started": now, "ended": now + 100, "gate_wait_s": 30, "peak": 2 * 2 ** 30, "exit": 0, "reason": None},
                {"name": "test", "cwd": "/w/app/sub", "started": now + 10, "ended": now + 20, "gate_wait_s": 0, "peak": 2 ** 30, "exit": 137, "reason": "limit"},
                {"name": "other", "cwd": "/w/other", "started": now, "ended": now + 5, "gate_wait_s": 99},
                {"name": "scratch", "cwd": "/tmp/x/-w-app/sess/scratchpad", "started": now, "ended": now + 5, "queue_wait_s": 1}]
        for i, r in enumerate(recs):
            with open(os.path.join(d, "%d.json" % i), "w") as f:
                json.dump(r, f)
        j = telemetry.jobs(d, now - 10, now + 1000, ["/w/app"], ["-w-app"])
        self.assertEqual(j["jobs"], 3)
        self.assertEqual(j["queue_wait_s"]["sum"], 31.0)
        self.assertEqual(j["killed"], 1)
        j2 = telemetry.jobs(d, now - 86400 * 7, now + 1000, ["/w/app"])
        self.assertIn("pruned", j2["gap"])


class TestCoverage(Tmp):
    def test_matrix_names_missing(self):
        root = make_project(self.tmp)
        tx = Tx()
        tx.human(0)
        tx.tool(1, 30, "Bash", {"command": "npm test"})
        tx.text(31)
        tx.write(os.path.join(root, "s1.jsonl"))
        import argparse
        ns = argparse.Namespace(project="proj-demo", all=False, session=None, since_ts=None, until_ts=None, idle_threshold=180.0, top=8,
                                machine_log=None, token_minute_equiv=0.0, sysstat=None, jobs=None, dora_json=None, host_snapshot=False)
        rep = core.run(ns, RS, root=self.tmp)
        cov = {c["dimension"]: c["status"] for c in rep["telemetry"]["coverage"]}
        self.assertEqual(cov["API latency"], "missing")
        self.assertEqual(cov["CI / DORA"], "missing")
        self.assertEqual(cov["queues / locks"], "missing")
        self.assertEqual(cov["CPU"], "inferred")
        self.assertEqual(cov["wall-clock attribution"], "measured")


def git(repo, *a, dates=None):
    """git in the test repo with a fixed identity; dates: (author, committer) unix times for this call only."""
    if dates:
        os.environ["GIT_AUTHOR_DATE"] = "@%d +0000" % dates[0]
        os.environ["GIT_COMMITTER_DATE"] = "@%d +0000" % dates[1]
    try:
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid"] + list(a),
                       check=True, capture_output=True)
    finally:
        if dates:
            os.environ.pop("GIT_AUTHOR_DATE", None)
            os.environ.pop("GIT_COMMITTER_DATE", None)


class TestDora(Tmp):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "r")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        now = int(time.time())
        for i, (msg, at, ct) in enumerate((("feat a", now - 7200, now - 3600), ("Revert \"feat a\"", now - 1800, now - 1800), ("feat b", now - 600, now - 600))):
            with open(os.path.join(self.repo, "f%d" % i), "w") as f:
                f.write("x")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", msg, dates=(at, ct))

    def test_proxies_are_labelled(self):
        rep = dora.run(self.repo, time.time() - 86400, branch="main")
        m = rep["metrics"]
        self.assertEqual(rep["mainline_changes"], 3)
        self.assertEqual(rep["reverts"], 1)
        self.assertIn("proxy", m["deployment_frequency_per_day"]["kind"])
        self.assertIn("missing", m["time_to_restore_h"]["kind"])
        self.assertEqual(m["lead_time_for_changes_h"]["p90"], 1.0)
        self.assertTrue(any("deploy events" in x for x in rep["missing"]))

    def test_deploy_log_is_measured(self):
        now = time.time()
        p = os.path.join(self.tmp, "deploys.jsonl")
        with open(p, "w") as f:
            for t, ok in ((now - 5000, True), (now - 4000, False), (now - 3000, True), (now - 100, True)):
                f.write(json.dumps({"time": t, "end": t + 240, "ok": ok}) + "\n")
        rep = dora.run(self.repo, now - 86400, branch="main", deploys_path=p)
        m = rep["metrics"]
        self.assertEqual(m["deployment_frequency_per_day"]["kind"], "measured")
        self.assertEqual(m["change_failure_rate"]["value"], 0.25)
        self.assertEqual(m["deploy_duration_min"]["p50"], 4.0)
        self.assertAlmostEqual(m["time_to_restore_h"]["p50"], 0.3, places=1)

    def test_ci_runs_come_only_from_an_exported_file(self):
        rep = dora.run(self.repo, time.time() - 86400, branch="main")
        self.assertIsNone(rep["ci"])
        self.assertTrue(any("--ci-runs" in x for x in rep["missing"]))
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        now = time.time()
        runs = [{"workflowName": "Deploy", "conclusion": c, "status": "completed", "headBranch": "main", "event": "push",
                 "createdAt": iso(t), "startedAt": iso(t + 30), "updatedAt": iso(t + 330)}
                for t, c in ((now - 7000, "success"), (now - 5000, "failure"), (now - 3000, "success"))]
        runs.append(dict(runs[0], createdAt=iso(now - 5 * 86400), startedAt=iso(now - 5 * 86400), updatedAt=iso(now - 5 * 86400 + 60)))
        p = os.path.join(self.tmp, "runs.json")
        with open(p, "w") as f:
            json.dump(runs, f)
        rep = dora.run(self.repo, now - 86400, branch="main", ci_runs_path=p)
        self.assertEqual(rep["ci"]["runs"], 3)  # the run before the window is left out
        self.assertEqual(rep["ci"]["failed"], 1)
        self.assertEqual(rep["ci"]["source"], "GitHub Actions runs (runs.json)")
        self.assertEqual(rep["metrics"]["deployment_frequency_per_day"]["kind"], "measured")

    def test_no_network_or_credential_tools_are_called(self):
        src = open(os.path.join(os.path.dirname(dora.__file__), "dora.py"), encoding="utf-8").read()
        self.assertNotIn('"gh"', src)


class TestWatchOptIn(Tmp):
    def _tx(self, root):
        tx = Tx()
        tx.human(0)
        tx.tool(1, 900, "Bash", {"command": "sleep 300; ls"})
        tx.text(901)
        return tx.write(os.path.join(root, "s1.jsonl"))

    def test_off_switch_and_detach_returns_fast(self):
        root = make_project(self.tmp)
        p = self._tx(root)
        st = os.path.join(self.tmp, "state")
        with redirect_stdout(io.StringIO()):
            cli.main(["watch-ctl", "off", "--state-dir", st])
        self.assertTrue(watchmod.disabled(st))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["watch", "--transcript", p, "--state-dir", st]), 0)
        self.assertFalse(os.path.exists(os.path.join(st, "history.jsonl")))
        with redirect_stdout(io.StringIO()):
            cli.main(["watch-ctl", "on", "--state-dir", st])
        self.assertFalse(watchmod.disabled(st))
        t0 = time.time()
        with redirect_stdout(io.StringIO()):
            cli.main(["watch", "--transcript", p, "--state-dir", st, "--detach"])
        self.assertLess(time.time() - t0, 2.0)

    def test_sync_run_writes_audit_and_pending(self):
        root = make_project(self.tmp)
        p = self._tx(root)
        st = os.path.join(self.tmp, "state")
        with redirect_stdout(io.StringIO()):
            cli.main(["watch", "--transcript", p, "--state-dir", st, "--pending"])
        rows = watchmod.read_history(os.path.join(st, "history.jsonl"))
        self.assertEqual(len(rows), 1)
        a = rows[0]["audit"]
        for k in ("tool_version", "rules_sha", "analysis_s", "transcript", "transcript_bytes", "recompute"):
            self.assertIn(k, a)
        pend = watchmod.take_pending(st)
        self.assertTrue(any("poll" in m for m in pend), pend)
        self.assertEqual(watchmod.take_pending(st), [])  # delivered once
        # unchanged transcript: skipped, no second line
        with redirect_stdout(io.StringIO()):
            cli.main(["watch", "--transcript", p, "--state-dir", st])
        self.assertEqual(len(watchmod.read_history(os.path.join(st, "history.jsonl"))), 1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["watch", "--report", "--state-dir", st])
        self.assertIn("Own overhead", buf.getvalue())

    def test_snippet_does_not_touch_settings(self):
        buf = io.StringIO()
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with redirect_stdout(buf):
            cli.main(["watch-ctl", "print-config", "--state-dir", os.path.join(self.tmp, "st")])
        snip = json.loads(buf.getvalue().split("\n\n#")[0])
        self.assertIn("--detach", snip["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertEqual(os.listdir(home), [])

    def test_broken_input_never_fails_the_hook(self):
        st = os.path.join(self.tmp, "state")
        bad = os.path.join(self.tmp, "bad.jsonl")
        with open(bad, "w") as f:
            f.write("not json\n")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["watch", "--transcript", bad, "--state-dir", st]), 0)


if __name__ == "__main__":
    unittest.main()
