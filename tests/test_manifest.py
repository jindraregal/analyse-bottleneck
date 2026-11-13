"""Plugin and marketplace manifests: names, version, and the install command in the README."""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import rbn  # noqa: E402


def load(name):
    with open(os.path.join(ROOT, ".claude-plugin", name), encoding="utf-8") as f:
        return json.load(f)


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.plugin = load("plugin.json")
        self.market = load("marketplace.json")
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            self.readme = f.read()

    def test_marketplace_lists_this_plugin(self):
        self.assertEqual(self.market["name"], "jindraregal")
        self.assertEqual([p["name"] for p in self.market["plugins"]], [self.plugin["name"]])
        self.assertEqual(self.market["plugins"][0]["source"], "./")

    def test_version_matches_scripts(self):
        self.assertEqual(self.plugin["version"], rbn.__version__)
        self.assertEqual(self.market["plugins"][0]["version"], self.plugin["version"])

    def test_rules_avoid_phrases_that_catalogue_scanners_grade_as_commands(self):
        # skillsdirectory.com grades the bundled rules/*.json as if they were instructions; these phrases
        # cost the listing an F in 0.5.7 (shell pipe, runtime install, rm -rf, path traversal, download + run)
        bad = [r"\|\s*(?:ba|z)?sh\b", r"\b(?:pnpm|pip3?)\s+i(?:nstall)?\b", r"\bnpm\s+install\b",
               r"\brm\s+-rf\b", r"\.\.[\\\\/]", r"\bRUN\s+pnpm\s+fetch\b", r"/root/"]
        rules = os.path.join(ROOT, "rules")
        for name in sorted(os.listdir(rules)):
            with open(os.path.join(rules, name), encoding="utf-8") as f:
                text = f.read()
            for pat in bad:
                m = re.search(pat, text)
                self.assertIsNone(m, "%s: %r" % (name, m and text[max(0, m.start() - 40):m.end() + 40]))

    def test_readme_install_uses_current_names(self):
        want = "%s@%s" % (self.plugin["name"], self.market["name"])
        installs = re.findall(r"plugin install (\S+)", self.readme)
        self.assertGreaterEqual(len(installs), 2)
        self.assertEqual(set(installs), {want})
        self.assertIn("marketplace add jindraregal/analyse-bottleneck", self.readme)


if __name__ == "__main__":
    unittest.main()
