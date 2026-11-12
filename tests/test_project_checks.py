"""Rules that look at the project on disk (detector.skip_if_project): a finding is not reported when the project
already does what the rule advises. Synthetic projects in a temp directory only."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

from fixtures import Tx  # noqa: E402
from test_drill import RS, Base  # noqa: E402
from rbn import repo  # noqa: E402

TSC = "ts-tsc-not-incremental"


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    return path


class Proj(Base):
    def setUp(self):
        super().setUp()
        repo.reset()
        self.root = os.path.join(self.tmp, "code")
        os.makedirs(os.path.join(self.root, ".git"))

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def run_cmds(self, cmds, cwd, sid="s1", sec=30):
        tx = Tx(sid, cwd=cwd)
        tx.human(0)
        t = 10
        for c in cmds:
            tx.tool(t, t + sec, "Bash", {"command": c})
            t += sec + 5
        tx.text(t)
        tx.write(os.path.join(self.proj, sid + ".jsonl"))

    def finding(self, rule_id=TSC):
        _, rep, _ = self.view()
        f = next((c for c in rep["ranked"] if c["id"] == rule_id), None)
        chk = next((k for k in rep["meta"]["project_checks"] if k["id"] == rule_id), None)
        return f, chk


class TestTsconfigResolution(Proj):
    def test_jsonc_comments_trailing_commas_and_slashes_in_strings(self):
        f = put(self.p("tsconfig.json"), '''{
  // line comment
  "compilerOptions": {
    /* block
       comment */
    "paths": {"@/*": ["./src/*"], "x": ["http://a//b"]},
    "incremental": true,
  },
}''')
        eff = repo.ts_effective(f)
        self.assertIs(eff["incremental"], True)
        self.assertEqual(eff["paths"]["@/*"], ["./src/*"])

    def test_extends_relative_without_suffix_and_child_wins(self):
        put(self.p("tsconfig.base.json"), {"compilerOptions": {"incremental": True, "strict": True}})
        a = put(self.p("apps", "a", "tsconfig.json"), {"extends": "../../tsconfig.base"})
        b = put(self.p("apps", "b", "tsconfig.json"), {"extends": "../../tsconfig.base.json", "compilerOptions": {"incremental": False}})
        self.assertIs(repo.ts_effective(a)["incremental"], True)
        self.assertNotIn("incremental", repo.ts_effective(a)["_own"])
        self.assertIs(repo.ts_effective(b)["incremental"], False)
        self.assertIs(repo.ts_effective(b)["strict"], True)

    def test_extends_list_package_and_grandparent(self):
        # a package in a hoisted node_modules, which itself extends a sibling file
        put(self.p("node_modules", "@acme", "tsconfig", "strict.json"), {"compilerOptions": {"composite": True}})
        put(self.p("node_modules", "@acme", "tsconfig", "base.json"), {"extends": "./strict.json", "compilerOptions": {"strict": True}})
        put(self.p("node_modules", "plain-config", "package.json"), {"name": "plain-config", "tsconfig": "./cfg/ts.json"})
        put(self.p("node_modules", "plain-config", "cfg", "ts.json"), {"compilerOptions": {"composite": False, "target": "ES2022"}})
        a = put(self.p("packages", "a", "tsconfig.json"), {"extends": "@acme/tsconfig/base.json"})
        b = put(self.p("packages", "b", "tsconfig.json"), {"extends": ["@acme/tsconfig/base", "plain-config"]})
        self.assertIs(repo.ts_effective(a)["composite"], True)
        eff = repo.ts_effective(b)
        self.assertIs(eff["composite"], False)  # the later entry of the list wins
        self.assertEqual(eff["target"], "ES2022")
        self.assertIs(eff["strict"], True)

    def test_extends_cycle_and_missing_parent_do_not_break(self):
        a = put(self.p("a.json"), {"extends": "./b.json", "compilerOptions": {"incremental": True}})
        put(self.p("b.json"), {"extends": ["./a.json", "./nope.json", "missing-package/tsconfig.json"]})
        self.assertIs(repo.ts_effective(a)["incremental"], True)
        self.assertEqual(repo.ts_effective(self.p("nope.json")), {})

    def test_event_dir_follows_cd_filter_and_dir_options(self):
        put(self.p("apps", "match", "package.json"), {"name": "@demo/match"})
        put(self.p("node_modules", "@demo", "ghost", "package.json"), {"name": "@demo/ghost"})
        rx = next(r for r in RS.stack if r["id"] == TSC)["_rx"]
        d = lambda cmd: repo.event_dir(cmd, rx, self.root)[0]  # noqa: E731
        self.assertEqual(d("npx tsc --noEmit"), self.root)
        self.assertEqual(d("cd apps/match && npx tsc --noEmit"), self.p("apps", "match"))
        self.assertEqual(d("git status; cd apps && cd match; npx tsc"), self.p("apps", "match"))
        self.assertEqual(d("(pnpm --filter @demo/match exec tsc --noEmit -p ."), self.p("apps", "match"))
        self.assertEqual(d("pnpm -F '@demo/match...' exec tsc"), self.p("apps", "match"))
        self.assertEqual(d("pnpm -C apps/match exec tsc"), self.p("apps", "match"))
        self.assertEqual(d("yarn workspace @demo/match tsc"), self.p("apps", "match"))
        self.assertEqual(d("pnpm --filter @demo/ghost exec tsc"), self.root)  # unknown package: the directory we know
        self.assertEqual(d("npx tsc -w -p apps/match"), self.root)  # options after the tool are the tool's own
        self.assertEqual(repo.event_dir("npx tsc", rx, self.p("gone"))[0], None)
        self.assertEqual(repo.event_dir("npx tsc", rx, None)[0], None)

    def test_event_dir_of_a_deleted_worktree_or_temp_clone(self):
        put(self.p("apps", "match", "package.json"), {"name": "@demo/match"})
        rx = next(r for r in RS.stack if r["id"] == TSC)["_rx"]
        wt = self.p(".claude", "worktrees", "agent-1")  # never created: the worktree is gone
        self.assertEqual(repo.event_dir("npx tsc", rx, os.path.join(wt, "apps", "match")), (self.p("apps", "match"), "", True))
        self.assertEqual(repo.event_dir("pnpm --filter @demo/match exec tsc -p .", rx, wt), (self.p("apps", "match"), " -p .", True))
        self.assertEqual(repo.event_dir("cd %s/apps/match && npx tsc" % wt, rx, self.root)[::2], (self.p("apps", "match"), True))
        gone = os.path.join(self.tmp, "tmp.clone")
        self.assertEqual(repo.event_dir("cd %s && pnpm --filter @demo/match exec tsc" % gone, rx, self.root)[::2], (self.p("apps", "match"), True))
        self.assertEqual(repo.event_dir("npx tsc", rx, self.root)[2], False)
        self.assertEqual(repo.event_dir("npx tsc", rx, os.path.join(self.tmp, "x", ".claude", "worktrees", "a"))[0], None)


class TestTscNotIncremental(Proj):
    def monorepo(self):
        put(self.p("tsconfig.json"), '{\n  // shared\n  "compilerOptions": {"strict": true, "noEmit": true, "incremental": true,},\n}')
        put(self.p("apps", "match", "package.json"), {"name": "@demo/match"})
        put(self.p("apps", "match", "tsconfig.json"), {"extends": "../../tsconfig.json", "compilerOptions": {"jsx": "preserve"}})

    def test_reported_when_project_is_not_incremental(self):
        put(self.p("tsconfig.json"), {"compilerOptions": {"strict": True}})
        self.run_cmds(["npx tsc --noEmit"] * 3, self.root)
        f, chk = self.finding()
        self.assertIsNotNone(f)
        self.assertIn("3 runs", f["evidence"])
        self.assertIsNone(chk)

    def test_not_reported_when_incremental_comes_through_extends(self):
        self.monorepo()
        self.run_cmds(["(pnpm --filter @demo/match exec tsc --noEmit -p .", "cd apps/match && npx tsc --noEmit", "npx tsc --noEmit -p apps/match"], self.root)
        f, chk = self.finding()
        self.assertIsNone(f)
        self.assertEqual(chk["events"], 3)
        self.assertEqual(chk["seconds"], 90.0)
        self.assertEqual(chk["reasons"], {'tsconfig sets "incremental" (through extends)': 3})

    def test_not_reported_when_set_in_the_file_itself_or_cwd_is_the_package(self):
        self.monorepo()
        self.run_cmds(["npx tsc --noEmit"] * 3, self.root)
        self.run_cmds(["npx tsc --noEmit"] * 3, self.p("apps", "match"), sid="s2")
        f, chk = self.finding()
        self.assertIsNone(f)
        self.assertEqual(chk["reasons"], {'tsconfig sets "incremental"': 3, 'tsconfig sets "incremental" (through extends)': 3})

    def test_not_reported_when_extended_package_sets_composite(self):
        put(self.p("node_modules", "@acme", "tsconfig", "base.json"), {"compilerOptions": {"composite": True}})
        put(self.p("packages", "lib", "tsconfig.json"), {"extends": "@acme/tsconfig/base.json"})
        self.run_cmds(["npx tsc --noEmit"] * 3, self.p("packages", "lib"))
        f, chk = self.finding()
        self.assertIsNone(f)
        self.assertEqual(chk["reasons"], {'tsconfig sets "composite" (through extends)': 3})

    def test_not_reported_when_a_referenced_project_is_composite(self):
        put(self.p("tsconfig.base.json"), {"compilerOptions": {"composite": True}})
        put(self.p("packages", "core", "tsconfig.json"), {"extends": "../../tsconfig.base.json"})
        put(self.p("tsconfig.json"), {"files": [], "references": [{"path": "./packages/core"}]})
        self.run_cmds(["npx tsc --noEmit -p ."] * 3, self.root)
        f, chk = self.finding()
        self.assertIsNone(f)
        self.assertEqual(chk["reasons"], {'a referenced project sets "composite"': 3})

    def test_not_reported_when_a_tsbuildinfo_exists(self):
        put(self.p("a", "tsconfig.json"), {"compilerOptions": {"strict": True}})
        put(self.p("a", "tsconfig.tsbuildinfo"), "{}")
        put(self.p("b", "tsconfig.json"), {"compilerOptions": {"outDir": "./dist"}})
        put(self.p("b", "dist", "tsconfig.tsbuildinfo"), "{}")
        put(self.p("c", "tsconfig.json"), {"compilerOptions": {"tsBuildInfoFile": "./.cache/tsc.info"}})
        put(self.p("c", ".cache", "tsc.info"), "{}")
        self.run_cmds(["cd a && npx tsc --noEmit", "cd b && npx tsc", "npx tsc -p c"], self.root)
        f, chk = self.finding()
        self.assertIsNone(f)
        self.assertEqual(chk["reasons"], {"a *.tsbuildinfo file exists": 3})

    def test_not_reported_for_a_deleted_worktree_of_an_incremental_project(self):
        self.monorepo()
        wt = self.p(".claude", "worktrees", "agent-1")
        self.run_cmds(["(pnpm --filter @demo/match exec tsc --noEmit -p . > /tmp/x.log 2>&1; echo done)"] * 2, wt)
        self.run_cmds(["cd %s && time pnpm --filter @demo/match exec tsc --noEmit -p . 2>&1 | tail -20" % os.path.join(self.tmp, "tmp.clone")], self.root, sid="s2")
        f, chk = self.finding()
        self.assertIsNone(f)
        self.assertEqual(chk["reasons"], {'tsconfig sets "incremental" (through extends); call directory is gone, the project\'s checkout was checked': 3})

    def test_child_that_turns_incremental_off_is_still_reported(self):
        put(self.p("tsconfig.base.json"), {"compilerOptions": {"incremental": True}})
        put(self.p("tsconfig.json"), {"extends": "./tsconfig.base.json", "compilerOptions": {"incremental": False}})
        self.run_cmds(["npx tsc --noEmit"] * 3, self.root)
        f, chk = self.finding()
        self.assertIsNotNone(f)
        self.assertIsNone(chk)

    def test_only_the_calls_of_the_incremental_project_are_dropped(self):
        self.monorepo()
        put(self.p("tools", "tsconfig.json"), {"compilerOptions": {"strict": True}})
        self.run_cmds(["cd tools && npx tsc --noEmit"] * 3 + ["npx tsc --noEmit -p apps/match"] * 2, self.root)
        f, chk = self.finding()
        self.assertIn("3 runs, 1.5m measured", f["evidence"])
        self.assertEqual(chk["events"], 2)

    def test_reported_when_the_directory_is_unknown_or_the_call_names_files(self):
        self.monorepo()
        self.run_cmds(["npx tsc --noEmit"] * 3, os.path.join(self.tmp, "deleted-worktree"))
        self.run_cmds(["npx tsc --noEmit src/a.ts"] * 3, self.root, sid="s2")  # tsc ignores tsconfig when given files
        self.run_cmds(["npx tsc --noEmit -p missing"] * 3, self.root, sid="s3")
        f, chk = self.finding()
        self.assertIn("9 runs", f["evidence"])
        self.assertIsNone(chk)


class TestOtherProjectChecks(Proj):
    NPM = "js-npm-vs-pnpm-bun"
    TURBO = "next-turbopack-not-used"
    FMT = "format-by-hand"

    def test_npm_reported_from_command_text_only(self):
        self.run_cmds(["npm install"] * 3, self.root, sec=40)
        f, chk = self.finding(self.NPM)
        self.assertIn("3 runs", f["evidence"])
        self.assertIsNone(chk)  # the project's npm config is not read: it can hold registry tokens
        self.run_cmds(["npm ci --prefer-offline"] * 3, self.root, sid="s2", sec=40)
        f, chk = self.finding(self.NPM)
        self.assertIn("3 runs", f["evidence"])  # the flag on the command still drops those calls

    def test_formatter_by_hand_reported_with_its_source_unless_a_hook_runs_it(self):
        cmds = ["npx prettier --write src/a.ts", "pnpm exec eslint src --fix", "npx prettier --check .", "ruff format .", "pnpm format"]
        self.run_cmds(cmds, self.root, sec=3)
        f, chk = self.finding(self.FMT)
        self.assertIn("4 runs", f["evidence"])  # --check only verifies
        self.assertIn("code.claude.com/docs/en/best-practices", f["ref"])
        self.assertIn("PostToolUse", f["fix"])
        repo.reset()
        put(self.p(".claude", "settings.json"), {"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": []}]}})
        f, chk = self.finding(self.FMT)
        self.assertIsNone(f)
        self.assertEqual(chk["reasons"], {".claude/settings.json already sets it": 4})

    def next_runs(self, version, cmd="npx next build"):
        put(self.p("package.json"), {"name": "root", "devDependencies": {"typescript": "^5"}})
        put(self.p("apps", "web", "package.json"), {"name": "web", "dependencies": {"next": version, "react": "19.0.0"}})
        self.run_cmds(["cd apps/web && " + cmd] * 4, self.root, sec=60)
        return self.finding(self.TURBO)

    def test_turbopack_not_reported_where_it_is_the_default(self):
        for version in ("16.0.1", "^16.1.0", "~17.0", ">=16", "16"):
            repo.reset()
            f, chk = self.next_runs(version)
            self.assertIsNone(f, version)
            self.assertEqual(chk["events"], 4, version)
            self.assertEqual(list(chk["reasons"]), ["package.json has next " + version])

    def test_turbopack_still_reported_on_older_or_unknown_versions(self):
        for version in ("15.5.4", "^14.2.0", "canary", "latest", "catalog:", "^15 || ^16"):
            repo.reset()
            f, chk = self.next_runs(version)
            self.assertIsNotNone(f, version)
            self.assertIsNone(chk, version)

    def test_skipped_seconds_use_the_time_the_rule_would_have_counted(self):
        put(self.p("tsconfig.json"), {"compilerOptions": {"incremental": True}})
        # the wall time of a compound command whose class is not the typecheck is credited as at most 2 s
        self.run_cmds(["npx tsc --noEmit"] * 3 + ["sleep 100; npx tsc --noEmit"], self.root)
        _, chk = self.finding()
        self.assertEqual(chk["events"], 4)
        self.assertLessEqual(chk["seconds"], 94.0)

    def test_every_project_check_in_the_rules_is_known(self):
        known = {name for name, _ in repo._CHECKS}
        with_checks = [r for r in RS.stack if (r.get("detector") or {}).get("skip_if_project")]
        self.assertEqual({r["id"] for r in with_checks}, {TSC, self.TURBO, self.FMT})
        for r in with_checks:
            self.assertTrue(set(r["detector"]["skip_if_project"]) <= known, r["id"])


if __name__ == "__main__":
    unittest.main()
