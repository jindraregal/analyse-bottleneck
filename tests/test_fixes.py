"""Regression tests for the audit, code review and UX fixes (2026-11-09). Synthetic fixtures only."""
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)

from fixtures import Tx, iso  # noqa: E402
from test_drill import RS, Base, notify, write_meta  # noqa: E402
from rbn import core, drill, view as viewmod, view_render as vr  # noqa: E402
from rbn.classify import classify_bash  # noqa: E402
from rbn.parse import parse_session  # noqa: E402
from rbn.timeline import sweep  # noqa: E402
from rbn.util import scrub  # noqa: E402
import analyze as cli  # noqa: E402

NOTE = "<task-notification><task-id>%s</task-id>%s<status>%s</status></task-notification>"


def note(task, tool=None, status="completed"):
    return NOTE % (task, "<tool-use-id>%s</tool-use-id>" % tool if tool else "", status)


class TestNotifications(Base):
    """Review bug 1: completion notices of background commands were mostly missed."""

    def bg(self, tx, t, cmd, task):
        return tx.tool(t, t + 1, "Bash", {"command": cmd, "run_in_background": True}, tur={"backgroundTaskId": task})

    def ends(self, tx, path):
        tx.write(path)
        S = parse_session(path, RS)
        return S, {tid: (t["bg_end"] - t["t0"]) if t["bg_end"] else None for tid, t in S["tools"].items()}

    def test_attachment_notice_in_subagent(self):
        sub = Tx("agent-a1", sidechain=True)
        tid = self.bg(sub, 10, "npx vitest run", "b1")
        sub.rows.append({"type": "attachment", "timestamp": iso(130), "sessionId": sub.sid,
                         "attachment": {"type": "queued_command", "commandMode": "task-notification", "prompt": note("b1", tid)}})
        sub.text(140)
        S, ends = self.ends(sub, self.sub("m", "a1"))
        self.assertAlmostEqual(ends[tid], 120, delta=0.01)

    def test_queue_prose_prefix_multi_block_task_id_and_earliest(self):
        tx = Tx("m2")
        tx.human(0)
        a = self.bg(tx, 10, "npx vitest run", "b1")
        b = self.bg(tx, 20, "npm run build", "b2")
        c = self.bg(tx, 30, "ffmpeg -i a.mp4 b.mp4", "b3")
        d = self.bg(tx, 40, "npx playwright test", "b4")
        tx.rows.append({"type": "queue-operation", "operation": "enqueue", "timestamp": iso(100), "sessionId": tx.sid,
                        "content": note("b1", a)})
        # prose before the tag; two blocks in one record; the second only names the task id
        tx.rows.append({"type": "user", "timestamp": iso(200), "sessionId": tx.sid, "isMeta": True,
                        "message": {"role": "user", "content": "x" * 490 + note("b2", b) + "\n" + note("b3")}})
        tx.rows.append({"type": "user", "timestamp": iso(300), "sessionId": tx.sid,
                        "message": {"role": "user", "content": note("b1", a)}})  # a later copy: the earliest wins
        tx.rows.append({"type": "user", "timestamp": iso(150), "sessionId": tx.sid,
                        "message": {"role": "user", "content": note("b4", d, status="running")}})  # not an end
        tx.text(400)
        S, ends = self.ends(tx, os.path.join(self.proj, "m2.jsonl"))
        self.assertAlmostEqual(ends[a], 90, delta=0.01)
        self.assertAlmostEqual(ends[b], 180, delta=0.01)
        self.assertAlmostEqual(ends[c], 170, delta=0.01)
        self.assertIsNone(ends[d])


class TestSweepTie(unittest.TestCase):
    def test_equal_rank_earliest_start_then_input_order(self):
        self.assertEqual(sweep([(0, 10, "b", 5, None), (5, 15, "a", 5, None)], 0, 15), [(0, 10, "b"), (10, 15, "a")])
        self.assertEqual(sweep([(0, 10, "zeta", 5, None), (0, 10, "alpha", 5, None)], 0, 10), [(0, 10, "zeta")])
        self.assertEqual(sweep([(0, 10, "x", 5, None), (2, 4, "y", 1, None)], 0, 10), [(0, 2, "x"), (2, 4, "y"), (4, 10, "x")])


class TestChildFill(Base):
    """Audit D3: child work after the parent stopped waiting was lost in the parent's gaps."""

    def test_child_work_after_parent_wait_counts(self):
        tx = Tx("fill")
        tx.human(0)
        tx.tool(10, 20, "Agent", {"description": "long check", "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "c1"})
        tx.text(22)
        tx.human(900)
        tx.text(905)
        tx.write(os.path.join(self.proj, "fill.jsonl"))
        sub = Tx("agent-c1", sidechain=True)
        sub.tool(25, 300, "Bash", {"command": "uv run pytest -x tests/"})
        sub.text(305)
        write_meta(sub.write(self.sub("fill", "c1")), agentType="general-purpose", description="long check")
        an, d = self.leaves()
        tests = [lf for lf in d["leaves"] if lf["cat"] == "test"]
        self.assertAlmostEqual(sum(lf["seconds"] for lf in tests), 275, delta=2)
        self.assertEqual({lf["via"] for lf in tests}, {"subagent: long check (general-purpose)"})
        self.assertAlmostEqual(sum(lf["seconds"] for lf in d["leaves"]), an[0]["tl"]["autonomous"], delta=0.01)


class TestResumePool(Base):
    """Review bug 2: a resumed session with more subagents took the copied records and lost the original's."""

    def test_original_subagent_parsed_by_the_group_that_owns_the_call(self):
        tx = Tx("orig")
        tx.human(0)
        tx.tool(10, 310, "Agent", {"description": "slow job", "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "s1"})
        tx.text(320)
        for i, r in enumerate(tx.rows):
            r.setdefault("uuid", "orig-%d" % i)
        tx.write(os.path.join(self.proj, "orig.jsonl"))
        sub = Tx("agent-s1", sidechain=True)
        sub.tool(12, 300, "Bash", {"command": "npx vitest run"})
        sub.text(305)
        write_meta(sub.write(self.sub("orig", "s1")), agentType="general-purpose", description="slow job")
        res = Tx("res")
        res.rows = [dict(r, sessionId="res") for r in tx.rows]
        res.n = 1000
        res.human(400)
        for k, aid in enumerate(("r1", "r2")):
            t0 = 410 + k * 100
            res.tool(t0, t0 + 60, "Agent", {"description": "build %d" % k, "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": aid})
            s = Tx("agent-" + aid, sidechain=True)
            s.tool(t0 + 2, t0 + 55, "Bash", {"command": "npm run build"})
            s.text(t0 + 57)
            write_meta(s.write(self.sub("res", aid)), agentType="general-purpose", description="build %d" % k)
        res.text(620)
        for i, r in enumerate(res.rows):
            r.setdefault("uuid", "res-%d" % i)
        res.write(os.path.join(self.proj, "res.jsonl"))
        groups = core.find_groups(project="proj-drill", root=self.tmp)
        self.assertEqual(groups[core.claim_order(groups)[0]]["main"].endswith("res.jsonl"), True)
        an = core.analyze_groups(groups, RS, (None, None), {"idle_threshold": 180.0})
        d = drill.drill_analyses(an, RS)
        by = self.by(d["leaves"], "cat")
        self.assertAlmostEqual(by.get("test", 0), 288, delta=2)
        self.assertLess(by.get(drill.UNKNOWN, 0), 20)
        self.assertEqual(sum(len(a["subs"]) for a in an), 3)  # each subagent transcript in one group only


class TestPollWaits(Base):
    """Audit D5, known issue 4 and UX 1: a poll loop is named by its loop, PIDs masked, and says what it waited for."""

    def test_poll_named_before_deploy_and_pid_masked(self):
        sig = drill.signature("while pgrep -f my-deploy >/dev/null; do sleep 15; done; my-deploy --wait", RS, "poll_wait")
        self.assertEqual(sig[0], "while pgrep -f my-deploy")
        self.assertEqual(drill.signature("while kill -0 31337 2>/dev/null; do sleep 30; done", RS, "poll_wait")[0], "while kill -0 <pid>")
        self.assertEqual(drill.signature("my-deploy --wait && curl -s https://x.example.com/health", RS, "network")[0], "my-deploy")
        # a for-loop that breaks on a condition is the same wait; 'break' never names an item
        sig = drill.signature("for i in $(seq 1 30); do grep -q done /tmp/x.log && break; sleep 5; done; tail /tmp/x.log", RS, "poll_wait")
        self.assertEqual(sig[0], "until grep -q done /tmp/x.log")
        self.assertNotEqual(drill.signature("ls /tmp/x && break", RS)[0], "break")

    def test_waited_for_by_file_and_by_pid(self):
        tx = Tx("wf")
        tx.human(0)
        b = tx.tool(10, 11, "Bash", {"command": "npx vitest run > /tmp/vt/out 2>&1; touch /tmp/vt/DONE", "run_in_background": True},
                    tur={"backgroundTaskId": "b1"})
        notify(tx, 15, b)
        tx.tool(20, 320, "Bash", {"command": "until [ -f /tmp/vt/DONE ]; do sleep 5; done; tail -3 /tmp/vt/out"})
        tx.tool(330, 332, "Bash", {"command": "./scripts/launch.sh real2"}, result="launched PID 4242")
        tx.tool(340, 940, "Bash", {"command": "while kill -0 4242 2>/dev/null; do sleep 30; done; echo done"})
        s1 = tx.tool(950, 951, "Bash", {"command": "python3 scripts/sweep.py --all > /tmp/sw.log 2>&1", "run_in_background": True},
                     tur={"backgroundTaskId": "b2"})
        notify(tx, 952, s1)
        tx.tool(960, 1260, "Bash", {"command": "until grep -q '^done' /tmp/sw.log; do sleep 10; done"})
        s2 = tx.tool(1270, 1271, "Bash", {"command": "npm run build", "run_in_background": True}, tur={"backgroundTaskId": "bz9"})
        notify(tx, 1272, s2)
        tx.tool(1280, 1580, "Bash", {"command": "for i in $(seq 1 60); do grep -q built /tmp/c/p/s/tasks/bz9.output && break; sleep 5; done"})
        tx.tool(1590, 1600, "Bash", {"command": "(npx playwright test e2e > /tmp/g/e1.log 2>&1; echo \"exit $?\" >> /tmp/g/e1.log) & sleep 5; tail /tmp/g/e1.log"})
        tx.tool(1610, 1910, "Bash", {"command": "until grep -q '^exit ' /tmp/g/e1.log; do sleep 10; done; tail -3 /tmp/g/e1.log"})
        tx.text(1915)
        tx.write(os.path.join(self.proj, "wf.jsonl"))
        an, rep, (v, _) = self.view()
        poll = next(s for s in v["segments"] if s["id"] == "poll")
        wf = {it["what"]: it.get("waited_for") for it in poll["items"]}
        self.assertEqual(wf.get("until [ -f /tmp/vt/DONE ]"), "npx vitest run")
        self.assertEqual(wf.get("while kill -0 <pid>"), "./scripts/launch.sh")  # a script's id-like argument does not split it
        self.assertEqual(wf.get("until grep -q '^done' /tmp/sw.log"), "python3 scripts/sweep.py")
        self.assertEqual(wf.get("until grep -q built /tmp/c/p/s/tasks/bz9.output"), "npm run build")
        # the writer is itself a poll ('(npx playwright test > log) & sleep 5'): both wait for the same run, one item
        self.assertEqual(wf.get("until grep -q '^exit ' /tmp/g/e1.log (+1 other poll)"), "npx playwright test")
        html = vr.render_html(v)
        self.assertIn("<dt>Waits for</dt>", html)


class TestCounts(Base):
    """Audit D6: count and median are over every run in the window, not only runs that won seconds."""

    def test_overlapped_run_still_counts(self):
        tx = Tx("cnt")
        tx.human(0)
        tx.tool(10, 300, "Bash", {"command": "npm run build"})
        b = tx.tool(20, 21, "Bash", {"command": "npx vitest run", "run_in_background": True}, tur={"backgroundTaskId": "b1"})
        notify(tx, 100, b)  # entirely under the build: 0 s of its own
        tx.tool(310, 340, "Bash", {"command": "npx vitest run"})
        tx.text(345)
        tx.write(os.path.join(self.proj, "cnt.jsonl"))
        an, rep, (v, _) = self.view()
        it = next(it for s in v["segments"] for it in s["items"] if it["what"] == "npx vitest run")
        self.assertEqual(it["count"], 2)
        self.assertAlmostEqual(it["seconds"], 30, delta=1)

    def test_capped_background_runs(self):
        self.assertFalse(drill.capped({"bg": True, "bg_end": None, "name": "Bash", "cls": "service"}))
        self.assertTrue(drill.capped({"bg": True, "bg_end": None, "name": "Bash", "cls": "test"}))
        self.assertFalse(drill.capped({"bg": True, "bg_end": 5.0, "name": "Bash", "cls": "test"}))
        srv = {"bg": True, "bg_end": None, "name": "Bash", "cls": "service", "dur": 2.0, "t0": 0.0, "t1": 2.0}
        self.assertEqual(drill._eff_dur(srv, {"last": 700.0}), 2.0)  # review 4: a dev server's median is not 690 s
        tx = Tx("cap")
        tx.human(0)
        tx.tool(10, 11, "Bash", {"command": "npx vitest run", "run_in_background": True}, tur={"backgroundTaskId": "b1"})
        tx.text(15)
        tx.human(400)
        tx.text(405)
        tx.write(os.path.join(self.proj, "cap.jsonl"))
        an, rep, (v, check) = self.view()
        self.assertGreater(check["capped_s"], 60)
        row = next(b for b in v["basis"] if b["what"] == "Background runs without an end")
        self.assertEqual(row["kind"], "missing")
        it = next(it for s in v["segments"] for it in s["items"] if it["what"] == "npx vitest run")
        self.assertIn("est_s", it)
        self.assertIn("≤", vr._item_html(it))


class TestClassifyAndText(unittest.TestCase):
    def test_chain_that_runs_a_script_is_a_script(self):
        self.assertEqual(classify_bash("rm -f /tmp/x/more.done; bash /tmp/x/more.sh", RS), "script_run")
        self.assertEqual(classify_bash("cd app && ./run.sh --fast", RS), "script_run")
        self.assertEqual(classify_bash("rm -rf dist", RS), "file_ops")
        self.assertEqual(classify_bash("which python3", RS), "file_ops")

    def test_scrub_keeps_token_counts(self):
        self.assertEqual(scrub("tokens: 12345678 units x 0.5"), "tokens: 12345678 units x 0.5")
        self.assertEqual(scrub("max_tokens=500"), "max_tokens=500")
        self.assertNotIn("abc123", scrub("GITHUB_TOKEN=ghp_abc123def456"))  # gitleaks:allow (fake test secret)
        self.assertNotIn("hunter2", scrub("api_key: hunter2"))

    def test_imperative_cuts_at_clause_not_colon(self):
        c = {"fix": 'Enable "incremental": true in tsconfig (cache in .tsbuildinfo), or use tsc -b.'}
        self.assertEqual(viewmod._imperative(c), 'Enable "incremental": true in tsconfig')
        c = {"fix": "Run changed/related tests while iterating (--changed, --related), full suite before commit."}
        self.assertEqual(viewmod._imperative(c), "Run changed/related tests while iterating")

    def test_stack_example_is_the_matched_part(self):
        from rbn.stack import _example_part
        from rbn.util import match_text
        rule = {"_rx": re.compile(r"\bvitest\b", re.I)}
        cmd = "pid=$(ss -ltnp | grep 3000); nice -n 5 npx vitest run src/a.test.ts --reporter=dot"
        self.assertEqual(_example_part(rule, cmd, match_text(cmd), RS), "npx vitest run src/a.test.ts --reporter=dot")

    def test_project_argument_may_start_with_a_dash(self):
        td = tempfile.mkdtemp()
        os.environ["RB_PROJECTS_DIR"] = td
        self.addCleanup(os.environ.pop, "RB_PROJECTS_DIR", None)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            try:
                cli.main(["view", "--project", "-home-me-git-app", "--out", td])
            except SystemExit as e:
                self.assertNotEqual(e.code, 2, err.getvalue())
        self.assertNotIn("expected one argument", err.getvalue())


class TestActions(unittest.TestCase):
    def info(self, sid, sec, items, pct=10.0):
        return {"id": sid, "seconds": sec, "pct": pct, "items": items, "all_items": items}

    def test_deploy_segment_rule(self):
        rule = next(r for r in RS.segments["segment_rules"] if r["id"] == "deploy-batch")
        items = [{"what": "my-deploy", "count": 25, "p50_s": 960.0, "seconds": 11000.0}, {"what": "gcloud run deploy", "count": 2, "p50_s": 300.0, "seconds": 880.0}]
        a = viewmod.segment_rule_action(rule, self.info("deploys", 11880.0, items, pct=9.0))
        self.assertEqual(a["saving_min"], 99.0)
        self.assertIn("27 deploys", a["evidence"])
        self.assertIn("16 min", a["evidence"])
        self.assertIn("assumption:", a["arithmetic"])
        self.assertIsNone(viewmod.segment_rule_action(rule, self.info("deploys", 11880.0, items, pct=4.0)))
        few = [dict(items[0], count=2)]
        self.assertIsNone(viewmod.segment_rule_action(rule, self.info("deploys", 11880.0, few)))

    def test_saving_capped_at_slice(self):
        a = viewmod._action({"id": "x", "fix": "Do it.", "saving_minutes": 80.0, "confidence": "high"}, 57.0)
        self.assertEqual(a["saving_min"], 57.0)
        self.assertIn("capped at this slice's 57 min", a["arithmetic"])

    def test_off_slice_actions(self):
        loops = self.info("poll", 100.0, [{"what": "while kill -0 <pid>", "seconds": 80.0}])
        self.assertTrue(viewmod._off_slice({"id": "polling"}, {}, RS, loops))
        gh = self.info("poll", 100.0, [{"what": "gh run view", "seconds": 80.0}])
        self.assertFalse(viewmod._off_slice({"id": "polling"}, {}, RS, gh))
        self.assertTrue(viewmod._off_slice({"id": "env_x", "fix": "See the signal hint."}, {}, RS, gh))
        rep = {"stack_findings": [{"id": "r1", "examples": [{"raw": "pid=$(ss -ltnp | grep 3000)"}]}]}
        tests = self.info("tests", 100.0, [{"what": "npx vitest run", "seconds": 90.0}])
        self.assertTrue(viewmod._off_slice({"id": "r1", "kind": "stack_rule"}, rep, RS, tests))

    def test_actions_sorted_dropped_and_do_first(self):
        segs = [{"id": "tests", "label": "Tests", "actions": [{"id": "a", "title": "A", "saving_min": 30.0, "confidence": "low"},
                                                             {"id": "b", "title": "B", "saving_min": 12.0, "confidence": "medium"}]},
                {"id": "poll", "label": "Poll", "actions": [{"id": "c", "title": "C", "saving_min": 5.0, "confidence": "high"}]}]
        self.assertEqual(viewmod.do_first(segs)["id"], "b")  # medium/high confidence first, at least 10 min
        segs[0]["actions"] = segs[0]["actions"][:1]
        self.assertEqual(viewmod.do_first(segs)["id"], "a")
        segs[0]["actions"][0]["saving_min"] = 9.0
        self.assertIsNone(viewmod.do_first(segs))
        report = {"ranked": [{"id": "polling", "kind": "signal", "fix": "Block in one call.", "saving_minutes": 0.4, "confidence": "high"},
                             {"id": "polling2", "kind": "signal", "fix": "Poll less.", "saving_minutes": 20.0, "confidence": "low"},
                             {"id": "polling3", "kind": "signal", "fix": "Batch polls.", "saving_minutes": 40.0, "confidence": "low"}]}
        cfg = {"finding_segments": {"polling": "poll", "polling2": "poll", "polling3": "poll"}}
        info = {"poll": self.info("poll", 1800.0, [{"what": "gh run view", "seconds": 1700.0}])}
        out = viewmod.build_actions(report, RS, cfg, {}, {"poll"}, info)
        self.assertEqual([a["id"] for a in out["poll"]], ["polling3", "polling2"])  # by saving; < 1 min dropped
        self.assertEqual(out["poll"][0]["saving_min"], 30.0)  # capped at the slice's 30 min


class TestRender(unittest.TestCase):
    def test_merge_notes_drops_the_no_notes_row(self):
        v = {"segments": [{"id": "tests", "label": "Tests", "seconds": 60, "items": [], "actions": [], "notes": []}],
             "basis": [{"what": "Tool time", "how": "x", "kind": "deterministic", "detail": ""},
                       {"what": "AI notes", "how": "none on this page", "kind": "none", "detail": ""}]}
        m = vr.merge_notes(v, {"model": "m", "segments": {"tests": [{"title": "Shard", "detail": "d", "evidence": []}]}})
        ai = [b for b in m["basis"] if "AI" in b["what"]]
        self.assertEqual([b["kind"] for b in ai], ["llm"])
        self.assertEqual(viewmod.merge_notes(v, {"model": "m", "segments": {}})["basis"][-1]["kind"], "llm")

    def test_times_say_utc(self):
        self.assertTrue(vr.fmt_window("2026-11-09T10:00:00Z", "2026-11-09T19:22:00Z").endswith("UTC"))
        page = vr.render_html({"generated": "2026-11-09T19:22:39Z", "segments": [{"id": "tests", "label": "Tests", "seconds": 60}],
                               "totals": {"agent_s": 60}})
        self.assertIn("19:22 UTC", page)
        b = viewmod.render_brief({"title": "t", "window": {"since": "2026-11-02T19:22:39Z", "until": "2026-11-09T19:22:39Z", "sessions": 1, "subagent_sessions": 0},
                                  "segments": [],
                                  "totals": {"agent_s": 0, "span_s": 0, "excluded": []}, "headline": "x", "basis": []})
        self.assertIn("UTC", b)

    def test_fixed_colour_per_slice_id(self):
        a = vr.colour_classes([{"id": "poll", "seconds": 9}, {"id": "tests", "seconds": 5}])
        b = vr.colour_classes([{"id": "tests", "seconds": 9}, {"id": "poll", "seconds": 5}])
        self.assertEqual((a[0], a[1]), (b[1], b[0]))
        c = vr.colour_classes([{"id": "scripts", "seconds": 9}, {"id": "files", "seconds": 5}])
        self.assertNotEqual(c[0], c[1])  # a taken slot goes to the next free one

    def test_items_median_and_noise(self):
        self.assertFalse(vr.show_median({"count": 4, "p50_s": 960, "seconds": 1260}))
        self.assertTrue(vr.show_median({"count": 4, "p50_s": 300, "seconds": 1260}))
        items = [{"what": "<synthetic>", "seconds": 500}, {"what": "a", "seconds": 400}, {"what": "b", "seconds": 59}]
        self.assertEqual([it["what"] for it in viewmod.top_items(items, 1000)], ["a"])
        self.assertEqual(vr.short_via("subagent: Shard e2e suite (general-purpose) · main session"), "by Shard e2e suite +1")
        self.assertEqual(vr.short_via("main session", subagents=False), "")


if __name__ == "__main__":
    unittest.main()
