"""Doc-drift pins for ``docs/EVAL-HARNESS.md``.

The failure mode these hold shut is a stale doc: a "gaps we do not cover yet" paragraph that
outlives the gap, so the doc reads as honest while quietly describing a version of the tool that no
longer exists. Two pins: the doc must not still claim the two now-shipped categories are missing, and
its category table must list every category the code actually ships.
"""

import os
import unittest

from pm_mesh.eval_corpus import CATEGORIES

_DOC = os.path.join(os.path.dirname(__file__), "docs", "EVAL-HARNESS.md")


class EvalDocTest(unittest.TestCase):
    def setUp(self):
        with open(_DOC, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_doc_no_longer_claims_the_two_categories_are_missing(self):
        # The old paragraph ended "Both are on the list; neither is here yet." — once repo_edit and
        # mesh_action shipped, that sentence became false. Pin that it is gone.
        self.assertNotIn("neither is here yet", self.text)
        self.assertNotIn("Both are on the list", self.text)

    def test_category_table_lists_every_shipped_category(self):
        # Every category in CATEGORIES must appear in the doc, so the table cannot silently lag the
        # code. Backtick form (`repo_edit`) is how the doc names them.
        for cat in CATEGORIES:
            self.assertIn(f"`{cat}`", self.text, f"{cat} missing from EVAL-HARNESS.md")

    def test_doc_documents_both_new_flags(self):
        self.assertIn("--repo-file", self.text)
        self.assertIn("--third-addr", self.text)


if __name__ == "__main__":
    unittest.main()
