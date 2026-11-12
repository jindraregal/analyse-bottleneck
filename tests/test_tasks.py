"""'By task' breakdown, one-off script groups, poll grouping and consistent rule evidence counts. Synthetic data only."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

from fixtures import Tx  # noqa: E402
from test_drill import RS, Base, notify, write_meta  # noqa: E402
from rbn import core, drill, view as viewmod, view_render as vr  # noqa: E402

MAIN = "main session"


def leaf(key, sec, via=MAIN, cat="script", scratch=False, what=None, inst=None):
    lf = {"cat": cat, "key": key, "what": what or key, "label": what or key, "inst": inst or "%s|%s" % (via, key), "dur": sec,
          "via": via, "group": "g1", "seconds": float(sec)}
    if scratch:
        lf["scratch"] = True
    return lf


def sub(desc, typ="general-purpose"):
    return "subagent: %s (%s)" % (desc, typ)


class TestTaskLabel(unittest.TestCase):
    def test_labels_are_normalised(self):
        self.assertEqual(viewmod.task_label(sub("Batch runner (T-105..T-115)")), "Batch runner (T-105..T-115)")
        self.assertEqual(viewmod.task_label("subagent: Fix   login\nbug (Explore)"), "Fix login bug")
        self.assertEqual(viewmod.task_label("workflow agent: nightly research (wf_1)"), "nightly research (wf_1)")
        self.assertEqual(viewmod.task_label("subagent ab12 (spawner unknown)"), "subagent ab12")
        self.assertEqual(viewmod.task_label(MAIN), MAIN)
        self.assertEqual(viewmod.task_label(""), "unknown")
        self.assertLessEqual(len(viewmod.task_label(sub("x" * 200))), 61)


class TestBuildTasks(unittest.TestCase):
    def test_top_four_by_time_the_items_do_not_cover(self):
        lv = [leaf("big.sh", 5000, sub("A")),                     # one listed item: A adds nothing
              leaf("x1.sh", 900, sub("B")), leaf("x2.sh", 800, sub("B")),
              leaf("y.sh", 1200, sub("C")), leaf("z.sh", 1000, sub("D")), leaf("w.sh", 600, sub("E")),
              leaf("v.sh", 300, sub("F")), leaf("u.sh", 100, sub("G"))]
        top, rest, n, mx = viewmod.build_tasks(lv, {"big.sh"})
        # B..E are picked by the time big.sh does not cover; A (5000 s, all listed) is bigger than E, so it is shown
        # too: no hidden task may be bigger than a shown one ('each <= X' and 'biggest first' stay true)
        self.assertEqual([t["label"] for t in top], ["A", "B", "C", "D", "E"])
        self.assertEqual(top[1], {"label": "B", "seconds": 1700.0, "items": 2})
        self.assertEqual((rest, n, mx), (400.0, 2, 300.0))
        self.assertLessEqual(mx, min(t["seconds"] for t in top))
        top, rest, n, mx = viewmod.build_tasks(lv)  # nothing listed: by whole time
        self.assertEqual([t["label"] for t in top], ["A", "B", "C", "D"])
        self.assertAlmostEqual(sum(t["seconds"] for t in top) + rest, sum(lf["seconds"] for lf in lv))

    def test_many_bigger_covered_tasks_fall_back_to_biggest_first(self):
        # five tasks fully explained by listed items are each bigger than every uncovered task: showing them all
        # would be long, so the list is simply the biggest four
        lv = [leaf("big%d.sh" % i, 3000 - i, sub("L%d" % i)) for i in range(5)]
        lv += [leaf("s%d.sh" % i, 500 - i, sub("S%d" % i)) for i in range(4)]
        top, rest, n, mx = viewmod.build_tasks(lv, {"big%d.sh" % i for i in range(5)})
        self.assertEqual([t["label"] for t in top], ["L0", "L1", "L2", "L3"])
        self.assertEqual(mx, 2996.0)
        self.assertLessEqual(mx, min(t["seconds"] for t in top))

    def test_same_task_from_two_spawn_types_is_one_task(self):
        lv = [leaf("a.sh", 100, sub("Fix bug", "general-purpose")), leaf("b.sh", 50, sub("Fix bug", "Explore"))]
        top, rest, n, _ = viewmod.build_tasks(lv)
        self.assertEqual(top, [{"label": "Fix bug", "seconds": 150.0, "items": 2}])
        self.assertEqual((rest, n), (0.0, 0))

    def test_coverage_items_tasks_union(self):
        lv = [leaf("big.sh", 600), leaf("s1.sh", 300, sub("B")), leaf("s2.sh", 100, sub("C"))]
        items = [{"_key": "big.sh", "_spec": True}]
        tasks = [{"label": "B"}]
        self.assertEqual(tuple(round(x, 2) for x in viewmod.coverage(lv, items, tasks)), (0.6, 0.3, 0.9))
        self.assertEqual(viewmod.coverage(lv, [{"_key": "big.sh", "_spec": False}], [])[0], 0.0)  # a model name alone


class TestOneOffScripts(unittest.TestCase):
    def lv(self):
        lv = [leaf("python3 tools/sync.py", 4000), leaf("bash …/scratchpad/build.sh", 3000, sub("Film"), scratch=True)]
        lv += [leaf("python3 /tmp/s%d.py" % i, 50, sub("Film"), scratch=True) for i in range(12)]  # 600 s, each < 0.5 %
        lv += [leaf("node /tmp/o%d.mjs" % i, 20, sub("Other"), scratch=True) for i in range(2)]
        lv += [leaf("python3 -c (inline script)", 30, scratch=True)]
        lv += [leaf("cmd%d" % i, 40) for i in range(60)]  # 2400 s of small named commands
        return lv

    def test_groups_one_task_when_it_raises_coverage(self):
        lv = self.lv()
        items = viewmod.build_items(lv, {}, {}, oneoff=True)
        sec = sum(lf["seconds"] for lf in lv)
        top = viewmod.top_items(items, sec)
        labels = [it["label"] for it in top]
        self.assertIn("one-off scripts of Film", labels)
        self.assertIn("bash …/scratchpad/build.sh", [it["what"] for it in top])  # the big named script keeps its own row
        g = next(it for it in top if it["label"] == "one-off scripts of Film")
        self.assertEqual(g["what"], "python3 /tmp/s0.py (+11 other scripts)")
        self.assertEqual((g["seconds"], g["count"]), (600.0, 12))
        self.assertNotIn("one-off scripts of Other", labels)  # 40 s: would not be listed, so not grouped
        self.assertEqual(sum(1 for lf in lv if lf.get("_gkey")), 12)
        self.assertEqual(sum(it["seconds"] for it in items), sec)
        _, t_s, u_s = viewmod.coverage(lv, top, [])
        self.assertGreater(u_s, sum(lf["seconds"] for lf in lv if lf["seconds"] >= 3000) / sec)

    def test_no_group_without_oneoff_or_for_a_single_script(self):
        lv = self.lv()
        self.assertFalse(any(it["_key"].startswith("oneoff|") for it in viewmod.build_items(lv, {}, {})))
        lv = [leaf("a.sh", 600), leaf("python3 /tmp/one.py", 2, sub("Solo"), scratch=True)]
        self.assertFalse(any(it["_key"].startswith("oneoff|") for it in viewmod.build_items(lv, {}, {}, oneoff=True)))

    def test_scratch_script_detection(self):
        self.assertTrue(drill.scratch_script("python3 /tmp/x/check.py", "python3 /tmp/x/check.py"))
        self.assertTrue(drill.scratch_script("bash /srv/u/.claude/scratchpad/build.sh", "bash …/scratchpad/build.sh"))
        self.assertTrue(drill.scratch_script("cd /tmp/work && node run.mjs", "node run.mjs"))
        self.assertTrue(drill.scratch_script("S=/tmp/s; python3 $S/fix.py", "python3 $S/fix.py"))
        self.assertTrue(drill.scratch_script("python3 - <<EOF\nprint(1)\nEOF", "python3 (inline script)"))
        self.assertFalse(drill.scratch_script("python3 tools/sync.py", "python3 tools/sync.py"))
        self.assertFalse(drill.scratch_script("npx vitest run /tmp/x", "npx vitest run"))


class TestCommandKeys(unittest.TestCase):
    def sig(self, cmd, cls=None):
        return drill.signature(cmd, RS, cls)[0]

    def test_script_arguments_and_runner_flags(self):
        self.assertEqual(self.sig("./scripts/launch.sh real2 --x"), "./scripts/launch.sh")
        self.assertEqual(self.sig("python3 tools/verify-ai.py T-123"), "python3 tools/verify-ai.py")
        self.assertEqual(self.sig("./ops.sh run db:check t153-01"), "./ops.sh run db:check")
        self.assertEqual(self.sig("npx tsc --incremental false"), "npx tsc")

    def test_shell_forms(self):
        self.assertEqual(self.sig('for i in $(seq 1 30); do pgrep -f "vitest run" >/dev/null || break; sleep 5; done', "poll_wait"),
                         'while pgrep -f "vitest run"')
        self.assertEqual(self.sig("(case $x in a) echo hi;; *) npm run build;; esac)"), "npm run build")
        self.assertEqual(self.sig("n=$((n+1)); npx vitest run"), "npx vitest run")
        self.assertEqual(self.sig('app-heavy --name x sh -c "npx vitest run src"'), "npx vitest run")
        self.assertEqual(drill.heavy_key('my-gate -- sh -c "npm run build && echo ok"', RS), "npm run build")


class TestPollGroups(Base):
    def test_polls_grouped_by_what_they_wait_for(self):
        tx = Tx("pg")
        tx.human(0)
        b = tx.tool(10, 11, "Bash", {"command": "npx vitest run > /tmp/q/out.log 2>&1", "run_in_background": True},
                    tur={"backgroundTaskId": "b1"})
        notify(tx, 12, b)
        tx.tool(20, 320, "Bash", {"command": "f=/tmp/q/out.log; until grep -q passed $f; do sleep 5; done"})
        tx.tool(330, 630, "Bash", {"command": "until [ -s /tmp/q/out.log ]; do sleep 5; done"})
        tx.tool(640, 940, "Bash", {"command": "while pgrep -f 'playwright test' >/dev/null; do sleep 10; done"})
        tx.tool(950, 1250, "Bash", {"command": "for i in $(seq 1 60); do pgrep -f 'playwright test' >/dev/null || break; sleep 5; done"})
        tx.text(1255)
        tx.write(os.path.join(self.proj, "pg.jsonl"))
        an, rep, (v, check) = self.view()
        poll = next(s for s in v["segments"] if s["id"] == "poll")
        wf = {it["what"]: it.get("waited_for") for it in poll["items"]}
        self.assertEqual(wf.get("until grep -q passed $f (+1 other poll)"), "npx vitest run")  # '$f' resolved to the log
        self.assertEqual(wf.get("while pgrep -f 'playwright test'"), "playwright test")  # no writer: the pattern; '|| break' too
        self.assertEqual(len(poll["items"]), 2)
        self.assertEqual(check["coverage"]["poll"]["items_pct"], 100.0)
        self.assertFalse(poll["tasks_show"])


class TestTasksInView(Base):
    def make(self):
        tx = Tx("tv")
        tx.human(0)
        t = 10
        for i, (desc, scripts) in enumerate([("Film runner", 6), ("Intake fix", 4), ("Demo film", 3), ("Workshop", 2), ("Tidy up", 2)]):
            st = Tx("agent-s%d" % i, sidechain=True)
            u = t + 1
            for j in range(scripts):
                st.tool(u, u + 20, "Bash", {"command": "python3 tools/job_%d_%d.py" % (i, j)})
                u += 21
            st.text(u + 1)
            p = st.write(self.sub("tv", "s%d" % i))
            write_meta(p, agentType="general-purpose", description=desc, toolUseId="toolu_%d" % i, spawnDepth=1)
            tx.tool(t, u + 2, "Agent", {"description": desc, "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "s%d" % i})
            t = u + 5
        tx.text(t + 5)
        tx.write(os.path.join(self.proj, "tv.jsonl"))

    def test_view_tasks_html_and_brief(self):
        self.make()
        an, rep, (v, check) = self.view()
        s = next(s for s in v["segments"] if s["id"] == "scripts")
        self.assertTrue(s["tasks_show"])
        self.assertEqual([t["label"] for t in s["tasks"]], ["Film runner", "Intake fix", "Demo film", "Tidy up"])
        self.assertEqual(s["tasks"][0]["items"], 6)
        self.assertEqual((s["tasks_rest_n"], s["tasks_rest_s"]), (1, 40.0))
        self.assertAlmostEqual(sum(t["seconds"] for t in s["tasks"]) + s["tasks_rest_s"], s["seconds"], delta=0.5)
        self.assertEqual(check["coverage"]["scripts"]["union_pct"], 88.2)  # 300 of 340 s; Workshop 40 s is the rest
        html = vr.render_html(v)
        self.assertIn("<h3>By task</h3>", html)
        self.assertIn('<span class="tt" title="Film runner">Film runner</span>', html)
        brief = viewmod.render_brief(v)
        self.assertIn("- by task: Film runner 2 min · Intake fix 1 min · Demo film 1 min · Tidy up 40 s", brief)

    def test_by_task_hidden_when_items_explain_the_slice(self):
        s = {"id": "scripts", "seconds": 1000.0, "items": [{"what": "a", "seconds": 800.0}],
             "tasks": [{"label": "A", "seconds": 600.0, "items": 1}, {"label": "B", "seconds": 400.0, "items": 1}], "tasks_rest_s": 0}
        self.assertFalse(vr.show_tasks(s))  # older view without tasks_show: falls back to the item share
        s["items"][0]["seconds"] = 650.0
        self.assertTrue(vr.show_tasks(s))
        s["items"][0]["seconds"] = 700.0
        self.assertFalse(vr.show_tasks(s))
        s["tasks_show"] = False
        page = vr.render_html({"segments": [s], "totals": {"agent_s": 1000.0}})
        self.assertNotIn("By task", page)
        s["tasks_show"] = True
        page = vr.render_html({"segments": [s], "totals": {"agent_s": 1000.0}})
        self.assertIn("By task", page)
        s["tasks_rest_s"], s["tasks_rest_n"], s["tasks_rest_max_s"] = 300.0, 7, 60.0
        self.assertIn("7 other tasks, each ≤ 1 min", vr._tasks_html(s))


class TestEvidenceCounts(Base):
    def test_runs_sessions_and_transcripts(self):
        tx = Tx("ev")
        tx.human(0)
        tx.tool(10, 40, "Bash", {"command": "npx tsc --noEmit"})
        tx.tool(50, 52, "Agent", {"description": "typecheck a", "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "a1"})
        tx.tool(53, 55, "Agent", {"description": "typecheck b", "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "a2"})
        tx.text(200)
        tx.write(os.path.join(self.proj, "ev.jsonl"))
        for aid, t0 in (("a1", 60), ("a2", 130)):
            st = Tx("agent-" + aid, sidechain=True)
            st.tool(t0, t0 + 30, "Bash", {"command": "npx tsc --noEmit"})
            st.tool(t0 + 31, t0 + 61, "Bash", {"command": "npx tsc --noEmit -p ."})
            st.text(t0 + 62)
            p = st.write(self.sub("ev", aid))
            write_meta(p, agentType="general-purpose", description="typecheck " + aid[-1], toolUseId="toolu_" + aid, spawnDepth=1)
        an, rep, (v, _) = self.view()
        f = next(c for c in rep["ranked"] if c["id"] == "ts-tsc-not-incremental")
        self.assertTrue(f["evidence"].startswith("5 runs, 2.5m measured, 1 session (3 transcripts); e.g. npx tsc"), f["evidence"])
        self.assertIn("5 runs, 2.5m measured", str(f["arithmetic"]))
        self.assertNotIn("session(s)", f["evidence"])
        self.assertEqual(core._where({"sessions": 2, "transcripts": 2}), "2 sessions")
        self.assertEqual(core._where({"sessions": 1}), "1 session")


if __name__ == "__main__":
    unittest.main()
