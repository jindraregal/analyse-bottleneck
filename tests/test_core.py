import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)

from fixtures import Tx, make_project  # noqa: E402
from rbn import core, compare as cmpmod, watch as watchmod  # noqa: E402
from rbn.classify import classify_bash  # noqa: E402
from rbn.parse import parse_session  # noqa: E402
from rbn.rules import RuleSet  # noqa: E402
from rbn.timeline import analyze_timeline  # noqa: E402
from rbn.util import match_text, norm_command  # noqa: E402
import analyze as cli  # noqa: E402

RS = RuleSet()


def timeline_for(tx, tmp, name="s.jsonl"):
    p = tx.write(os.path.join(tmp, name))
    S = parse_session(p, RS)
    return S, analyze_timeline(S, RS)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()


class TestRulesFiles(unittest.TestCase):
    def test_no_warnings(self):
        self.assertEqual(RS.warnings, [])

    def test_stack_rules_schema(self):
        ids = set()
        self.assertGreaterEqual(len(RS.stack), 20)
        for r in RS.stack:
            self.assertNotIn(r["id"], ids)
            ids.add(r["id"])
            for k in ("title", "why", "fix", "before", "after", "confidence", "evidence", "estimate", "detector"):
                self.assertIn(k, r, r["id"])
            self.assertIn(r["confidence"], ("low", "medium", "high"))
            self.assertIn("assumption", r["estimate"], r["id"])

    def test_required_topics_present(self):
        need = ["next-build-as-verification", "next-wiped-cache", "next-turbopack-not-used", "next-dev-restarts", "js-npm-vs-pnpm-bun", "py-pip-vs-uv",
                "js-webpack-vs-rspack-vite", "js-jest-vs-vitest", "js-eslint-prettier-vs-biome", "ts-tsc-not-incremental", "test-full-suite-vs-affected",
                "docker-build-no-cache", "search-grep-find-vs-rg-fd", "native-cache-wiped", "playwright-full-rerun", "media-ffmpeg-software-encode",
                "media-whisper-cpu", "git-repeated-clone", "ci-polled-by-sleep"]
        have = {r["id"] for r in RS.stack}
        for n in need:
            self.assertIn(n, have)

    def test_no_personal_strings_in_public_files(self):
        bad = ["/" + "home/", "@" + "gmail", "/" + "Users/", "jind" + "ra", "orb" + "it-capital"]
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in ("reports", "__pycache__", ".git", ".playwright-mcp")]
            for f in files:
                if f.endswith((".pyc",)):
                    continue
                with open(os.path.join(base, f), encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read().lower()
                # the project's own public links, its GitHub owner and the marketplace named after it
                # (report footer, plugin manifests, install command) are allowed
                for ok in ("github.com/" + "jind" + "raregal/analyse-bottleneck", "jind" + "raregal/analyse-bottleneck",
                           "github.com/" + "jind" + "raregal", "jind" + "raregal.com", '"' + "jind" + 'raregal"',
                           "analyse-bottleneck@" + "jind" + "raregal"):
                    txt = txt.replace(ok, "")
                for b in bad:
                    self.assertNotIn(b.lower(), txt, "%s contains %s" % (f, b))


class TestClassify(unittest.TestCase):
    def test_table(self):
        cases = {
            "npm run build": "build", "cd app && next build": "build", "pytest -q tests/": "test", "npm ci": "install", "pip install requests": "install",
            "npx eslint .": "lint_typecheck", "git status": "git", "curl -s https://example.com": "network", "ffmpeg -i a.mp4 b.mp4": "media_gpu",
            "sleep 30": "poll_wait", "ls -la": "file_ops", "gh run watch": "poll_wait", "echo hello": "file_ops",
            "git commit -m 'fix the test and build'": "git", "cat notes.md | head": "file_ops", "grep -c x vitest-run.log": "file_ops",
            "timeout 600 bash -c 'until test -f done; do sleep 5; done'": "poll_wait",
        }
        for cmd, want in cases.items():
            self.assertEqual(classify_bash(cmd, RS), want, cmd)

    def test_heredoc_and_quotes_ignored(self):
        cmd = "python3 - <<'PY'\nprint('npm run build and pytest')\nPY"
        self.assertNotIn("pytest", match_text(cmd))
        self.assertEqual(classify_bash(cmd, RS), "script_run")

    def test_norm_command_masks_numbers(self):
        self.assertEqual(norm_command("cd x && sleep 30"), norm_command("sleep 45"))


class TestTimeline(TmpCase):
    def test_sums_to_span_and_categories(self):
        tx = Tx()
        tx.human(0)
        tx.tool(14, 74, "Bash", {"command": "npm run build"}, think_ms=8000)   # 60 s build; request 0-14 incl 8 s thinking
        tx.tool(80, 110, "Bash", {"command": "sleep 30"})
        tx.text(115)
        tx.human(415, "next")  # 300 s human wait
        tx.tool(420, 425, "Read", {"file_path": "/x/a.txt"})
        tx.text(430)
        S, tl = timeline_for(tx, self.tmp)
        by = tl["by_cat"]
        self.assertAlmostEqual(sum(by.values()), tl["span"], places=3)
        self.assertAlmostEqual(tl["span"], 430, places=3)
        self.assertAlmostEqual(by["build"], 60, delta=0.5)
        self.assertAlmostEqual(by["poll_wait"], 30, delta=0.5)
        self.assertAlmostEqual(by["thinking"], 8, delta=0.5)
        self.assertAlmostEqual(by["human_wait:prompt"], 300, delta=1.0)
        self.assertAlmostEqual(tl["autonomous"], 430 - 300, delta=1.0)
        self.assertAlmostEqual(tl["human"], 300, delta=1.0)

    def test_gated_approval_is_human_wait(self):
        tx = Tx()
        tx.human(0)
        tx.tool(5, 305, "Bash", {"command": "GATE_REASON='why' ssh host 'uptime'"})
        tx.text(310)
        S, tl = timeline_for(tx, self.tmp)
        self.assertAlmostEqual(tl["by_cat"]["human_wait:approval"], 300, delta=1)
        self.assertLess(tl["autonomous"], 20)

    def test_parallel_tools_use_interval_union(self):
        tx = Tx()
        tx.human(0)
        a = tx.use_only(10, "Bash", {"command": "npm run build"})
        b = tx.use_only(10.5, "Bash", {"command": "pytest -q"})
        tx.result(70, a)
        tx.result(100, b)
        tx.text(105)
        S, tl = timeline_for(tx, self.tmp)
        by = tl["by_cat"]
        self.assertAlmostEqual(sum(by.values()), tl["span"], places=3)
        self.assertAlmostEqual(by["build"] + by["test"], 90, delta=1.0)  # union 10..100, not 60 + 90
        self.assertGreater(tl["parallel_factor"], 1.4)

    def test_idle_gap_separate_from_human(self):
        tx = Tx()
        tx.human(0)
        tx.tool(5, 6, "Bash", {"command": "ls"})
        tx.text(10)
        tx.rows.append({"type": "user", "timestamp": "2026-01-01T12:16:40.000Z", "sessionId": "sess-a", "origin": {"kind": "task-notification"},
                        "message": {"role": "user", "content": "<task-notification><status>completed</status></task-notification>"}})
        tx.text(1005)  # 990 s of nothing, then a machine-generated wake-up, no human prompt
        S, tl = timeline_for(tx, self.tmp)
        self.assertGreater(tl["by_cat"].get("idle_gap", 0), 900)
        self.assertEqual(tl["by_cat"].get("human_wait:prompt", 0), 0)
        self.assertLess(tl["autonomous"], 100)

    def test_human_tools_excluded_from_autonomous(self):
        tx = Tx()
        tx.human(0)
        tx.tool(5, 205, "AskUserQuestion", {"question": "which?"})
        tx.text(210)
        S, tl = timeline_for(tx, self.tmp)
        self.assertAlmostEqual(tl["by_cat"]["human_wait:ask"], 200, delta=1.0)
        self.assertLess(tl["autonomous"], 20)

    def test_permission_wait_inferred_only_when_not_bypass(self):
        for perm, expect in (("default", True), ("bypassPermissions", False)):
            tx = Tx(perm=perm)
            tx.human(0)
            tx.tool(5, 125, "Read", {"file_path": "/x/y"})  # a Read taking 120 s
            tx.text(130)
            S, tl = timeline_for(tx, self.tmp, "p-%s.jsonl" % perm)
            self.assertEqual("human_wait:permission" in tl["by_cat"], expect, perm)

    def test_background_and_subagent_wait(self):
        tx = Tx()
        tx.human(0)
        tid = tx.tool(5, 6, "Bash", {"command": "npm run build", "run_in_background": True}, tur={"backgroundTaskId": "bg1"})
        tx.rows.append({"type": "user", "timestamp": "2026-01-01T12:03:06.000Z", "sessionId": "sess-a",
                        "message": {"role": "user", "content": "<task-notification><task-id>bg1</task-id><tool-use-id>%s</tool-use-id><status>completed</status></task-notification>" % tid},
                        "origin": {"kind": "task-notification"}})
        tx.text(190)
        S, tl = timeline_for(tx, self.tmp)
        self.assertAlmostEqual(tl["by_cat"]["background_wait:build"], 180, delta=3)

    def test_background_server_lifetime_is_not_blocked_time(self):
        tx = Tx()
        tx.human(0)
        tid = tx.tool(5, 6, "Bash", {"command": "python3 app.py serve", "run_in_background": True}, tur={"backgroundTaskId": "bg2"})
        tx.rows.append({"type": "user", "timestamp": "2026-01-01T12:10:06.000Z", "sessionId": "sess-a",
                        "message": {"role": "user", "content": "<task-notification><task-id>bg2</task-id><tool-use-id>%s</tool-use-id><status>killed</status></task-notification>" % tid},
                        "origin": {"kind": "task-notification"}})
        tx.text(610)
        S, tl = timeline_for(tx, self.tmp)
        self.assertFalse([k for k in tl["by_cat"] if k.startswith("background_wait")])
        self.assertLess(tl["autonomous"], 30)

    def test_tolerates_garbage_and_unknown_records(self):
        tx = Tx()
        tx.human(0)
        tx.text(10)
        S, tl = timeline_for(tx, self.tmp)
        self.assertEqual(S["bad_lines"], 1)
        self.assertAlmostEqual(tl["span"], 10, places=3)


class TestApiWait(TmpCase):
    def test_stalled_generation_is_api_wait(self):
        tx = Tx()
        tx.human(0)
        tx.tool(5, 6, "Bash", {"command": "ls"})
        tx.tool(406, 407, "Bash", {"command": "ls"}, usage={"output_tokens": 30})  # 400 s to emit 30 tokens
        tx.text(410)
        S, tl = timeline_for(tx, self.tmp)
        self.assertGreater(tl["by_cat"].get("api_wait", 0), 300)
        self.assertAlmostEqual(sum(tl["by_cat"].values()), tl["span"], places=3)


class TestSignals(TmpCase):
    def test_signals_and_confidence(self):
        tx = Tx()
        tx.human(0)
        tx.tool(5, 8, "Bash", {"command": "npm run build"}, result="Exit code 1\nENOSPC: no space left on device, write", is_error=True)
        tx.tool(10, 12, "Bash", {"command": "make"}, result="Exit code 137\nKilled", is_error=True)
        tx.tool(15, 16, "Read", {"file_path": "/x/doc.md"}, result="notes about ENOSPC and OOM and 429 errors")
        tx.tool(20, 21, "mcp__mail__read", {}, result="Subject: heap out of memory FATAL ERROR")
        tx.tool(25, 26, "Bash", {"command": "python3 gpu_job.py"}, result="RuntimeError: CUDA is not available", is_error=True)
        tx.tool(30, 31, "Bash", {"command": "cat log.txt"}, result="ENOSPC mentioned in a cat of a file")
        tx.text(35)
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        ids = {(s["id"], s["conf"]) for s in S["signals"]}
        self.assertIn(("disk_full", "high"), ids)
        self.assertIn(("sigkill", "medium"), ids)
        self.assertNotIn(("oom", "high"), ids)  # exit 137 alone is not proof of an OOM kill
        self.assertIn(("gpu_absent", "high"), ids)
        self.assertEqual(sum(1 for s in S["signals"] if s["id"] == "disk_full"), 1)  # Read, mcp, cat outputs are not scanned


class TestQuota(TmpCase):
    def test_cache_miss_after_idle_and_hit_rate(self):
        from rbn.quota import analyze_quota
        tx = Tx()
        tx.human(0)
        tx.tool(5, 6, "Bash", {"command": "ls"}, usage={"cache_read_input_tokens": 50000, "cache_creation_input_tokens": 100})
        tx.tool(10, 880, "Bash", {"command": "make"}, usage={"cache_read_input_tokens": 50100, "cache_creation_input_tokens": 100})  # 14 min tool call
        tx.tool(890, 891, "Bash", {"command": "ls -l"}, usage={"cache_read_input_tokens": 0, "cache_creation_input_tokens": 50300})
        tx.text(895, usage={"cache_read_input_tokens": 50300, "cache_creation_input_tokens": 10})
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        q = analyze_quota([S], RS)
        self.assertEqual(q["cache_misses"]["by_cause"]["idle_over_ttl"]["count"], 1)
        self.assertGreater(q["cache_misses"]["waste_units"], 100000)
        self.assertLess(q["cache_hit_rate"], 0.8)
        self.assertEqual(q["requests"], 4)

    def test_rebuild_after_human_prompt_is_not_waste(self):
        from rbn.quota import analyze_quota
        tx = Tx()
        tx.human(0)
        tx.tool(5, 6, "Bash", {"command": "ls"}, usage={"cache_read_input_tokens": 50000, "cache_creation_input_tokens": 100})
        tx.text(10, usage={"cache_read_input_tokens": 50100, "cache_creation_input_tokens": 100})
        tx.human(2000, "back again")
        tx.tool(2005, 2006, "Bash", {"command": "ls -l"}, usage={"cache_read_input_tokens": 0, "cache_creation_input_tokens": 50300})
        tx.text(2010, usage={"cache_read_input_tokens": 50300, "cache_creation_input_tokens": 10})
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        q = analyze_quota([S], RS)
        self.assertEqual(q["cache_misses"]["count"], 0)
        self.assertEqual(q["cache_misses"]["waste_units"], 0)

    def test_repeated_reads_respect_edits_and_ranges(self):
        from rbn.quota import repeated_reads
        tx = Tx()
        tx.human(0)
        tx.tool(5, 6, "Read", {"file_path": "/x/a.py"}, result="x" * 4000)
        tx.tool(8, 9, "Read", {"file_path": "/x/a.py"}, result="x" * 4000)       # repeat
        tx.tool(10, 11, "Edit", {"file_path": "/x/a.py", "old_string": "a", "new_string": "b"})
        tx.tool(12, 13, "Read", {"file_path": "/x/a.py"}, result="x" * 4000)      # after edit: fine
        tx.tool(14, 15, "Read", {"file_path": "/x/b.py", "offset": 1, "limit": 10}, result="y" * 400)
        tx.tool(16, 17, "Read", {"file_path": "/x/b.py", "offset": 50, "limit": 10}, result="y" * 400)  # other range: fine
        tx.text(20)
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        r = repeated_reads([S], RS)
        self.assertEqual(r["repeat_reads"], 1)
        self.assertEqual(r["wasted_tokens"], 1000)

    def test_repeated_commands_need_identical_output(self):
        from rbn.quota import repeated_commands
        tx = Tx()
        tx.human(0)
        t = 5
        for i in range(4):
            tx.tool(t, t + 2, "Bash", {"command": "git status --short"}, result="same output")
            t += 5
        for i in range(4):
            tx.tool(t, t + 2, "Bash", {"command": "tail -n 3 app.log"}, result="different %d" % i)
            t += 5
        tx.text(t)
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        r = repeated_commands([S], RS)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["top"][0]["redundant"], 3)

    def test_oversized_and_polling(self):
        from rbn.quota import analyze_quota
        tx = Tx()
        tx.human(0)
        tx.tool(5, 6, "Bash", {"command": "npm test"}, result="line\n" * 20000)
        t = 10
        for i in range(6):
            tx.tool(t, t + 30, "Bash", {"command": "sleep 30"})
            t += 40
        tx.text(t)
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        q = analyze_quota([S], RS)
        self.assertEqual(q["oversized_results"]["count"], 1)
        self.assertGreater(q["oversized_results"]["turns_carried"] if "turns_carried" in q["oversized_results"] else q["oversized_results"]["top"][0]["turns_carried"], 3)
        self.assertEqual(q["polling"]["polls"], 6)

    def test_model_fit_flags_mechanical_top_tier_turns(self):
        from rbn.quota import analyze_quota
        tx = Tx()
        tx.human(0)
        t = 5
        for i in range(12):
            tx.tool(t, t + 1, "Bash", {"command": "git status"}, usage={"output_tokens": 40})
            t += 3
        tx.text(t)
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        q = analyze_quota([S], RS)
        self.assertEqual(q["model_fit"]["mechanical_requests"], 12)
        self.assertGreater(q["model_fit"]["potential_saving_units"], 0)


class TestStackRules(TmpCase):
    def _run(self, cmds, durations, session_extra=None):
        from rbn.stack import evaluate
        tx = Tx()
        tx.human(0)
        t = 5
        for c, d in zip(cmds, durations):
            tx.tool(t, t + d, "Bash", {"command": c})
            t += d + 3
        tx.text(t)
        S = parse_session(tx.write(os.path.join(self.tmp, "s.jsonl")), RS)
        return evaluate([S], RS, [], 4000)

    def ids(self, f):
        return {x["id"] for x in f}

    def test_next_build_fires_with_evidence_and_arithmetic(self):
        f = self._run(["cd web && npm run build", "next build", "npm run build", "ls next.config.js"], [70, 65, 80, 1])
        hit = [x for x in f if x["id"] == "next-build-as-verification"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["events"], 3)
        self.assertAlmostEqual(hit[0]["saving_seconds"], (70 + 65 + 80) * 0.7, delta=1)
        self.assertIn("x 0.70", hit[0]["arithmetic"]["time"])

    def test_compound_command_time_goes_to_its_own_class(self):
        # a long test run that merely contains a grep must not be charged to the grep rule
        f = self._run(["npx vitest run src | grep -E fail | head -5"] * 4 + ["grep -rn foo src | head"] * 3, [200, 200, 200, 200, 60, 60, 60])
        hit = [x for x in f if x["id"] == "search-grep-find-vs-rg-fd"]
        if hit:
            self.assertLess(hit[0]["measured_seconds"], 300)

    def test_below_threshold_does_not_fire(self):
        f = self._run(["next build", "next build"], [30, 30])
        self.assertNotIn("next-build-as-verification", self.ids(f))

    def test_wiped_cache_requires_following_build(self):
        f = self._run(["rm -rf .next", "next build", "rm -rf .next", "next build", "ls"], [1, 50, 1, 50, 1])
        self.assertIn("next-wiped-cache", self.ids(f))
        g = self._run(["rm -rf .next", "ls", "rm -rf .next", "ls"], [1, 1, 1, 1])
        self.assertNotIn("next-wiped-cache", self.ids(g))

    def test_turbopack_absent_check(self):
        cmds = ["next dev"] * 4 + ["next build"] * 2
        f = self._run(cmds, [60] * 6)
        self.assertIn("next-turbopack-not-used", self.ids(f))
        g = self._run(["next dev --turbopack"] * 5, [60] * 5)
        self.assertNotIn("next-turbopack-not-used", self.ids(g))

    def test_no_double_credit(self):
        f = self._run(["npm ci"] * 5, [40] * 5)
        fired = [x for x in f if x["id"] in ("node-modules-per-worktree", "js-npm-vs-pnpm-bun")]
        self.assertGreaterEqual(len(fired), 1)
        measured = sum(x["measured_seconds"] for x in fired[:1])
        # overlapping rules never credit more than the measured time in total
        self.assertLessEqual(sum(x["saving_seconds"] for x in fired), measured + 0.01)

    def test_other_stacks(self):
        f = self._run(["pip install -r r.txt"] * 3 + ["ffmpeg -i a.mp4 -c:v libx264 o.mp4"] * 3 + ["whisper a.mp3 --model small"] + ["grep -rn foo ."] * 8
                      + ["git clone https://example.com/a.git"] * 2, [20] * 3 + [60] * 3 + [150] + [4] * 8 + [30] * 2)
        ids = self.ids(f)
        for want in ("py-pip-vs-uv", "media-ffmpeg-software-encode", "media-whisper-cpu", "search-grep-find-vs-rg-fd", "git-repeated-clone"):
            self.assertIn(want, ids)

    def test_ci_poll_loop_inside_one_command_is_not_flagged(self):
        f = self._run(["sleep 60; gh run view 1", "sleep 60; gh run view 1", "sleep 60; gh run view 1"], [60] * 3)
        self.assertIn("ci-polled-by-sleep", self.ids(f))
        g = self._run(["for i in 1 2 3; do gh run view 1; sleep 30; done"] * 4, [90] * 4)
        self.assertNotIn("ci-polled-by-sleep", self.ids(g))


class TestEndToEnd(TmpCase):
    def make(self):
        proj = make_project(self.tmp, "proj-demo")
        tx = Tx("aaaa1111")
        tx.human(0)
        tx.tool(10, 80, "Bash", {"command": "npm run build"})
        agent = tx.tool(85, 205, "Agent", {"description": "sub job", "prompt": "x"}, tur={"agentId": "sub1"})
        tx.text(210)
        tx.human(500)
        tx.tool(505, 506, "Bash", {"command": "ls"})
        tx.text(510)
        tx.write(os.path.join(proj, "aaaa1111.jsonl"))
        sub = Tx("agent-sub1", sidechain=True)
        sub.tool(90, 190, "Bash", {"command": "pytest -q"})
        sub.text(200)
        sub.write(os.path.join(proj, "aaaa1111", "subagents", "agent-sub1.jsonl"))
        return proj

    def run_cli(self, argv):
        os.environ["RB_PROJECTS_DIR"] = self.tmp
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_json_output_and_attribution(self):
        self.make()
        rc, out = self.run_cli(["--project", "proj-demo", "--format", "json", "--no-json-file"])
        self.assertEqual(rc, 0)
        rep = json.loads(out)
        w = rep["wall_clock"]
        self.assertAlmostEqual(w["sum_check_pct"], 100.0, delta=0.01)
        cats = {r["category"]: r["seconds"] for r in w["categories"]}
        self.assertAlmostEqual(cats["build"], 70, delta=1)
        self.assertGreater(cats["subagent_wait"], 100)
        self.assertGreater(w["inside_subagent_wait_s"].get("test", 0), 80)  # what the subagent was doing
        self.assertGreater(w["human_s"], 250)
        self.assertEqual(rep["meta"]["sessions"], 1)

    def test_markdown_is_short(self):
        self.make()
        rc, out = self.run_cli(["--project", "proj-demo", "--no-json-file"])
        self.assertEqual(rc, 0)
        self.assertIn("Where the time went", out)
        self.assertLess(len(out.splitlines()), 90)

    def test_time_window_filters(self):
        self.make()
        rc, out = self.run_cli(["--project", "proj-demo", "--format", "json", "--no-json-file", "--since", "2027-01-01"])
        self.assertEqual(rc, 2)

    def test_compare_and_watch(self):
        self.make()
        os.environ["RB_PROJECTS_DIR"] = self.tmp
        rc, out = self.run_cli(["compare", "--project", "proj-demo", "--before", "session:aaaa", "--after", "session:aaaa", "--format", "json"])
        self.assertEqual(rc, 0)
        cmp = json.loads(out)
        self.assertIn("deltas", cmp)
        hist = os.path.join(self.tmp, "hist.jsonl")
        rc, out = self.run_cli(["watch", "--project", "proj-demo", "--history", hist, "--session", "latest",
                                "--thresholds", self._thr()])
        self.assertEqual(rc, 0)
        self.assertIn("analyse-bottleneck", out)           # idle gap or class share crossed a threshold
        with open(hist) as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)
        rc, out2 = self.run_cli(["watch", "--project", "proj-demo", "--history", hist, "--session", "latest", "--thresholds", self._thr()])
        self.assertEqual(out2.strip(), "")             # same warning is not repeated

    def _thr(self):
        p = os.path.join(self.tmp, "thr.json")
        with open(p, "w") as f:
            json.dump({"min_autonomous_minutes_to_judge": 0, "single_tool_class_share_of_autonomous": 0.1}, f)
        return p


class TestWatchUnits(unittest.TestCase):
    def line(self, **kw):
        base = {"session": "s", "span_s": 3600, "autonomous_s": 3000, "idle_s": 0, "human_s": 600, "requests": 100, "cache_hit_rate": 0.95,
                "cache_misses": 0, "oversized": 0, "repeat_reads": 0, "retry_loops": 0, "cat_s": {}, "signals": {}, "top_saving": []}
        base.update(kw)
        return base

    def test_only_new_or_worse_warnings(self):
        th = dict(RS.thresholds)
        l1 = self.line(signals={"oom": 2})
        w1 = watchmod.warnings_for(l1, th, [])
        self.assertTrue(any("oom" in t for _, _, t in w1))
        hist = [{"session": "s", "warned": {k: m for k, m, _ in w1}}]
        self.assertEqual(watchmod.warnings_for(l1, th, hist), [])
        l2 = self.line(signals={"oom": 5})
        self.assertTrue(watchmod.warnings_for(l2, th, hist))

    def test_short_runs_not_judged(self):
        th = dict(RS.thresholds)
        self.assertEqual(watchmod.warnings_for(self.line(autonomous_s=60, signals={"oom": 9}), th, []), [])


class TestCompareUnits(unittest.TestCase):
    def test_delta_verdicts(self):
        d = cmpmod._delta(100, 50)
        self.assertEqual(d["verdict"], "improved")
        d = cmpmod._delta(0.9, 0.5, lower_is_better=False)
        self.assertEqual(d["verdict"], "regressed")


class CallsTest(unittest.TestCase):
    def test_scrub_masks_secrets(self):
        from rbn.calls import scrub
        for raw in ('curl -u me:hunter2 http://x', 'curl -d \'{"password":"hunter2"}\' http://x', 'API_TOKEN=hunter2 run', 'curl "http://x?key=hunter2"'):  # gitleaks:allow (fake test secret)
            self.assertNotIn("hunter2", scrub(raw), raw)

    def test_infer_command(self):
        from rbn.calls import infer_command, load_intents
        got = infer_command("cd app && npx next build && git push", "build", load_intents())
        self.assertEqual(got, "production build of a Next.js app + push to the remote")
        self.assertIn("wait", infer_command("sleep 60", "poll_wait", load_intents()))


if __name__ == "__main__":
    unittest.main()
