"""HARD-01 §2: `mesh doctor` — read-only self-diagnosis of the host wiring.

Reports whether the inject-hook is wired, the skill symlink is correct, MESH_ROOT/MESH_ACL, and the
mailbox perms — the checklist that used to be done by hand with `sudo ls /srv/mesh/...`. Purely
reading; never mutates, never raises, always returns 0.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pm_mesh import doctor


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def _write_settings(self, content):
        cdir = os.path.join(self.home, ".claude")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "settings.json"), "w", encoding="utf-8") as fh:
            fh.write(content)

    def _by_name(self, home=None):
        return {c["name"]: c for c in doctor.diagnose(home=home or self.home)}

    def test_inject_hook_detected(self):
        self._write_settings('{"hooks": {"SessionStart": [{"hooks": '
                             '[{"command": "/usr/local/bin/mesh-inject"}]}]}}')
        self.assertTrue(self._by_name()["inject-hook"]["ok"])

    def test_inject_hook_missing_is_not_ok(self):
        self._write_settings('{"hooks": {}}')
        self.assertFalse(self._by_name()["inject-hook"]["ok"])

    def test_no_settings_file_fails_closed(self):
        checks = self._by_name()  # nothing written
        self.assertFalse(checks["inject-hook"]["ok"])  # absent => not ok, no crash

    def test_skill_symlink_ok(self):
        skills = os.path.join(self.home, ".claude", "skills")
        os.makedirs(skills, exist_ok=True)
        target = os.path.join(self.tmp.name, "skill")
        os.makedirs(target, exist_ok=True)
        os.symlink(target, os.path.join(skills, "pm-mesh"))
        self.assertTrue(self._by_name()["skill-symlink"]["ok"])

    def test_skill_symlink_missing_is_not_ok(self):
        self.assertFalse(self._by_name()["skill-symlink"]["ok"])

    def test_mesh_root_and_acl_reported(self):
        with mock.patch.dict(os.environ, {"MESH_ROOT": self.tmp.name, "MESH_ACL": "1"}, clear=False):
            checks = self._by_name()
        self.assertIn("mesh-root", checks)
        self.assertIn("mesh-acl", checks)
        self.assertIn(self.tmp.name, checks["mesh-root"]["detail"])

    def test_main_returns_zero_and_never_raises(self):
        self._write_settings("{}")
        with mock.patch.dict(os.environ, {"HOME": self.home}, clear=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = doctor.main([])
        self.assertEqual(rc, 0)
        self.assertTrue(buf.getvalue())  # printed something


if __name__ == "__main__":
    unittest.main()
