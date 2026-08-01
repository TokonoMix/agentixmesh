"""Tests for ``pm_mesh.team_init_cli`` — the ``mesh team-init`` command.

The load-bearing property is the split: this tool never escalates privilege and never executes a root
step. Each hard constraint gets its own test: no ``subprocess``/``sudo`` invocation; ``--apply`` writes
only the caller's own dropbox under ``$MESH_ROOT``; ``--apply`` refuses (exit 2, nothing written) on a
broken substrate; and the root is resolved from the environment so the suite can never touch the real
shared root.
"""

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pm_mesh import config, maildir, team_init, team_init_cli


def _snapshot(root):
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(dirs) + sorted(files):
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, root))
    return sorted(out)


def _step(name, ok, priv=False):
    return {"name": name, "ok": ok, "detail": "d", "privileged": priv, "remedy": "r"}


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.root = os.path.join(self.base, "mesh")
        self.home = os.path.join(self.base, "home")
        os.makedirs(self.home, mode=0o700)
        patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": self.home,
                "XDG_DATA_HOME": os.path.join(self.base, "xdg"),
                "MESH_ROOT": self.root,
                "MESH_CROSS_USER": "",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = team_init_cli.main(argv)
        return rc, out.getvalue()


class ExitCodeTest(unittest.TestCase):
    def test_all_ok_is_0(self):
        steps = [_step("shared_root_present", True), _step("shared_root_mode", True),
                 _step("own_dropbox", True)]
        self.assertEqual(team_init_cli._exit_code(steps), 0)

    def test_root_ok_but_missing_is_1(self):
        steps = [_step("shared_root_present", True), _step("shared_root_mode", True),
                 _step("own_dropbox", False)]
        self.assertEqual(team_init_cli._exit_code(steps), 1)

    def test_unusable_root_is_2(self):
        for mode_ok in (False, None):
            steps = [_step("shared_root_present", True), _step("shared_root_mode", mode_ok),
                     _step("own_dropbox", True)]
            self.assertEqual(team_init_cli._exit_code(steps), 2)
        steps = [_step("shared_root_present", False), _step("shared_root_mode", None)]
        self.assertEqual(team_init_cli._exit_code(steps), 2)


class PlanOutputTest(_Isolated):
    def test_empty_root_plan_lists_steps_and_exits_2(self):
        rc, out = self._run([])
        self.assertEqual(rc, 2)  # absent root -> unusable substrate
        self.assertIn("shared_root_present", out)
        self.assertIn("own_dropbox", out)
        self.assertIn("mesh-doctor", out)  # verification line

    def test_json_parses(self):
        rc, out = self._run(["--json"])
        parsed = json.loads(out)
        self.assertIn("steps", parsed)
        self.assertIn("summary", parsed)
        self.assertEqual(rc, 2)

    def test_root_is_resolved_from_environment(self):
        # A future default change must not make this silently read the real shared root.
        rc, out = self._run(["--json"])
        parsed = json.loads(out)
        present = next(s for s in parsed["steps"] if s["name"] == "shared_root_present")
        self.assertIn(self.root, present["detail"])
        self.assertNotIn("/srv/mesh", present["detail"])


class ApplyTest(_Isolated):
    def test_apply_on_good_root_creates_only_the_dropbox(self):
        os.makedirs(self.root, mode=0o700)  # root exists but can't be owner-0 in a test
        before = _snapshot(self.root)
        addr = config.current_address()
        # Given a usable root (the owner-0/mode check needs real root, tested separately),
        # --apply must create exactly the caller's own dropbox and nothing else.
        with mock.patch.object(team_init_cli, "_root_usable", return_value=True):
            rc, out = self._run(["--apply"])
        after = _snapshot(self.root)
        new_paths = set(after) - set(before)
        self.assertEqual(new_paths, {addr, f"{addr}/new", f"{addr}/cur", f"{addr}/held"})
        self.assertIn(addr, out)
        self.assertIn(rc, (0, 1))  # 0 if all else ok, 1 if human steps remain

    def test_apply_on_bad_root_writes_nothing_and_exits_2(self):
        os.makedirs(self.root, mode=0o755)  # present but wrong mode/owner -> unusable
        before = _snapshot(self.root)
        rc, out = self._run(["--apply"])
        after = _snapshot(self.root)
        self.assertEqual(rc, 2)
        self.assertEqual(before, after)  # nothing written on a broken substrate
        self.assertIn("refusing", out.lower())

    def test_apply_makes_no_subprocess_or_system_call(self):
        os.makedirs(self.root, mode=0o700)
        with mock.patch.object(team_init_cli, "_root_usable", return_value=True), \
             mock.patch.object(subprocess, "run") as run, \
             mock.patch.object(subprocess, "Popen") as popen, \
             mock.patch.object(os, "system") as system:
            self._run(["--apply"])
        run.assert_not_called()
        popen.assert_not_called()
        system.assert_not_called()


class NoSudoSourceTest(unittest.TestCase):
    def test_module_has_no_privilege_escalation_primitive(self):
        # No execution primitive may appear in the source. (The remedy STRINGS print 'sudo' as data,
        # which is fine; what must never exist is code that runs a command.)
        src = open(team_init_cli.__file__, encoding="utf-8").read()
        for primitive in ("import subprocess", "subprocess.", "os.system", "check_call",
                          "check_output", "Popen", "os.exec", "pty.spawn"):
            self.assertNotIn(primitive, src, f"escalation primitive {primitive!r} in module")


if __name__ == "__main__":
    unittest.main()
