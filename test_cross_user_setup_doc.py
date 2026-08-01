"""Drift guard: the check names cited in ``CROSS-USER-SETUP.md`` must exist in ``team_init.plan()``.

T07 folds the manual runbook into ``mesh team-init`` by tagging each runbook step with the
``team_init.plan()`` check name it maps to. This test makes that mapping non-drifting: every check
name the doc references must be a real step name, and the runbook's own privileged/dropbox steps must
all be tagged.
"""

import os
import re
import unittest

from pm_mesh import team_init

_DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pm_mesh", "CROSS-USER-SETUP.md")

# Every annotation line carries this marker; check names on it are pure-identifier backtick tokens.
_MARKER = "team-init check"
_NAME_RE = re.compile(r"`([a-z_]+)`")


def _doc_check_names():
    names = set()
    with open(_DOC, encoding="utf-8") as fh:
        for line in fh:
            if _MARKER in line:
                names.update(_NAME_RE.findall(line))
    return names


class CrossUserSetupDocTest(unittest.TestCase):
    def test_doc_check_names_all_exist_in_plan(self):
        plan_names = {s["name"] for s in team_init.plan()}
        doc_names = _doc_check_names()
        self.assertTrue(doc_names, "the runbook references no team-init check names")
        unknown = doc_names - plan_names
        self.assertEqual(unknown, set(), f"runbook cites check names not in team_init.plan(): {unknown}")

    def test_runbook_covers_its_privileged_and_dropbox_steps(self):
        # The runbook is the reference for exactly these steps; each must be tagged so the tool's
        # output lines up one-to-one with the sections here.
        expected = {
            "group_mesh_exists",
            "caller_in_group",
            "shared_root_present",
            "shared_root_mode",
            "protected_hardlinks",
            "own_dropbox",
        }
        missing = expected - _doc_check_names()
        self.assertEqual(missing, set(), f"runbook does not tag these steps: {missing}")


if __name__ == "__main__":
    unittest.main()
