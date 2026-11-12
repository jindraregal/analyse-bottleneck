"""Regressions from the third audit of the view: fork copies, long commands, wrappers, headline, session counts,
child work after the parent's last record. Synthetic data only."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

from fixtures import Tx  # noqa: E402
from test_drill import RS, Base, write_meta  # noqa: E402
from rbn import drill, view as viewmod  # noqa: E402
from rbn.drill import heavy_key, run_key, signature  # noqa: E402
from rbn.parse import CMD_KEEP, _keep_cmd  # noqa: E402

VITEST = run_key("test", "bash|npx vitest run")


class TestForkedSubagents(Base):
    """A: a fork subagent copies its parent agent's records (same uuids); its runs and requests were counted again."""

    def make(self):
        tx = Tx("m")
        tx.human(0)
        tx.tool(10, 420, "Agent", {"description": "collect", "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "s1"})
        tx.text(425)
        tx.write(os.path.join(self.proj, "m.jsonl"))
        s1 = Tx("agent-s1", sidechain=True)
        s1.human(11, "collect")
        s1.tool(30, 100, "Bash", {"command": "npx vitest run src/a.test.ts"})
        s1.text(105)
        for i, r in enumerate(s1.rows):
            r.setdefault("uuid", "s1-%d" % i)  # real transcripts give every record a uuid
        write_meta(s1.write(self.sub("m", "s1")), agentType="general-purpose", description="collect", spawnDepth=1)
        for k, aid in enumerate(("a0f1", "a0f2")):  # sort before 's1': the original must still claim the copies
            f = Tx("agent-" + aid, sidechain=True)
            f.rows = [dict(r) for r in s1.rows]
            # the parent's spawning message, re-recorded by the fork under a new uuid (same request id)
            f.rows.append(dict(s1.rows[-1], uuid="fork-%d-spawn" % k))
            f.n = 1000
            f.human(110 + k, "fork %d" % k)
            f.tool(115 + k * 100, 190 + k * 100, "Bash", {"command": "npm run build"})
            f.text(195 + k * 100)
            write_meta(f.write(self.sub("m", aid)), agentType="fork", isFork=True, parentAgentId="s1", spawnDepth=2,
                       description="part %d" % k)

    def test_copied_runs_and_requests_counted_once(self):
        self.make()
        an, d = self.leaves()
        self.assertEqual(len(d["runs"][VITEST]), 1)  # was 3: the original plus two fork copies
        reqs = sum(len(v) for k, v in d["runs"].items() if k.startswith("inference\t"))
        own = sum(len(S["requests"]) for S in an[0]["sessions"])
        self.assertEqual(reqs, own)
        subs = {S["session_id"]: S for S in an[0]["subs"]}
        self.assertEqual(len(subs["agent-s1"]["requests"]), 2)  # the original keeps its records
        self.assertGreater(subs["agent-a0f1"]["dup_records"], 0)
        self.assertGreaterEqual(subs["agent-a0f1"]["first"], subs["agent-s1"]["last"] - 1e-6)  # the fork starts at its own work
        tests = sum(lf["seconds"] for lf in d["leaves"] if lf["cat"] == "test")
        self.assertAlmostEqual(tests, 70, delta=1)  # time was right before and stays right

    def test_basis_counts_fork_copies(self):
        self.make()
        _, _, (v, _) = self.view()
        row = next(b for b in v["basis"] if b["what"] == "Forked sessions")
        self.assertIn("forked subagents", row["detail"])


class TestLongCommand(unittest.TestCase):
    """B: a long command lost the part that did the work at the 700-char cut."""

    def test_long_quoted_data_is_shortened_first(self):
        cmd = ("cd /repo/app; sed -i 's/%s/x/g' src/a.test.ts; echo '%s' > src/b.test.ts; "
               "timeout 300 run-heavy --need 6G -- npx vitest run src/a.test.ts 2>&1 | grep -E \"passed|failed\"; cat /tmp/out.txt"
               % ("a" * 300, "import { x } from 'vitest'; " * 15))
        self.assertGreater(len(cmd), CMD_KEEP)
        kept = _keep_cmd(cmd)
        self.assertLessEqual(len(kept), CMD_KEEP)
        self.assertIn("npx vitest run src/a.test.ts", kept)
        self.assertEqual(heavy_key(kept, RS), "npx vitest run")
        self.assertEqual(signature(kept, RS, "test")[0], "npx vitest run")
        self.assertEqual(signature(cmd[:CMD_KEEP], RS, "test")[0], "sed")  # what the plain cut kept

    def test_many_parts_lose_the_middle(self):
        cmd = " && ".join(["sed -i s/k%d/v%d/ src/file%d.ts" % (i, i, i) for i in range(40)]) + " && npx playwright test e2e/a.spec.ts"
        kept = _keep_cmd(cmd)
        self.assertLessEqual(len(kept), CMD_KEEP)
        self.assertTrue(kept.startswith("sed -i s/k0/v0/"))
        self.assertTrue(kept.endswith("npx playwright test e2e/a.spec.ts"))
        self.assertIn(" …", kept)
        self.assertEqual(heavy_key(kept, RS), "npx playwright test")

    def test_short_and_heredoc_commands_unchanged(self):
        self.assertEqual(_keep_cmd("npx vitest run"), "npx vitest run")
        self.assertIsNone(_keep_cmd(""))
        body = "print(1)\n" * 120
        kept = _keep_cmd("python3 - <<'EOF'\n%sEOF\nnpx playwright test" % body)
        self.assertIn("npx playwright test", kept)


class TestWrappers(unittest.TestCase):
    """C: '<x>-heavy npx vitest' without '--' and node_modules/.bin shims were keyed apart."""

    def test_heavy_wrapper_without_separator(self):
        for cmd, key in (("~/bin/run-heavy npx vitest run src/x.test.ts", "npx vitest run"),
                         ("run-heavy --need 6G npx playwright test e2e/a", "npx playwright test"),
                         ("run-heavy npx tsc --noEmit", "npx tsc"),
                         ("cd app && ~/bin/run-heavy --quiet npx vitest run", "npx vitest run"),
                         ("run-heavy --name t1 bash /tmp/x/run.sh", "bash /tmp/x/run.sh"),
                         ("run-heavy --list 2>&1 | head", "run-heavy")):
            self.assertEqual(signature(cmd, RS)[0], key, cmd)

    def test_bin_shims_are_the_tool(self):
        self.assertEqual(signature("./node_modules/.bin/vitest run src/a.test.ts", RS)[0], "npx vitest run")
        self.assertEqual(signature("node_modules/.bin/vitest run", RS)[0], "npx vitest run")
        self.assertEqual(signature("/repo/app/node_modules/.bin/tsc --noEmit", RS)[0], "npx tsc")


class TestHeadline(unittest.TestCase):
    """E: the headline named one subagent for a command it ran only 12 % of."""

    def seg(self, share, n_via, item_s=500.0):
        it = {"label": "npx vitest run", "what": "npx vitest run", "seconds": item_s, "count": 40, "p50_s": 15.0,
              "via": "subagent: Gate (general-purpose) · subagent: Other (general-purpose)", "_via_share": share, "_n_via": n_via}
        return [{"id": "tests", "label": "Tests", "seconds": 1000.0, "items": [it]}]

    def test_spread_over_tasks(self):
        h = viewmod._headline(3600, self.seg(0.12, 9))
        self.assertIn("mostly 'npx vitest run' across 9 tasks", h)
        self.assertNotIn("Gate", h)

    def test_one_task_ran_most(self):
        self.assertIn("run by subagent 'Gate'", viewmod._headline(3600, self.seg(0.8, 3)))

    def test_main_session_reads_naturally(self):
        s = self.seg(0.9, 1)
        s[0]["items"][0]["via"] = "main session"
        h = viewmod._headline(3600, s)
        self.assertIn("'npx vitest run' in the main session.", h)
        self.assertNotIn("run by main session", h)

    def test_mostly_only_for_half(self):
        h = viewmod._headline(3600, self.seg(0.8, 3, item_s=300.0))
        self.assertIn("biggest item 'npx vitest run'", h)
        self.assertNotIn("mostly", h)

    def test_summary_follows_the_same_rule(self):
        s = self.seg(0.12, 9)
        self.assertIn("across 9 tasks", viewmod._summary(1000.0, s[0]["items"], 1000.0))

    def test_row_counts_every_task_not_the_two_named(self):
        # the page said 'by Pre-deploy gate +1' for a command 53 tasks ran
        from rbn import view_render as vr
        via = "subagent: Gate (general-purpose) · subagent: Other (general-purpose)"
        self.assertEqual(vr.short_via(via, n_tasks=53), "by Gate +52")
        self.assertEqual(vr.short_via(via), "by Gate +1")
        self.assertEqual(vr.short_via("main session", n_tasks=1), "main session")

    def test_sub_second_median_is_not_zero(self):
        from rbn import view_render as vr
        self.assertEqual((vr.fmt_h(0.1), vr.fmt_h(0), vr.fmt_h(0.6)), ("<1 s", "0 s", "1 s"))

    def test_folded_item_names_who_ran_the_folded_slice(self):
        lv = [{"cat": "install", "key": "bash|npm ci", "what": "npm ci", "label": "npm ci", "inst": ("a", "t%d" % k), "dur": 10.0,
               "via": "subagent: T%d (general-purpose)" % k, "group": "g", "seconds": 10.0, "seg0": "installs"} for k in range(4)]
        lv.append({"cat": "install", "key": "bash|pip install x", "what": "pip install x", "label": "pip install x", "inst": ("a", "p"),
                   "dur": 25.0, "via": "subagent: P (general-purpose)", "group": "g", "seconds": 25.0, "seg0": "installs"})
        it = viewmod.build_other_items(lv, {"installs": "Installs"}, {})[0]
        self.assertTrue(it["label"].startswith("Installs, mostly"))
        self.assertEqual(it["_n_via"], 5)
        self.assertAlmostEqual(it["_via_share"], 25.0 / 65.0, places=3)
        self.assertTrue(it["via"].startswith("subagent: P "))


class TestSessionCounts(Base):
    """F: sessions counted only groups whose runs won time; one task's scratch script merged with another session's."""

    def test_sessions_cover_every_run(self):
        lv = [{"cat": "poll_wait", "key": "poll|deploy", "what": "while pgrep -f deploy", "label": "wait", "inst": ("a", "t1"),
               "dur": 100.0, "via": "main session", "group": "g1", "seconds": 100.0}]
        runs = {run_key("poll_wait", "poll|deploy"): [100.0, 50.0, 70.0]}
        groups = {run_key("poll_wait", "poll|deploy"): {"g1", "g2", "g3"}}
        it = viewmod.build_items(lv, runs, {}, rgroups=groups)[0]
        self.assertEqual((it["count"], it["sessions"]), (3, 3))
        self.assertEqual(viewmod.build_items(lv, runs, {})[0]["sessions"], 1)

    def test_run_groups_from_drill(self):
        for sid in ("ga", "gb"):
            tx = Tx(sid)
            tx.human(0)
            tx.tool(10, 100, "Bash", {"command": "npx vitest run"})
            tx.text(105)
            tx.write(os.path.join(self.proj, "%s.jsonl" % sid))
        _, d = self.leaves()
        self.assertEqual(d["run_groups"][VITEST], {"ga", "gb"})

    def test_scratch_scripts_of_two_sessions_are_two_items(self):
        for k, sid in enumerate(("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")):
            tx = Tx(sid)
            tx.human(0)
            tx.tool(10, 100 + 400 * k, "Bash", {"command": "bash /tmp/claude-1001/-repo/%s/scratchpad/build.sh v%d" % (sid, k)})
            tx.text(505)
            tx.write(os.path.join(self.proj, "%s.jsonl" % sid))
        _, d = self.leaves()
        builds = [lf for lf in d["leaves"] if lf["what"] == "bash …/scratchpad/build.sh"]
        self.assertEqual(len({lf["key"] for lf in builds}), 2)
        items = [it for it in viewmod.build_items(builds, d["runs"], d["runs_by_via"], rgroups=d["run_groups"])]
        self.assertEqual(sorted((it["count"], it["sessions"]) for it in items), [(1, 1), (1, 1)])


class TestChildAfterParent(Base):
    """G: a subagent still working after the main session's last record lost that time."""

    def test_child_work_after_last_parent_record_counts(self):
        tx = Tx("late")
        tx.human(0)
        tx.tool(10, 12, "Agent", {"description": "long gate", "prompt": "x", "subagent_type": "general-purpose"},
                tur={"agentId": "c1", "status": "async_launched"})
        tx.text(15)
        tx.write(os.path.join(self.proj, "late.jsonl"))
        sub = Tx("agent-c1", sidechain=True)
        sub.human(13, "gate")
        sub.tool(20, 600, "Bash", {"command": "npx playwright test"})
        sub.text(605)
        write_meta(sub.write(self.sub("late", "c1")), agentType="general-purpose", description="long gate")
        an, d = self.leaves()
        tests = sum(lf["seconds"] for lf in d["leaves"] if lf["cat"] == "test")
        self.assertAlmostEqual(tests, 580, delta=2)  # the main session's last record is at 15 s
        self.assertAlmostEqual(an[0]["tl"]["hi"] - an[0]["tl"]["lo"], 605, delta=1)
        self.assertAlmostEqual(sum(lf["seconds"] for lf in d["leaves"]), an[0]["tl"]["autonomous"], delta=0.01)

    def test_window_still_clips(self):
        from fixtures import T0
        tx = Tx("late2")
        tx.human(0)
        tx.tool(10, 12, "Agent", {"description": "g", "prompt": "x", "subagent_type": "general-purpose"},
                tur={"agentId": "c2", "status": "async_launched"})
        tx.text(15)
        tx.write(os.path.join(self.proj, "late2.jsonl"))
        sub = Tx("agent-c2", sidechain=True)
        sub.human(13, "g")
        sub.tool(20, 250, "Bash", {"command": "npx playwright test"})
        sub.tool(260, 600, "Bash", {"command": "npx playwright test e2e/b"})  # ends after the window: not counted
        sub.text(605)
        write_meta(sub.write(self.sub("late2", "c2")), agentType="general-purpose", description="g")
        from rbn import core
        until = T0.timestamp() + 300
        an = core.analyze_groups(core.find_groups(project="proj-drill", root=self.tmp), RS, (None, until), {"idle_threshold": 180.0})
        self.assertLessEqual(an[0]["tl"]["hi"], until + 1e-6)
        d = drill.drill_analyses(an, RS, (None, until))
        self.assertAlmostEqual(sum(lf["seconds"] for lf in d["leaves"] if lf["cat"] == "test"), 230, delta=2)


if __name__ == "__main__":
    unittest.main()
