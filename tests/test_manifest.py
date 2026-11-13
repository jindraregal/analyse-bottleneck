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

    def test_readme_install_uses_current_names(self):
        want = "%s@%s" % (self.plugin["name"], self.market["name"])
        installs = re.findall(r"plugin install (\S+)", self.readme)
        self.assertGreaterEqual(len(installs), 2)
        self.assertEqual(set(installs), {want})
        self.assertIn("marketplace add jindraregal/analyse-bottleneck", self.readme)


if __name__ == "__main__":
    unittest.main()
