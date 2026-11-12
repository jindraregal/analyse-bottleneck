"""Leaf attribution (drill.py) and the one-chart view model (view.py). Synthetic fixtures only."""
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

from fixtures import Tx, iso, make_project  # noqa: E402
from rbn import core, drill, view as viewmod  # noqa: E402
from rbn.rules import RuleSet  # noqa: E402
import analyze as cli  # noqa: E402

RS = RuleSet()
OPTS = {"idle_threshold": 180.0}


def notify(tx, t, tid, status="completed"):
    tx.rows.append({"type": "user", "timestamp": iso(t), "sessionId": tx.sid, "origin": {"kind": "task-notification"},
                    "message": {"role": "user", "content": "<task-notification><task-id>x</task-id><tool-use-id>%s</tool-use-id>"
                                                           "<status>%s</status></task-notification>" % (tid, status)}})


def write_meta(path, **kw):
    with open(path[:-len(".jsonl")] + ".meta.json", "w", encoding="utf-8") as f:
        json.dump(kw, f)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.proj = make_project(self.tmp, "proj-drill")

    def tearDown(self):
        self._td.cleanup()

    def sub(self, sid, agent_id, workflow=None):
        d = os.path.join(self.proj, sid, "subagents")
        if workflow:
            d = os.path.join(d, "workflows", workflow)
        return os.path.join(d, "agent-%s.jsonl" % agent_id)

    def analyses(self):
        out = []
        for g in core.find_groups(project="proj-drill", root=self.tmp):
            a = core.analyze_group(g, RS, (None, None), OPTS)
            if a:
                out.append(a)
        return out

    def view(self):
        an = self.analyses()

        class NS(object):
            top = 1000
            host_snapshot = False
        rep = core.build_report(an, RS, OPTS, NS())
        return an, rep, viewmod.build_view_checked(rep, an, {"rs": RS, "title": "demo", "idle_threshold": 180.0})

    def leaves(self):
        an = self.analyses()
        return an, drill.drill_analyses(an, RS)

    @staticmethod
    def by(leaves, field="what"):
        out = {}
        for lf in leaves:
            out[lf[field]] = out.get(lf[field], 0.0) + lf["seconds"]
        return out


class TestDrill(Base):
    def make_main(self):
        tx = Tx("main1")
        tx.human(0)
        # 1) waits on a subagent that runs the e2e suite
        tx.tool(10, 120, "Agent", {"description": "e2e verify", "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "sub1"})
        # 2) a background build it waits for
        bid = tx.tool(125, 126, "Bash", {"command": "cd app && npm run build", "run_in_background": True, "description": "Build the app"},
                      tur={"backgroundTaskId": "b1"})
        notify(tx, 306, bid)
        # 3) a chained command: the slow part is the test, not the git pull
        tx.tool(310, 400, "Bash", {"command": "git pull --rebase --autostash -q && npx playwright test e2e/a.spec.ts 2>&1 | tail -5"})
        # 4) a subagent without any transcript
        tx.tool(405, 505, "Agent", {"description": "ghost job", "prompt": "x", "subagent_type": "Explore"}, tur={"agentId": "ghost"})
        # 5) a poll loop: names what it polls
        tx.tool(510, 570, "Bash", {"command": "sleep 60; gh run view 42 --json status", "description": "Wait for CI"})
        tx.text(575)
        tx.write(os.path.join(self.proj, "main1.jsonl"))
        sub = Tx("agent-sub1", sidechain=True)
        sub.tool(12, 112, "Bash", {"command": "cd app && timeout 900 my-heavy --mem 8G -- npx playwright test e2e --workers=2", "description": "Run e2e suite"})
        sub.text(118)
        p = sub.write(self.sub("main1", "sub1"))
        write_meta(p, agentType="general-purpose", description="e2e verify", toolUseId="toolu_x", spawnDepth=1)

    def test_subagent_wait_resolves_to_child_command(self):
        self.make_main()
        an, d = self.leaves()
        tests = [lf for lf in d["leaves"] if lf["cat"] == "test" and lf["via"].startswith("subagent")]
        self.assertAlmostEqual(sum(lf["seconds"] for lf in tests), 100, delta=1)
        self.assertEqual({lf["what"] for lf in tests}, {"npx playwright test"})
        self.assertEqual({lf["via"] for lf in tests}, {"subagent: e2e verify (general-purpose)"})
        self.assertEqual({lf["label"] for lf in tests}, {"Run e2e suite"})

    def test_background_wait_is_the_background_command(self):
        self.make_main()
        an, d = self.leaves()
        b = [lf for lf in d["leaves"] if lf["cat"] == "build"]
        self.assertAlmostEqual(sum(lf["seconds"] for lf in b), 181, delta=2)
        self.assertEqual({lf["what"] for lf in b}, {"npm run build"})
        self.assertEqual({lf["via"] for lf in b}, {"main session"})

    def test_chained_command_names_the_slow_part(self):
        self.make_main()
        an, d = self.leaves()
        main_tests = [lf for lf in d["leaves"] if lf["cat"] == "test" and lf["via"] == "main session"]
        self.assertEqual({lf["what"] for lf in main_tests}, {"npx playwright test"})
        self.assertAlmostEqual(sum(lf["seconds"] for lf in main_tests), 90, delta=1)

    def test_poll_loop_names_what_is_polled(self):
        self.make_main()
        an, d = self.leaves()
        polls = [lf for lf in d["leaves"] if lf["cat"] == "poll_wait"]
        self.assertEqual({lf["what"] for lf in polls}, {"gh run view"})
        self.assertEqual({lf["label"] for lf in polls}, {"Wait for CI"})

    def test_unresolvable_subagent_is_unknown_wait(self):
        self.make_main()
        an, d = self.leaves()
        unk = [lf for lf in d["leaves"] if lf["cat"] == drill.UNKNOWN]
        ghost = [lf for lf in unk if "ghost job" in lf["label"]]
        self.assertAlmostEqual(sum(lf["seconds"] for lf in ghost), 100, delta=1)
        self.assertEqual(ghost[0]["what"], drill.REASONS["none"])
        self.assertAlmostEqual(sum(d["unresolved"].values()), sum(lf["seconds"] for lf in unk), delta=0.01)
        # nothing of the ghost wait leaks into another category
        self.assertFalse([lf for lf in d["leaves"] if "ghost" in lf["label"] and lf["cat"] != drill.UNKNOWN])

    def test_leaves_sum_to_autonomous_and_categories_match_timeline(self):
        self.make_main()
        an, d = self.leaves()
        auto = sum(a["tl"]["autonomous"] for a in an)
        self.assertAlmostEqual(sum(lf["seconds"] for lf in d["leaves"]), auto, delta=0.01)
        self.assertAlmostEqual(d["autonomous"], auto, delta=0.01)
        for k, v in an[0]["tl"]["by_cat"].items():
            self.assertAlmostEqual(d["by_cat"].get(k, 0), v, delta=0.01, msg=k)

    def test_model_time_named_by_model_and_place(self):
        self.make_main()
        an, d = self.leaves()
        whats = self.by([lf for lf in d["leaves"] if lf["cat"] in ("inference", "thinking", "api_wait")], "label")
        self.assertIn("claude-opus-test in main session", whats)
        self.assertIn("claude-opus-test in subagents", whats)


class TestNested(Base):
    def test_nested_and_workflow_children(self):
        tx = Tx("main2")
        tx.human(0)
        tx.tool(10, 200, "Agent", {"description": "outer", "prompt": "x", "subagent_type": "general-purpose"}, tur={"agentId": "outer1"})
        wid = tx.tool(205, 206, "Workflow", {"script": "x"}, tur={"status": "async_launched", "runId": "wf_1", "workflowName": "nightly"})
        notify(tx, 320, wid)
        tx.text(325)
        tx.write(os.path.join(self.proj, "main2.jsonl"))
        outer = Tx("agent-outer1", sidechain=True)
        outer.tool(12, 190, "Agent", {"description": "inner pytest", "prompt": "y", "subagent_type": "general-purpose"}, tur={"agentId": "inner1"})
        outer.text(195)
        po = outer.write(self.sub("main2", "outer1"))
        write_meta(po, agentType="general-purpose", description="outer", spawnDepth=1)
        inner = Tx("agent-inner1", sidechain=True)
        inner.tool(15, 185, "Bash", {"command": "uv run pytest -x tests/"})
        inner.text(188)
        pi = inner.write(self.sub("main2", "inner1"))
        write_meta(pi, agentType="general-purpose", description="inner pytest", parentAgentId="outer1", spawnDepth=2)
        wf = Tx("agent-w1", sidechain=True)
        wf.tool(210, 310, "Bash", {"command": "npm test"})
        wf.text(315)
        pw = wf.write(self.sub("main2", "w1", workflow="wf_1"))
        write_meta(pw, agentType="workflow-subagent", description="research", workflowPhase="One", spawnDepth=1)
        an, d = self.leaves()
        self.assertEqual(len(an[0]["subs"]), 3)  # workflow agents are discovered too
        tests = {}
        for lf in d["leaves"]:
            if lf["cat"] == "test":
                tests[(lf["what"], lf["via"])] = tests.get((lf["what"], lf["via"]), 0) + lf["seconds"]
        self.assertAlmostEqual(tests[("pytest", "subagent: inner pytest (general-purpose)")], 170, delta=1)
        self.assertAlmostEqual(tests[("npm test", "workflow agent: research (nightly)")], 100, delta=1)
        auto = an[0]["tl"]["autonomous"]
        self.assertAlmostEqual(sum(lf["seconds"] for lf in d["leaves"]), auto, delta=0.01)


class TestView(Base):
    def make(self):
        TestDrill.make_main(self)

    def test_view_contract(self):
        self.make()
        an, rep, (v, check) = self.view()
        self.assertEqual(v["schema"], "run-bottleneck/view@1")
        for k in ("schema", "title", "generated", "window", "totals", "headline", "segments", "basis"):
            self.assertIn(k, v)
        self.assertEqual(set(v), {"schema", "title", "generated", "window", "totals", "headline", "segments", "basis", "waiting"})
        for sg in v["segments"]:
            for it in sg["items"]:
                self.assertTrue(1 <= len(it["short"].split()) <= 5, it["short"])
        for k in ("since", "until", "sessions", "subagent_sessions", "transcript_files"):
            self.assertIn(k, v["window"])
        self.assertEqual([e["id"] for e in v["totals"]["excluded"]], ["human", "idle"])
        self.assertLessEqual(len(v["headline"]), 140)
        self.assertTrue(4 <= len(v["segments"]) <= 9, [s["id"] for s in v["segments"]])
        secs = [s["seconds"] for s in v["segments"]]
        self.assertEqual(secs, sorted(secs, reverse=True))
        self.assertAlmostEqual(sum(secs), v["totals"]["agent_s"], delta=0.005 * v["totals"]["agent_s"])
        seg_keys = {"id", "label", "seconds", "pct", "summary", "items", "rest_s", "rest_n", "tasks", "tasks_rest_s", "tasks_rest_n",
                    "tasks_rest_max_s", "tasks_show", "actions", "notes"}
        item_keys = {"label", "what", "seconds", "count", "p50_s", "via", "sessions"}
        act_keys = {"id", "title", "saving_min", "confidence", "source", "evidence", "why", "arithmetic", "before", "after"}
        for s in v["segments"]:
            self.assertEqual(set(s), seg_keys)
            self.assertLessEqual(len(s["summary"]), 110)
            self.assertLessEqual(len(s["items"]), 6)
            self.assertLessEqual(len(s["actions"]), 3)
            self.assertEqual(s["notes"], [])
            self.assertAlmostEqual(s["seconds"], sum(i["seconds"] for i in s["items"]) + s["rest_s"], delta=0.5)
            self.assertLessEqual(len(s["tasks"]), 6)  # view_render.MAX_TASKS
            self.assertAlmostEqual(s["seconds"], sum(t["seconds"] for t in s["tasks"]) + s["tasks_rest_s"], delta=0.5)
            for t in s["tasks"]:
                self.assertEqual(set(t), {"label", "seconds", "items"})
            for it in s["items"]:
                self.assertLessEqual(item_keys, set(it))
                self.assertLessEqual(set(it) - item_keys, {"n_tasks", "est_s", "waited_for", "short", "short_src"})
                if "n_tasks" in it:
                    self.assertGreater(it["n_tasks"], 1)
                self.assertLessEqual(len(it["what"]), 90)
            for a in s["actions"]:
                self.assertEqual(set(a), act_keys)
                self.assertEqual(a["source"], "rule")
                self.assertLessEqual(len(a["title"]), 70)
        self.assertTrue(5 <= len(v["basis"]) <= 12)
        for b in v["basis"]:
            self.assertEqual(set(b), {"what", "how", "kind", "detail"})
            self.assertIn(b["kind"], ("deterministic", "measured", "llm", "missing", "none"))
        ids = {s["id"] for s in v["segments"]}
        self.assertIn("unknown_wait", ids)
        self.assertIn("tests", ids)
        self.assertAlmostEqual(check["leaf_sum_s"], check["report_autonomous_s"], delta=0.2)
        json.dumps(v)

    def test_headline_names_biggest_slice_and_item(self):
        self.make()
        an, rep, (v, _) = self.view()
        top = v["segments"][0]
        self.assertIn(top["label"].lower(), v["headline"].lower())
        self.assertIn(top["items"][0]["label"][:10], v["headline"])

    def test_notes_merge_marked_ai_and_headline_kept(self):
        self.make()
        an, rep, (v, _) = self.view()
        notes = {"model": "m", "headline": "AI says hi", "segments": {"tests": [{"title": "Run fewer specs", "detail": "d", "evidence": ["tests.1"]}]}}
        m = viewmod.merge_notes(v, notes)
        self.assertEqual(m["headline"], v["headline"])
        seg = [s for s in m["segments"] if s["id"] == "tests"][0]
        self.assertEqual(seg["notes"][0]["source"], "llm")
        self.assertEqual(sum(1 for b in m["basis"] if b["kind"] == "llm"), 1)
        self.assertFalse([b for b in m["basis"] if b["what"] == "AI notes" and b["how"] == "none yet"])
        self.assertEqual(v["segments"][0]["notes"], [])  # input not mutated

    def test_brief_is_short_and_has_numbers(self):
        self.make()
        an, rep, (v, _) = self.view()
        b = viewmod.render_brief(v)
        self.assertLessEqual(len(b.splitlines()), 60)
        self.assertIn("tests.1 npx playwright test", b)
        self.assertNotIn("/" + "home/", b)

    def test_cli_view_and_render(self):
        self.make()
        out = os.path.join(self.tmp, "out")
        os.environ["RB_PROJECTS_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "RB_PROJECTS_DIR", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["view", "--project", "proj-drill", "--out", out, "--name", "v", "--no-host-snapshot"])
        self.assertEqual(rc, 0, buf.getvalue())
        with open(os.path.join(out, "v.view.json"), encoding="utf-8") as f:
            v = json.load(f)
        self.assertEqual(v["schema"], "run-bottleneck/view@1")
        self.assertTrue(os.path.isfile(os.path.join(out, "v.brief.md")))
        try:
            import rbn.view_render  # noqa: F401
        except ImportError:
            return
        self.assertTrue(os.path.isfile(os.path.join(out, "v.html")))
        npath = os.path.join(self.tmp, "notes.json")
        with open(npath, "w", encoding="utf-8") as f:
            json.dump({"model": "m", "segments": {"tests": [{"title": "Shard the suite", "detail": "x", "evidence": ["tests.1"]}]}}, f)
        with redirect_stdout(io.StringIO()):
            rc = cli.main(["render", os.path.join(out, "v.view.json"), "--notes", npath, "--out", os.path.join(out, "n.html")])
        self.assertEqual(rc, 0)
        with open(os.path.join(out, "n.html"), encoding="utf-8") as f:
            self.assertIn("Shard the suite", f.read())


class TestBasisDora(unittest.TestCase):
    def rows(self, dora, extra=0):
        from rbn import view as V
        v = {"window": {"sessions": 1, "subagent_sessions": 0, "since": None, "until": None},
             "totals": {"excluded": [{"seconds": 60}, {"seconds": 60}]}}
        d = {"unresolved": {"none": 5.0} if extra else {}}
        rep = {"telemetry": {"dora": dora, "latency": {"matched": 3} if extra else {}}}
        return V._basis(v, d, rep, {"dup_records": 2 if extra else 0, "sysstat": False})

    def test_dora_row_when_room_and_ai_row_kept(self):
        dora = {"mainline_changes": 62, "window_days": 7.0, "reverts": 1,
                "metrics": {"deployment_frequency_per_day": {"value": 8.86, "kind": "proxy (no deploy source found)"},
                            "lead_time_for_changes_h": {"p50": 0.0, "p90": 0.6}, "change_failure_rate": {"value": 0.016}}}
        rows = self.rows(dora)
        row = next(b for b in rows if b["what"] == "Delivery (DORA)")
        self.assertIn("8.9/day, proxy: no deploy source found", row["detail"])
        self.assertIn("1 revert (1.6%)", row["detail"])
        self.assertEqual(rows[-1]["what"], "AI notes")
        full = self.rows(dora, extra=1)  # known issue 2: no row cap, the DORA row stays when there are many rows
        self.assertTrue(any(b["what"] == "Delivery (DORA)" for b in full))
        self.assertEqual(full[-1]["what"], "AI notes")
        self.assertEqual(full[-1]["kind"], "none")


class TestForkAndDeploy(Base):
    def test_forked_session_counts_copied_records_once(self):
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
        fork = Tx("fork")
        fork.rows = [dict(r, sessionId="fork") for r in tx.rows]  # a forked session repeats the history with the same uuids
        fork.n = 1000  # its own records get new ids
        fork.human(400)
        fork.tool(410, 470, "Bash", {"command": "npm run build"})
        fork.text(475)
        for i, r in enumerate(fork.rows):
            r.setdefault("uuid", "fork-%d" % i)
        fork.write(os.path.join(self.proj, "fork.jsonl"))
        an = core.analyze_groups(core.find_groups(project="proj-drill", root=self.tmp), RS, (None, None), OPTS)
        d = drill.drill_analyses(an, RS)
        by = self.by(d["leaves"], "cat")
        self.assertAlmostEqual(by.get("test", 0), 288, delta=2)  # once, not twice
        self.assertAlmostEqual(by.get("build", 0), 60, delta=1)
        self.assertLess(by.get(drill.UNKNOWN, 0), 15)  # only the child's own start/finish edges, not the copied 300 s wait
        self.assertGreater(sum(a["main"]["dup_records"] for a in an), 0)

    def test_chained_deploy_is_named_by_its_deploy_step(self):
        tx = Tx("dep")
        tx.human(0)
        tx.tool(10, 910, "Bash", {"command": "git pull --rebase --autostash -q && S=$(date -u +%s) && PATH=~/bin:$PATH ops-deploy --wait; "
                                             "V=$(curl -s https://ops.example.com/api/health | python3 -c 'print(1)'); echo live $V",
                                  "description": "Deploy main to prod"})
        tx.text(915)
        tx.write(os.path.join(self.proj, "dep.jsonl"))
        an, rep, (v, _) = self.view()
        seg = {s["id"]: s for s in v["segments"]}
        self.assertIn("deploys", seg)
        self.assertEqual(seg["deploys"]["items"][0]["what"], "ops-deploy")
        self.assertEqual(seg["deploys"]["items"][0]["label"], "Deploy main to prod")
        self.assertAlmostEqual(seg["deploys"]["seconds"], 900, delta=2)
        self.assertEqual(drill.signature("npm run build && ./deploy.sh", RS, "build")[0], "npm run build")

    def test_long_heredoc_command_keeps_the_command_after_the_body(self):
        body = "\n".join("s = s.replace('a%d', 'b%d')" % (i, i) for i in range(80))
        tx = Tx("hd")
        tx.human(0)
        tx.tool(10, 610, "Bash", {"command": "cd app && python3 - <<'EOF'\n%s\nEOF\nnpx playwright test e2e/a.spec.ts --reporter=line" % body})
        tx.text(615)
        tx.write(os.path.join(self.proj, "hd.jsonl"))
        an, d = self.leaves()
        tests = self.by([lf for lf in d["leaves"] if lf["cat"] == "test"])
        self.assertAlmostEqual(tests.get("npx playwright test", 0), 600, delta=2)

    def test_item_label_is_the_description_that_took_the_time(self):
        tx = Tx("lab")
        tx.human(0)
        for i in range(3):
            tx.tool(10 + i * 5, 11 + i * 5, "Bash", {"command": "python3 - <<'EOF'\nprint(1)\nEOF", "description": "Quick check %d" % i})
        tx.tool(30, 630, "Bash", {"command": "python3 - <<'EOF'\nimport time\nEOF", "description": "Long migration"})
        tx.text(635)
        tx.write(os.path.join(self.proj, "lab.jsonl"))
        an, rep, (v, _) = self.view()
        items = [it for s in v["segments"] for it in s["items"] if it["count"] == 4]
        self.assertEqual(items[0]["label"], "Long migration")
        # no single run explains most of it: the signature names the item, not one arbitrary run
        an, d = self.leaves()
        lv = [dict(lf, seg0="x") for lf in d["leaves"] if lf["what"] == "python3 - (inline script)"]
        for lf in lv:
            if lf["label"] == "Long migration":
                lf["seconds"] = 1.0
        self.assertEqual(viewmod.build_items(lv)[0]["label"], "python3 - (inline script)")


class TestSegmentsChoice(unittest.TestCase):
    def test_small_slices_fold_into_other_but_unknown_stays(self):
        cfg = RS.segments
        sec = {"tests": 50.0, "model": 30.0, "network": 10.0, "files": 1.0, "media": 1.5, "unknown_wait": 0.8, "other": 6.7}
        vis = viewmod.choose_segments(sec, 100.0, cfg)
        self.assertIn("unknown_wait", vis)
        self.assertNotIn("files", vis)
        self.assertNotIn("media", vis)

    def test_other_lists_folded_slices_by_name(self):
        def lf(seg0, what, sec, i, desc=""):
            return {"seg0": seg0, "key": what, "what": what, "label": desc or what, "seconds": sec, "inst": ("s", "t%d" % i), "dur": sec,
                    "via": "main session", "group": "g", "desc": desc}
        leaves = [lf("media", "ffmpeg", 50, 1, "Rebuild film"), lf("media", "ffmpeg", 10, 2), lf("other", "WebSearch", 5, 3),
                  lf("servers", "npx next dev", 30, 4)]
        items = viewmod.build_other_items(leaves, {"media": "Media encode", "servers": "Servers & containers"})
        self.assertEqual([i["label"] for i in items], ["Media encode, mostly Rebuild film", "Servers & containers, mostly npx next dev", "WebSearch"])
        self.assertEqual(items[0]["count"], 2)
        self.assertAlmostEqual(sum(i["seconds"] for i in items), 95)

    def test_at_most_nine(self):
        sec = {k: 10.0 for k in ("tests", "builds", "installs", "poll", "model", "network", "browser", "media", "servers", "scripts", "files")}
        vis = viewmod.choose_segments(sec, 110.0, RS.segments)
        self.assertLessEqual(len(vis) + 1, 9)



class TestSignature(unittest.TestCase):
    def sig(self, cmd, cls):
        return drill.signature(cmd, RS, cls)[0]

    def test_wrappers_chains_and_loops(self):
        self.assertEqual(self.sig("for d in app web; do (cd $d && npm ci); done", "install"), "npm ci")
        self.assertEqual(self.sig("FOO=1 nice -n 10 timeout 600 npx playwright test e2e/a.spec.ts", "test"), "npx playwright test")
        self.assertEqual(self.sig("x=$(curl -s https://api.example.com/v1/runs)", "network"), "curl api.example.com/v1")
        self.assertEqual(self.sig("while true; do gh run view 1 --json status; sleep 30; done", "poll_wait"), "gh run view")
        self.assertEqual(self.sig("until [ -f /tmp/vt/DONE ]; do sleep 20; done; ls /tmp/vt", "poll_wait"), "until [ -f /tmp/vt/DONE ]")
        tmp = "/tmp/claude-1001/-home-u-git-app/0a1b2c3d-1111-4222-8333-444455556666/scratchpad"
        self.assertEqual(self.sig("timeout 900 bash -c 'until grep -q rc= %s/x.out; do sleep 5; done'" % tmp, "poll_wait"),
                         "until grep -q rc= …/scratchpad/x.out")
        self.assertEqual(drill.signature('my-heavy --mem 8G -- sh -c "npx vitest run src/a; echo exit $?"', RS, "test")[1], "npx vitest run src/a")

    def test_heredoc_body_is_not_a_command(self):
        cmd = "cd app && python3 - <<'EOF'\nold = 'npx vitest run'\nEOF\nnpx playwright test e2e/a.spec.ts"
        self.assertEqual(self.sig(cmd, "test"), "npx playwright test")
        self.assertEqual(self.sig("python3 - <<'EOF'\nimport vitest\nEOF", "test"), "python3 - (inline script)")

    def test_server_path_is_not_a_server(self):
        from rbn.classify import classify_bash
        self.assertEqual(classify_bash("npx vitest run src/server/a.test.ts", RS), "test")
        self.assertEqual(classify_bash("npx serve dist", RS), "service")

    def test_fifo_and_read_timeouts_are_waits(self):
        from rbn.classify import classify_bash
        fifo = "rm -f waitfifo && mkfifo waitfifo && (timeout 240 cat waitfifo; true); rm -f waitfifo"
        self.assertEqual(classify_bash(fifo, RS), "poll_wait")
        self.assertEqual(classify_bash("read -r -t 30 x <> /tmp/f", RS), "poll_wait")
        self.assertEqual(classify_bash("read line < file", RS), "shell_other")

    def test_plain_programs_take_no_subcommand(self):
        self.assertEqual(self.sig("ffmpeg -v error -i a.mp4 b.png", "media_gpu"), "ffmpeg")
        self.assertEqual(self.sig("git status && ./scripts/long_job.sh", "git"), "./scripts/long_job.sh")


if __name__ == "__main__":
    unittest.main()
