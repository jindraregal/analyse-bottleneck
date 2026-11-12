import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from rbn.rules import RuleSet  # noqa: E402
from rbn.describe import describe, dedupe, shorten_desc  # noqa: E402
from rbn.view import build_waiting  # noqa: E402

RS = RuleSet()


class TestDescribe(unittest.TestCase):
    def name(self, what, label="", seg=""):
        return describe(RS, what, label or what, "", seg)[0]

    def test_rules_name_commands(self):
        self.assertEqual(self.name("npx vitest run"), "Unit tests (vitest)")
        self.assertEqual(self.name("npx playwright test"), "Browser tests (Playwright)")
        self.assertEqual(self.name("pnpm test"), "Whole test suite")
        self.assertEqual(self.name("pnpm test site-"), "Tests: site")
        self.assertEqual(self.name("git add"), "Commit with git")
        self.assertEqual(self.name("ssh buildbox"), "SSH to buildbox")
        self.assertEqual(self.name("curl https://coolify.example.com/api/v1/deployments", seg="poll"), "Wait for Coolify deploy")
        self.assertEqual(self.name("curl https://coolify.example.com/api/v1/deployments", seg="network"), "Check Coolify deploys")
        self.assertEqual(self.name("Acme_Corp call_tool"), "Acme Corp: call tool")

    def test_labels_and_agent_descriptions(self):
        self.assertEqual(self.name("claude-opus-5-5", "claude-opus-5-5 in main session"), "Opus, main session")
        self.assertEqual(self.name("ffmpeg", "Media encode, mostly ffmpeg"), "Media encode")
        self.assertEqual(self.name("until [ -f /tmp/vt/DONE ] (+9 other polls)", "10 polls waiting for npx vitest run"),
                         "Wait for unit tests (vitest)")
        # no command rule: the agent's own description, first clause, at most 5 words
        self.assertEqual(self.name("curl stg.example.com/api", "Wait for match build, then redeploy with new domain"), "Wait for match build")
        self.assertEqual(self.name("python3 x.py", "Build the film with a higher voice cap"), "Build the film")
        # nothing else: fallback rules, then the first two words
        self.assertEqual(self.name("bash …/scratchpad/build.sh"), "Scratch script build.sh")
        self.assertEqual(self.name("frobnicate --all now"), "Frobnicate --all")

    def test_always_two_to_five_words_and_stable(self):
        for d in ("Poll until stg.example.com approvals endpoint is live", "Run GT1 confirmatory phase in background",
                  "Check progress and run gradient budget for B6 and I05", "x"):
            s = shorten_desc(d)
            self.assertLessEqual(len(s.split()), 5, s)
            self.assertLessEqual(len(s), 38, s)
            self.assertEqual(s, shorten_desc(d))

    def test_dedupe_in_one_slice(self):
        items = [{"short": "Typecheck (tsc)", "what": "pnpm tsc", "label": "pnpm tsc"},
                 {"short": "Typecheck (tsc)", "what": "npx tsc", "label": "npx tsc"},
                 {"short": "Lint", "what": "pnpm lint", "label": "Typecheck auth package and match app"},
                 {"short": "Lint", "what": "npx eslint", "label": "npx eslint"}]
        dedupe(RS, items)
        self.assertEqual([it["short"] for it in items], ["Typecheck (tsc)", "Typecheck (tsc) · npx", "Lint", "Lint · npx"])


def analysis(slices, requests=(), tools=()):
    S = {"requests": {r["id"]: r for r in requests}, "tools": {t["id"]: t for t in tools}, "tool_order": [t["id"] for t in tools]}
    return {"main": S, "tl": {"slices": slices}, "group": {"session_id": "s1"}}


class TestWaiting(unittest.TestCase):
    def test_parts_questions_and_rules(self):
        reqs = [{"id": "r%d" % i, "end": 1000.0 * i, "ends_q": i <= 3} for i in range(1, 6)]
        slices = [(1000.0 * i, 1000.0 * i + 900, "human_wait:prompt") for i in range(1, 6)]
        tools = [{"id": "t%d" % i, "name": "Bash", "cmd": 'GATE_REASON="check" ssh buildbox uptime', "t0": 10000.0 + 100 * i,
                  "t1": 10060.0 + 100 * i, "bg_end": None} for i in range(4)]
        slices += [(10000.0 + 100 * i, 10060.0 + 100 * i, "human_wait:approval") for i in range(4)]
        slices.append((20000.0, 29000.0, "human_wait:away"))
        w = build_waiting([analysis(slices, reqs, tools)], RS)
        self.assertEqual(w["seconds"], 5 * 900 + 4 * 60)  # a parked session is not drawn
        self.assertEqual(w["parked_s"], 9000.0)
        self.assertNotIn("away", [p["id"] for p in w["parts"]])
        prompt = w["parts"][0]
        self.assertEqual((prompt["id"], prompt["count"], prompt["questions"], prompt["p50_s"]), ("prompt", 5, 3, 900.0))
        appr = [p for p in w["parts"] if p["id"] == "approval"][0]
        self.assertEqual(appr["top"][0]["what"], "ssh buildbox")  # the gate variable and its reason are not shown
        self.assertEqual(appr["top"][0]["count"], 4)
        ids = [a["id"] for a in w["actions"]]
        self.assertIn("wait-end-with-result", ids)
        self.assertIn("wait-batch-approvals", ids)
        q = [a for a in w["actions"] if a["id"] == "wait-end-with-result"][0]
        self.assertIn("3 of 5 stops ended with a question", q["why"])
        by = {a["id"]: a for a in w["actions"]}
        self.assertEqual({i: a["for"] for i, a in by.items()},
                         {"wait-end-with-result": "you", "wait-push-on-stop": "you", "wait-queue-next": "you",
                          "wait-batch-approvals": "agent"})
        self.assertEqual(q["saving_min"], 45.0)  # the 3 waits after a question, 15 min each
        self.assertIn("45 min after 3 stops that ended with a question", q["arithmetic"])
        appr_tip = by["wait-batch-approvals"]
        self.assertEqual(appr_tip["saving_min"], 3.0)  # 4 approvals in one session: 3 extra waits x median 60 s
        self.assertIn("time: (4 waits - 1 session) x median 1 min = 3 min; assumption", appr_tip["arithmetic"])
        self.assertIsNone(by["wait-push-on-stop"]["saving_min"])  # no honest estimate
        self.assertIn("code.claude.com/docs/en/hooks", by["wait-push-on-stop"]["ref"])

    def test_no_waiting(self):
        w = build_waiting([analysis([(0.0, 50.0, "test")])], RS)
        self.assertEqual((w["seconds"], w["parts"], w["actions"]), (0, [], []))


if __name__ == "__main__":
    unittest.main()
