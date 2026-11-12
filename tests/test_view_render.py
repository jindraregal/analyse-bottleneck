import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from rbn import view_render as vr  # noqa: E402

H = 3600.0


def synthetic_view():
    """Five segments: tests (with an action), model time, network, unknown wait, other."""
    segs = [
        {"id": "tests", "label": "Tests", "seconds": 9.8 * H,
         "summary": "Mostly 'npx playwright test' (6.1 h) run by subagent 'e2e verify'.",
         "items": [
             {"label": "<script>alert(1)</script> e2e run", "what": "npx playwright test --project=chromium",
              "seconds": 6.1 * H, "count": 14, "p50_s": 1500.0,
              "via": "subagent: e2e verify (general-purpose) · main session", "sessions": 4},
             {"label": "Run unit tests", "what": "pnpm vitest run", "seconds": 2.2 * H, "count": 31, "p50_s": 240.0,
              "via": "main session", "sessions": 6},
         ],
         "rest_s": 1.5 * H,
         "actions": [{"id": "playwright-workers", "title": "Run Playwright with --workers=4 and only changed specs",
                      "saving_min": 140.0, "confidence": "medium", "source": "rule",
                      "evidence": "14 runs, p50 25 min, 1 worker", "why": "Specs run serially on a 4-core host.",
                      "arithmetic": "6.1 h x (1 - 1/2.5) = 3.7 h; x 0.6 confidence = 2.3 h",
                      "before": "25 min per run", "after": "about 10 min per run"}],
         "notes": []},
        {"id": "model", "label": "Model thinking & writing", "seconds": 8.6 * H,
         "summary": "claude-opus-5-5 in the main session 5.2 h.",
         "items": [{"label": "claude-opus-5-5 in main session", "what": "claude-opus-5-5", "seconds": 5.2 * H,
                    "count": 812, "p50_s": 14.0, "via": "main session", "sessions": 9}],
         "rest_s": 3.4 * H, "actions": [], "notes": []},
        {"id": "network", "label": "Network, git & APIs", "seconds": 8.0 * H,
         "summary": "Mostly 'sleep 600 && curl -s https-less health check' (5.0 h).",
         "items": [{"label": "Wait for deploy", "what": "sleep 600 && curl -s localhost:3000/health | jq '.ok' && echo \"done\"",
                    "seconds": 5.0 * H, "count": 5, "p50_s": 3600.0, "via": "main session", "sessions": 2}],
         "rest_s": 3.0 * H, "actions": [], "notes": []},
        {"id": "unknown_wait", "label": "Waiting — cause unknown", "seconds": 5.2 * H,
         "summary": "Subagent ran with no transcript (5.2 h).", "items": [], "rest_s": 5.2 * H,
         "actions": [], "notes": []},
        {"id": "other", "label": "Other", "seconds": 3.5 * H, "summary": "Edits, reads and small commands.",
         "items": [], "rest_s": 3.5 * H, "actions": [], "notes": []},
    ]
    agent = sum(s["seconds"] for s in segs)
    for s in segs:
        s["pct"] = round(100 * s["seconds"] / agent, 2)
    return {
        "schema": "run-bottleneck/view@1", "title": "demo-app", "generated": "2026-11-09T14:07:00+00:00",
        "window": {"since": "2026-11-02T14:00:00+00:00", "until": "2026-11-09T14:00:00+00:00", "sessions": 41,
                   "subagent_sessions": 118, "transcript_files": 159},
        "totals": {"agent_s": agent, "span_s": agent + 175.8 * H,
                   "excluded": [{"id": "human", "label": "Waiting for you", "seconds": 116.4 * H},
                                {"id": "idle", "label": "Idle (nobody working)", "seconds": 59.4 * H}]},
        "headline": "35.1 h agent time. Biggest: tests 9.8 h, mostly 'npx playwright test' run by subagent 'e2e verify'.",
        "segments": segs,
        "basis": [
            {"what": "Slice sizes", "how": "Sweep-line over transcript timestamps", "kind": "deterministic",
             "detail": "Each second goes to one category by priority."},
            {"what": "Commands per slice", "how": "Bash tool calls, wrappers unwrapped", "kind": "deterministic",
             "detail": "Signatures redacted."},
            {"what": "Host load", "how": "Not collected", "kind": "missing", "detail": "Run with --sysstat."},
            {"what": "Hook latency", "how": "Telemetry log", "kind": "measured", "detail": ""},
            {"what": "Actions", "how": "rules/*.json", "kind": "deterministic", "detail": "Ranked by score."},
        ],
    }


def synthetic_notes():
    return {"model": "claude-opus-5-5",
            "headline": "Most of the test time is one serial Playwright suite.",
            "segments": {"network": [{"title": "Replace the 10-minute sleep with a readiness poll",
                                      "detail": "The deploy is usually ready after 3-4 minutes.",
                                      "evidence": ["sleep 600 x5", "p50 60 min"]}],
                         "nope": [{"title": "dropped", "detail": "", "evidence": []}]}}


def data_json(page):
    m = re.search(r'<script type="application/json" id="rb-view">(.*?)</script>', page, re.S)
    return json.loads(m.group(1))


def donut(page):
    return re.search(r'<svg class="donut.*?</svg>', page, re.S).group(0)


class ViewRenderTest(unittest.TestCase):
    def setUp(self):
        self.view = synthetic_view()
        self.page = vr.render_html(self.view, synthetic_notes())

    def test_one_donut_with_a_path_per_segment(self):
        self.assertEqual(self.page.count('<svg class="donut'), 1)
        svg = donut(self.page)
        self.assertEqual(svg.count("<path"), len(self.view["segments"]))
        self.assertEqual(svg.count('<path class="slice '), 5)
        self.assertIn("url(#rb-hatch)", self.page)  # unknown wait is hatched
        self.assertIn('class="slice cu', svg)

    def test_data_json_round_trips_with_notes(self):
        data = data_json(self.page)
        self.assertEqual(data["schema"], "run-bottleneck/view@1")
        self.assertEqual([s["id"] for s in data["segments"]], [s["id"] for s in self.view["segments"]])
        net = [s for s in data["segments"] if s["id"] == "network"][0]
        self.assertEqual(net["notes"][0]["source"], "llm")
        self.assertEqual(data["notes_unmatched"], ["nope"])
        self.assertEqual(data["headline"], self.view["headline"])  # deterministic headline never replaced
        self.assertEqual(data["segments"][0]["items"][0]["label"], "<script>alert(1)</script> e2e run")

    def test_no_external_requests(self):
        # nothing is loaded from elsewhere; the only absolute URLs are the footer links (project and author)
        self.assertIsNone(re.search(r"""src\s*=\s*["']?(?:https?:)?//""", self.page))
        links = set(re.findall(r"https?://[^\s\"'<>]+", self.page))
        self.assertEqual(links, {vr.REPO_URL, vr.AUTHOR_URL})
        self.assertEqual(set(re.findall(r'<a [^>]*href="([^"]+)"', self.page)), {vr.REPO_URL, vr.AUTHOR_URL})
        self.assertNotIn("@import", self.page)

    def test_escaping(self):
        self.assertNotIn("<script>alert(1)", self.page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; e2e run", self.page)
        self.assertIn("jq &#x27;.ok&#x27; &amp;&amp; echo &quot;done&quot;", self.page)
        self.assertEqual(self.page.count("<script"), 2)  # the JSON blob and the page script, nothing injected
        self.assertEqual(self.page.count("</script>"), 2)

    def test_notes_and_badges(self):
        self.assertIn('<span class="bd b-ai">AI</span>', self.page)
        self.assertIn("Replace the 10-minute sleep with a readiness poll", self.page)
        self.assertNotIn('class="aihead"', self.page)  # no AI summary in the header: notes live in their slices
        self.assertIn('<span class="bd b-rule">rule</span>', self.page)
        self.assertIn("saves 2.3 h", self.page)
        self.assertIn("Interpreted by AI", self.page)
        visible = self.page.split('<script type="application/json"', 1)[0]
        self.assertNotIn("Most of the test time is one serial Playwright suite.", visible)

    def test_starts_on_all_slices(self):
        # nothing picked at first: the All panel shows what to do first across slices; every slice panel is hidden
        panels = re.findall(r'<section class="panel" id="rb-p(\w+)" data-i="\w+" aria-labelledby="rb-h\w+"( hidden)?>', self.page)
        self.assertEqual(len(panels), 6)
        self.assertEqual([p for p, h in panels if not h], ["all"])
        self.assertEqual(re.findall(r'<button type="button" class="lg[^"]*" data-i="(\w+)" aria-pressed="true"', self.page), ["all"])
        self.assertNotIn(" sel\"", donut(self.page))
        self.assertIn('<svg class="donut all"', self.page)
        self.assertIn('<g class="slices">', self.page)  # no has-sel: every slice at full strength
        pall = self.page.split('id="rb-pall"', 1)[1].split("</section>", 1)[0]
        self.assertIn("<h3>Do this first</h3>", pall)
        self.assertIn("Run Playwright with <span class=\"nw\">--workers</span>=4", pall)
        self.assertIn("Replace the 10-minute sleep with a readiness poll", pall)
        self.assertLess(pall.index("Run Playwright"), pall.index("Replace the 10-minute"))  # rule with a saving first
        self.assertIn(">Tests</h2>", self.page)

    def test_do_this_first_keeps_room_for_ai_notes(self):
        v = synthetic_view()
        for k in range(6):  # a slice shows at most 3 rules: 3 on Tests, 3 on Model
            v["segments"][k // 3]["actions"].append({"id": "r%d" % k, "title": "Rule number %d" % k, "saving_min": 60.0 - k})
        v["segments"][0]["actions"] = v["segments"][0]["actions"][1:]  # drop the fixture's own Playwright rule
        page = vr.render_html(v, synthetic_notes())
        pall = page.split('id="rb-pall"', 1)[1].split("</section>", 1)[0].split("<h3>", 2)[1]
        self.assertIn("Replace the 10-minute sleep with a readiness poll", pall)  # 4 rules, then the AI note
        self.assertIn("Rule number 3", pall)
        self.assertNotIn("Rule number 5", pall)
        self.assertIn("Rule number 4", pall)
        self.assertIn("+ 1 more in the slices", page)

    def test_deselect_controls(self):
        self.assertIn('class="hole"', self.page)  # the centre goes back to All
        self.assertIn('class="tb back" data-i="all"', self.page)
        self.assertEqual(self.page.count('class="tb xall"'), 6)  # Expand all in every panel
        self.assertIn('if (i === cur && i !== "all") i = "all";', self.page)  # picking the picked slice again
        self.assertIn('e.key === "Escape"', self.page)

    def test_text_bits(self):
        self.assertNotIn("Not in the chart", self.page)
        self.assertNotIn('class="headline"', self.page)
        self.assertNotIn("Do first", self.page)
        self.assertIn(">2–9 Nov<", self.page)
        self.assertIn(">2026, 7 days<", self.page)
        self.assertIn('data-ct="of 35.1 h agent time"', self.page)  # no waiting slice in this view: the ring is agent time
        self.assertIn('class="lede">Agents worked 35.1 h on their own. Pick a slice', self.page)
        self.assertIn('<details class="about">', self.page)
        self.assertIn("How to read this", self.page)

    def test_stats_row_with_icons(self):
        stats = re.search(r'<ul class="stats".*?</ul>', self.page, re.S).group(0)
        self.assertEqual(stats.count('<li class="st"'), 4)  # to save, recommendations, period, sessions
        self.assertEqual(stats.count("<svg"), 4)
        self.assertIn(">35.1 h<", donut(self.page))  # agent time sits in the chart's centre
        self.assertIn(">≈2.3 h<", stats)  # the one rule saving
        self.assertIn(">2<", stats)  # one rule + one AI note
        self.assertIn(">41<", stats)
        self.assertIn("sessions, 118 subagents", stats)

    def test_items_expand_with_short_name(self):
        v = synthetic_view()
        v["segments"][0]["items"][0]["short"] = "Browser tests (Playwright)"
        page = vr.render_html(v)
        p0 = page.split('id="rb-p0"', 1)[1].split("</section>", 1)[0]
        items = p0.split("<h3>What exactly</h3>", 1)[1]
        self.assertIn('<li class="rw"><details><summary class="sm"><span class="tt" title="Browser tests (Playwright)">', items)
        self.assertIn("<dt>Command</dt><dd><code>npx playwright test --project=chromium</code>", items)
        self.assertIn("<dt>Agent&#x27;s note</dt>", items)
        self.assertIn("<dt>Median run</dt><dd>25 min</dd>", items)

    def test_waiting_for_you_is_a_hatched_slice(self):
        v = synthetic_view()
        v["waiting"] = {"id": "waiting", "label": "Waiting for you", "seconds": 35.1 * H, "notes": [],
                        "parts": [{"id": "prompt", "label": "Your next message", "help": "h", "seconds": 30 * H, "count": 12,
                                   "p50_s": 900.0, "longest_s": 8 * H, "sessions": 5, "questions": 4, "questions_s": 2 * H, "top": []},
                                  {"id": "permission", "label": "Permission prompts", "help": "p", "seconds": 5.1 * H, "count": 40,
                                   "p50_s": 120.0, "longest_s": 900.0, "sessions": 3, "questions": 0, "questions_s": 0,
                                   "top": [{"what": "git push", "short": "Push with git", "count": 22, "seconds": 3 * H}]}],
                        "actions": [{"id": "wait-allow-rules", "title": "Add allow-rules for commands that keep asking",
                                     "saving_min": 245.0, "confidence": "medium", "evidence": "40 permission waits"}]}
        page = vr.render_html(v)
        svg = donut(page)
        self.assertEqual(svg.count('<path class="slice '), 6)
        self.assertIn('class="slice cw', svg)
        self.assertIn("url(#rb-hatch-w)", page)
        self.assertIn('data-ct="of 70.2 h in all"', svg)  # ring = agent time + waiting
        self.assertIn(">+ 35.1 h waiting<", svg)
        self.assertIn('<span class="lh">35.1 h</span><span class="lsv">≈4.1 h</span>', page)  # the waiting row: 245 min of yours
        pw = page.split('id="rb-p5"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Add allow-rules for commands that keep asking", pw)
        self.assertIn("<h3>What you can do</h3>", pw)  # a tip without 'for' is one for you
        self.assertNotIn("<h3>What the agent can do</h3>", pw)
        self.assertIn("<dt>Ended on a question</dt><dd>4 of 12 stops, 2.0 h</dd>", pw)
        self.assertIn("Push with git ×22", pw)
        self.assertIn("Not agent time.", pw)
        stats = re.search(r'<ul class="stats".*?</ul>', page, re.S).group(0)
        self.assertIn("waited 35.1 h for you (the striped slice)", page)
        self.assertIn(">≈6.4 h<", stats)  # 140 min of agent time + 245 min of waiting: the 'Can save' column added up
        self.assertIn('All slices</span><span class="lh">70.2 h</span><span class="lsv">≈6.4 h</span>', page)
        self.assertIn(">2<", stats)

    def test_notes_reach_the_waiting_slice(self):
        v = synthetic_view()
        v["waiting"] = {"id": "waiting", "label": "Waiting for you", "seconds": H, "parts": [], "actions": [], "notes": []}
        out = vr.merge_notes(v, {"segments": {"waiting": [{"title": "Answer in one batch", "detail": "", "evidence": []}]}})
        self.assertEqual(out["waiting"]["notes"][0]["title"], "Answer in one batch")
        self.assertNotIn("notes_unmatched", out)

    def test_footer_links(self):
        foot = self.page.split('<footer class="foot">', 1)[1]
        self.assertIn("Star on GitHub", foot)
        self.assertIn('href="https://github.com/jindraregal/analyse-bottleneck"', foot)
        self.assertIn('href="https://jindraregal.com"', foot)
        self.assertIn("analyse-bottleneck", foot)

    def test_centre_shows_selected_slice(self):
        self.assertIn('data-cl="Tests" data-ct="≈2.3 h to save"', self.page)  # what it can save, not its share
        self.assertIn('data-cl="Other" data-ct="no saving estimate"', self.page)
        self.assertIn('p.getAttribute("data-ct") || svg.getAttribute("data-ct")', self.page)
        self.assertIn('data-cv="9.8 h"', self.page)
        self.assertRegex(donut(self.page), r'class="ctr-l"[^>]*>Agent time<')
        page = vr.render_html(self.view)
        self.assertNotIn('data-cl0=""', page)

    def test_legend_shows_saving_not_share(self):
        leg = re.search(r'<ul class="leg".*?</ul>', self.page, re.S).group(0)
        self.assertNotIn("%", leg)
        self.assertIn('<li class="lhd" aria-hidden="true"><span></span><span></span><span>Time</span><span>Can save</span></li>', leg)
        self.assertIn('aria-label="Tests: 9.8 h, about 2.3 h to save"', leg)
        self.assertIn('<span class="ll">Tests</span><span class="lh">9.8 h</span><span class="lsv">≈2.3 h</span>', leg)
        self.assertIn('<span class="ll">Other</span><span class="lh">3.5 h</span><span class="lsv none">–</span>', leg)  # no rule estimate
        self.assertIn('All slices</span><span class="lh">35.1 h</span><span class="lsv">≈2.3 h</span>', leg)  # = the header stat
        p0 = self.page.split('id="rb-p0"', 1)[1].split("</section>", 1)[0]
        self.assertIn('<span class="pv">9.8 h <span class="pp">· ≈2.3 h to save</span></span>', p0)
        p4 = self.page.split('id="rb-p4"', 1)[1].split("</section>", 1)[0]
        self.assertIn('<span class="pv">3.5 h</span>', p4)

    def test_waiting_tips_for_you_and_for_the_agent(self):
        v = synthetic_view()
        tip = lambda i, who, sv: {"id": i, "for": who, "title": "Tip " + i, "saving_min": sv, "confidence": "medium", "evidence": "e"}
        v["waiting"] = {"id": "waiting", "label": "Waiting for you", "seconds": 10 * H, "notes": [],
                        "parts": [{"id": "prompt", "label": "Your next message", "help": "h", "seconds": 10 * H, "count": 6,
                                   "p50_s": 900.0, "longest_s": 4 * H, "sessions": 2, "questions": 0, "questions_s": 0, "top": []}],
                        "actions": [tip("batch", "agent", 30.0), tip("push", "you", None)]}
        page = vr.render_html(v)
        pw = page.split('id="rb-p5"', 1)[1].split("</section>", 1)[0]
        self.assertLess(pw.index("<h3>What you can do</h3>"), pw.index("Tip push"))
        self.assertLess(pw.index("Tip push"), pw.index("<h3>What the agent can do</h3>"))
        self.assertLess(pw.index("<h3>What the agent can do</h3>"), pw.index("Tip batch"))
        segs = sorted(v["segments"], key=lambda s: -s["seconds"])
        shown, more = vr.top_todos(segs, v["waiting"], [], vr.colour_classes(segs), limit=1)
        self.assertEqual(len(shown), 1)
        self.assertIn("Tip push", shown[0])  # one tip for you always reaches 'Do this first'
        self.assertIn("<dt>For</dt><dd>you</dd>", shown[0])
        self.assertGreaterEqual(more, 2)

    def test_do_this_before_what_exactly_and_basis_collapsed(self):
        p0 = self.page.split('id="rb-p0"', 1)[1].split("</section>", 1)[0]
        self.assertLess(p0.index("<h3>Do this</h3>"), p0.index("<h3>What exactly</h3>"))
        self.assertIn('<details class="basis">', self.page)
        self.assertNotIn('<details class="basis" open', self.page)

    def test_size(self):
        self.assertLess(len(self.page.encode("utf-8")), 300 * 1024)

    def test_merge_notes_is_pure_and_idempotent(self):
        once = vr.merge_notes(self.view, synthetic_notes())
        twice = vr.merge_notes(once, synthetic_notes())
        self.assertEqual(self.view["segments"][2]["notes"], [])
        net = [s for s in twice["segments"] if s["id"] == "network"][0]
        self.assertEqual(len(net["notes"]), 1)
        self.assertEqual(sum(1 for b in twice["basis"] if b["kind"] == "llm"), 1)

    def test_colours_unknown_ids_take_free_slots(self):
        segs = [{"id": "s%d" % i, "seconds": 10 - i} for i in range(7)] + [{"id": "tests", "seconds": 1}]
        cls = vr.colour_classes(segs)
        self.assertEqual(cls[7], "c1")  # tests keeps its own colour, whatever its rank
        self.assertEqual([cls[i] for i in range(7)], ["c2", "c3", "c4", "c5", "c6", "c7", "c8"])

    def test_degenerate_views(self):
        page = vr.render_html({"segments": []})
        self.assertIn("No agent time in this window.", page)
        one = vr.render_html({"segments": [{"id": "tests", "label": "Tests", "seconds": 60}], "totals": {"agent_s": 60}})
        self.assertEqual(donut(one).count("<path"), 1)


if __name__ == "__main__":
    unittest.main()
