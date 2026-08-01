"""Tests for ``pm_mesh.team_init.plan`` — the read-only shared-root inspection.

Pins: (a) an empty root reports the root/dropbox steps missing; (b) a correctly-provisioned fake root
yields all-ok for the *unprivileged* steps; (c) a wrong root mode reports the expected mode taken
from ``config.CROSS_USER_ROOT_MODE`` (change the constant, the expectation follows); (d) a symlinked
root is refused; (e) a missing ``/proc`` entry is unknown, not ok; and (f) ``plan()`` performs no
writes whatsoever. Everything runs against a ``tmp_path`` root — the real shared root is never read.
"""

import os
import stat
import tempfile
import unittest
from unittest import mock

from pm_mesh import config, team_init


def _snapshot(root):
    """Recursive listing of (relpath, mode) — to prove plan() mutates nothing."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(dirs) + sorted(files):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            out.append((rel, stat.S_IMODE(os.lstat(full).st_mode)))
    return out


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.root = os.path.join(self.base, "mesh")  # NOT created by default
        self.home = os.path.join(self.base, "home")
        os.makedirs(self.home, mode=0o700)
        patcher = mock.patch.dict(
            os.environ,
            {
                "HOME": self.home,
                "XDG_DATA_HOME": os.path.join(self.base, "xdg"),
                "MESH_ROOT": self.root,
                "MESH_CROSS_USER": "",  # let it derive; tmp root => same-user modes for own_dropbox
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _steps(self):
        return {s["name"]: s for s in team_init.plan()}


class EmptyRootTest(_Isolated):
    def test_root_and_dropbox_report_missing(self):
        steps = self._steps()
        self.assertFalse(steps["shared_root_present"]["ok"])
        self.assertFalse(steps["own_dropbox"]["ok"])
        summary = team_init.summary(list(steps.values()))
        self.assertIn("shared_root_present", summary["missing"])
        self.assertIn("own_dropbox", summary["missing"])
        self.assertFalse(summary["ok"])

    def test_every_step_has_the_full_shape(self):
        for step in team_init.plan():
            self.assertEqual(set(step), {"name", "ok", "detail", "privileged", "remedy"})
            self.assertIsInstance(step["privileged"], bool)
            self.assertTrue(step["remedy"])  # a non-empty paste-able remedy


class ProvisionedUnprivilegedTest(_Isolated):
    def _provision_unprivileged(self):
        # own dropbox (same-user modes, since the tmp root is not the shared cross-user root)
        os.makedirs(self.root, mode=0o700)
        addr = config.current_address()
        drop = os.path.join(self.root, addr)
        os.makedirs(drop, mode=0o700)
        for sub in ("new", "cur", "held"):
            os.makedirs(os.path.join(drop, sub), mode=0o700)
            os.chmod(os.path.join(drop, sub), 0o700)
        os.chmod(drop, 0o700)
        # the four host couplings under the fake HOME + a fake wrapper dir
        claude = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(claude, "skills"), mode=0o700)
        with open(os.path.join(claude, "settings.json"), "w", encoding="utf-8") as fh:
            fh.write('{"hooks":{"SessionStart":[{"hooks":[{"command":"mesh-inject"}]}]}}')
        skill_target = os.path.join(self.base, "skill")
        os.makedirs(skill_target, mode=0o700)
        os.symlink(skill_target, os.path.join(claude, "skills", "pm-mesh"))
        wrapper_dir = os.path.join(self.base, "bin")
        os.makedirs(wrapper_dir, mode=0o700)
        for w in ("mesh-send", "mesh-inject"):
            path = os.path.join(wrapper_dir, w)
            open(path, "w").close()
            os.chmod(path, 0o755)  # a wrapper that is not executable is not an installed wrapper
        # A fake site dir with a .pth pointing at this checkout — the python_path step asks whether a
        # FRESH interpreter would find pm_mesh, so a fully-provisioned host must be staged, not
        # inherited from whatever the developer happens to have installed. Without this the test
        # passes or fails on a property of the machine rather than of the code under test.
        site_dir = os.path.join(self.base, "site-packages")
        os.makedirs(site_dir, mode=0o700)
        parent = os.path.dirname(os.path.dirname(os.path.abspath(team_init.__file__)))
        with open(os.path.join(site_dir, "pm-mesh.pth"), "w", encoding="utf-8") as fh:
            fh.write(parent + "\n")
        self._site_dir = site_dir
        return wrapper_dir

    def test_unprivileged_steps_all_ok(self):
        wrapper_dir = self._provision_unprivileged()
        with mock.patch.object(team_init, "_WRAPPER_DIR", wrapper_dir), \
                mock.patch.object(team_init, "_site_dirs", lambda: [self._site_dir]):
            steps = self._steps()
            summary = team_init.summary(list(steps.values()))
        self.assertEqual(summary["unprivileged_missing"], [], msg=steps)
        for name in ("own_dropbox", "python_path", "wrappers", "skill_symlink", "inject_hook"):
            self.assertTrue(steps[name]["ok"], msg=f"{name}: {steps[name]}")


class RootModeTest(_Isolated):
    def test_wrong_mode_reports_expected_from_constant(self):
        os.makedirs(self.root, mode=0o755)
        os.chmod(self.root, 0o755)
        step = self._steps()["shared_root_mode"]
        self.assertNotEqual(step["ok"], True)
        self.assertIn(f"{config.CROSS_USER_ROOT_MODE:o}", step["detail"])

        # Change the constant -> the reported expectation follows it.
        with mock.patch.object(config, "CROSS_USER_ROOT_MODE", 0o2750):
            step2 = self._steps()["shared_root_mode"]
            self.assertIn("2750", step2["detail"])


class SymlinkedRootTest(_Isolated):
    def test_symlinked_root_is_refused(self):
        real = os.path.join(self.base, "real-root")
        os.makedirs(real, mode=0o755)
        os.symlink(real, self.root)  # MESH_ROOT now points at a symlink
        step = self._steps()["shared_root_present"]
        self.assertFalse(step["ok"])
        self.assertIn("symlink", step["detail"].lower())


class ProtectedHardlinksTest(_Isolated):
    def test_missing_proc_entry_is_unknown_not_ok(self):
        with mock.patch.object(team_init, "_PROTECTED_HARDLINKS_PATH",
                               os.path.join(self.base, "nope")):
            step = self._steps()["protected_hardlinks"]
        self.assertIsNone(step["ok"])  # unknown, never a false pass

    def test_value_one_is_ok_zero_is_fail(self):
        good = os.path.join(self.base, "ph-1")
        bad = os.path.join(self.base, "ph-0")
        with open(good, "w") as fh:
            fh.write("1\n")
        with open(bad, "w") as fh:
            fh.write("0\n")
        with mock.patch.object(team_init, "_PROTECTED_HARDLINKS_PATH", good):
            self.assertTrue(self._steps()["protected_hardlinks"]["ok"])
        with mock.patch.object(team_init, "_PROTECTED_HARDLINKS_PATH", bad):
            self.assertFalse(self._steps()["protected_hardlinks"]["ok"])


class NoWriteTest(_Isolated):
    def test_plan_writes_nothing(self):
        os.makedirs(self.root, mode=0o755)
        addr = config.current_address()
        drop = os.path.join(self.root, addr)
        os.makedirs(os.path.join(drop, "new"), mode=0o700)
        before = _snapshot(self.base)
        team_init.plan()
        after = _snapshot(self.base)
        self.assertEqual(before, after)

    def test_mesh_root_is_honoured_not_default(self):
        # The reported root is the one from MESH_ROOT, never the hardcoded shared default.
        step = self._steps()["shared_root_present"]
        self.assertIn(self.root, step["detail"])
        self.assertNotIn("/srv/mesh", step["detail"])


if __name__ == "__main__":
    unittest.main()
