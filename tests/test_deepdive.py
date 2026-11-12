"""Deep dives (deepdive.py, rules/deepdive.json): trigger, facts, checks on disk, tips, brief, page. Synthetic data only."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

from fixtures import Tx  # noqa: E402
from test_drill import RS, Base  # noqa: E402
from rbn import deepdive, view_render as vr  # noqa: E402

SPEC = next(d for d in RS.deepdive if d["id"] == "deploys")
DOCKERFILE = """FROM node:22-slim AS build
WORKDIR /app
COPY . .
RUN pnpm install --frozen-lockfile
RUN pnpm build
"""


class TestSpec(unittest.TestCase):
    def setUp(self):
        self.spec = deepdive._Spec(SPEC, RS)

    def test_runs_and_status_checks(self):
        run = self.spec.is_run
        for cmd in ("./deploy.sh", "bash scripts/deploy.sh prod", "app-deploy --wait", "pnpm deploy",
                    "coolify deploy uuid abc", "gcloud builds submit --config cloudbuild.yaml .",
                    "curl -X POST https://coolify.example.com/api/v1/deploy?uuid=x"):
            self.assertTrue(run(cmd), cmd)
        for cmd in ("curl -s https://coolify.example.com/api/v1/deployments/abc", "git commit -m 'deploy fix'", "gh run watch 123"):
            self.assertFalse(run(cmd), cmd)

    def test_reading_about_deploys_is_not_a_deploy(self):
        m = self.spec.matches
        self.assertTrue(m("curl -s https://coolify.example/api/v1/deployments/abc"))
        self.assertTrue(m("gh run watch 123 --exit-status"))
        self.assertFalse(m("grep -rn deploy docs/"))
        self.assertFalse(m("cat deploy.md"))
        self.assertFalse(m("git log --grep deploy"))
        self.assertTrue(m("sleep 60", waited_for="./deploy.sh"))  # a wait on a deploy counts even when the command does not say so
        self.assertFalse(m("npx tsc --noEmit"))

    def test_conditions_and_fill(self):
        facts = {"checks_per_deploy": 4.0, "back_to_back": 1, "episodes": 3}
        self.assertTrue(deepdive._cond("checks_per_deploy >= 3", facts))
        self.assertFalse(deepdive._cond("back_to_back >= 2", facts))
        self.assertFalse(deepdive._cond("nonsense", facts))
        self.assertFalse(deepdive._cond("missing >= 1", facts))
        self.assertEqual(deepdive._fill("{episodes} deploys, {unknown}", facts), "3 deploys, {unknown}")


class TestDeepDive(Base):
    def make(self, deploys=4, deploy_s=600, checks=4, gap_s=1100):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        os.makedirs(os.path.join(repo, "app"), exist_ok=True)
        with open(os.path.join(repo, "app", "Dockerfile"), "w") as f:
            f.write(DOCKERFILE)
        tx = Tx("dd", cwd=repo)
        tx.human(0)
        t = 10
        tx.tool(t, t + 1500, "Bash", {"command": "npx tsc --noEmit"})
        t += 1510
        for _ in range(deploys):
            tx.tool(t, t + deploy_s, "Bash", {"command": "./deploy.sh"})
            t += deploy_s + 5
            for _ in range(checks):
                tx.tool(t, t + 5, "Bash", {"command": "curl -s https://coolify.example/api/v1/deployments/abc"})
                t += 30
            t += gap_s
            tx.human(t)
            t += 5
        tx.text(t)
        tx.write(os.path.join(self.proj, "dd.jsonl"))
        return repo

    def test_triggers_with_facts_checks_and_tips(self):
        self.make()
        an, rep, (v, check) = self.view()
        dds = v.get("deep_dives") or []
        self.assertEqual([d["id"] for d in dds], ["deploys"])
        dd = dds[0]
        self.assertGreaterEqual(dd["seconds"], 1800)
        self.assertIn("of agent time", dd["trigger"])
        facts = {f["label"]: f["value"] for f in dd["facts"]}
        self.assertEqual(facts["Deploys"], "4")
        self.assertEqual(facts["Status checks"], "16 calls, 4.0 per deploy")
        self.assertIn("3 deploys started within 30 min", facts["Back to back"])
        self.assertIn("Coolify", facts["Platform"])
        self.assertIn("own deploy script", facts["Platform"])
        self.assertTrue(all(re.match(r"deploys\.f\d+$", f["id"]) for f in dd["facts"]))
        checks = {c["id"]: c for c in dd["checks"]}
        self.assertFalse(checks["docker-cache-mount"]["ok"])
        self.assertFalse(checks["docker-copy-order"]["ok"])
        self.assertIn("app/Dockerfile:3", checks["docker-copy-order"]["text"])
        self.assertFalse(checks["dockerignore"]["ok"])
        self.assertNotIn("cloudbuild-cache", checks)  # no Cloud Build in these calls
        ids = [a["id"] for a in dd["actions"]]
        self.assertIn("dd-one-blocking-wait", ids)
        self.assertNotIn("dd-batch", ids)  # the Deploys slice already says 'deploy once per batch' with an estimate
        self.assertIn("deploy-batch", [a["id"] for s in v["segments"] for a in s["actions"]])
        self.assertIn("dd-docker-copy-order", ids)
        self.assertLessEqual(len(ids), 5)
        self.assertTrue(any(b["what"].startswith("Deep dive") for b in v["basis"]))

    def test_cache_mount_check_skipped_on_cloud_build(self):
        repo = self.make()
        os.makedirs(os.path.join(repo, "infra"), exist_ok=True)
        with open(os.path.join(repo, "infra", "cloudbuild.yaml"), "w") as f:
            f.write("steps:\n- name: gcr.io/cloud-builders/docker\n  args: [build, -t, img, .]\n")
        spec = deepdive._Spec(SPEC, RS)
        ids = {c["id"]: c["ok"] for c in deepdive.run_checks(spec, [repo], ["cloudbuild"])}
        self.assertNotIn("docker-cache-mount", ids)  # a fresh worker has no cache to mount
        self.assertFalse(ids["cloudbuild-cache"])
        self.assertIn("docker-cache-mount", {c["id"] for c in deepdive.run_checks(spec, [repo], ["coolify"])})

    def test_fixed_dockerfile_is_ok(self):
        repo = self.make()
        with open(os.path.join(repo, "app", "Dockerfile"), "w") as f:
            f.write("FROM node:22\nCOPY package.json pnpm-lock.yaml ./\n"
                    "RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile\nCOPY . .\n")
        with open(os.path.join(repo, ".dockerignore"), "w") as f:
            f.write("node_modules\n")
        an, rep, (v, check) = self.view()
        checks = {c["id"]: c["ok"] for c in v["deep_dives"][0]["checks"]}
        self.assertEqual(checks, {"docker-cache-mount": True, "docker-copy-order": True, "dockerignore": True})

    def test_copy_order_with_cache_mount_is_minor(self):
        repo = self.make()
        with open(os.path.join(repo, "app", "Dockerfile"), "w") as f:
            f.write("FROM node:22\nCOPY . .\nRUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile\n")
        with open(os.path.join(repo, ".dockerignore"), "w") as f:
            f.write("node_modules\n")
        an, rep, (v, check) = self.view()
        dd = v["deep_dives"][0]
        checks = {c["id"]: c for c in dd["checks"]}
        self.assertFalse(checks["docker-copy-order"]["ok"])
        self.assertTrue(checks["docker-copy-order"].get("minor"))
        self.assertIn("app/Dockerfile:2", checks["docker-copy-order"]["text"])
        self.assertIn("only relinks", checks["docker-copy-order"]["text"])
        self.assertNotIn("dd-docker-copy-order", [a["id"] for a in dd["actions"]])  # a small gain is listed, not advised
        self.assertIn("- minor: ", deepdive.render_brief(dd, RS, v))
        page = vr.render_html(v)
        self.assertIn("<dt>Minor</dt>", page)
        self.assertIn("1 minor", page)
        self.assertNotIn("all ok", page.split("Deploy files on disk", 1)[1][:400])

    def test_measured_share_rescales_the_slice_rule(self):
        from rbn import view
        self.assertEqual(deepdive._share("back_to_back / episodes", {"back_to_back": 2, "episodes": 10}), (2, 10))
        self.assertIsNone(deepdive._share("back_to_back / episodes", {"back_to_back": 2, "episodes": 0}))
        self.assertIsNone(deepdive._share("nonsense", {}))

        def run(n, of):
            segs = [{"id": "deploys", "actions": [{"id": "deploy-batch", "saving_min": 100.0, "evidence": "e", "arithmetic": "a"}]}]
            dds = [{"actions": [{"id": "dd-batch", "same_as": "deploy-batch", "same_as_share": [n, of, "%d of %d deploys" % (n, of)]}]}]
            view.rescale_same_as(segs, dds, RS)
            return segs[0]["actions"][0]
        a = run(2, 10)  # rule: half of the slice (100 min of 200); measured: 2 of 10 deploys -> 40 min
        self.assertEqual(a["saving_min"], 40.0)
        self.assertIn("x 2/10", a["arithmetic"])
        self.assertIn("2 of 10 deploys", a["evidence"])
        self.assertEqual(run(8, 10)["saving_min"], 100.0)  # never raised above the rule's own estimate

    def test_small_deploy_share_does_not_trigger(self):
        self.make(deploys=1, deploy_s=120, checks=1)
        an, rep, (v, check) = self.view()
        self.assertNotIn("deep_dives", v)

    def test_brief_and_page(self):
        self.make()
        an, rep, (v, check) = self.view()
        dd = v["deep_dives"][0]
        brief = deepdive.render_brief(dd, RS, v)
        self.assertIn("You are a subagent started by the analyse-bottleneck skill", brief)
        self.assertIn("- deploys.f1 Agent time:", brief)
        self.assertIn("NOT OK:", brief)
        self.assertIn('"deepdive:deploys"', brief)
        self.assertNotIn("{facts}", brief)
        page = vr.render_html(v)
        pall = page.split('id="rb-pall"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Deep dive: deploy waits", pall)
        self.assertIn("AI deep dive not run yet", pall)
        self.assertIn("Wait on each deploy with one blocking call", pall)  # the deep dive's tips also reach 'Do this first'
        i = [s["id"] for s in sorted(v["segments"], key=lambda s: -s["seconds"])].index(dd["segments"][0])
        panel = page.split('id="rb-p%d"' % i, 1)[1].split("</section>", 1)[0]
        self.assertIn("Deep dive: deploy waits", panel)
        notes = {"model": "m", "segments": {"deepdive:deploys": [{"title": "Cache the pnpm store between builds",
                                                                   "detail": "d", "evidence": ["deploys.f3: 4"]}]}}
        page = vr.render_html(v, notes)
        pall = page.split('id="rb-pall"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Cache the pnpm store between builds", pall)
        self.assertNotIn("AI deep dive not run yet", pall)
        self.assertNotIn("notes_unmatched", vr.merge_notes(v, notes))
        stats = re.search(r'<ul class="stats".*?</ul>', page, re.S).group(0)
        rules = sum(len(s.get("actions") or []) for s in v["segments"]) + len((v.get("waiting") or {}).get("actions") or [])
        rules += len(dd["actions"])
        self.assertIn(">%d<" % (rules + 1), stats)  # deep-dive tips and its AI note count as recommendations


if __name__ == "__main__":
    unittest.main()
