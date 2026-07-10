"""Light validation of the opt-in hook install artifacts (snippet + README)."""

import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SNIPPET = os.path.join(HERE, "hooks", "settings-snippet.json")
README = os.path.join(HERE, "hooks", "README.md")


class HookArtifactsTest(unittest.TestCase):
    def test_snippet_is_valid_json(self):
        with open(SNIPPET, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIsInstance(data, dict)
        self.assertIn("hooks", data)

    def test_snippet_has_both_events(self):
        with open(SNIPPET, encoding="utf-8") as fh:
            data = json.load(fh)
        hooks = data["hooks"]
        self.assertIn("SessionStart", hooks)
        self.assertIn("UserPromptSubmit", hooks)

    def test_snippet_references_mesh_inject(self):
        with open(SNIPPET, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn("mesh-inject", raw)

    def test_snippet_documents_composition(self):
        # A _README/_comment key that explicitly says: compose, don't overwrite.
        with open(SNIPPET, encoding="utf-8") as fh:
            data = json.load(fh)
        readme_keys = [k for k in data if k.lower().startswith("_")]
        self.assertTrue(readme_keys, "expected a _README/_comment key in the snippet")
        blob = " ".join(str(data[k]) for k in readme_keys).lower()
        self.assertTrue("compon" in blob or "merge" in blob)

    def test_readme_exists_and_mentions_key_terms(self):
        self.assertTrue(os.path.isfile(README))
        with open(README, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("SessionStart", text)
        self.assertIn("UserPromptSubmit", text)
        self.assertIn("fail-closed", text.lower())
        self.assertIn("mesh-inject", text)


if __name__ == "__main__":
    unittest.main()
