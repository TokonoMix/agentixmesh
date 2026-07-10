"""Cross-user transport (phase 2, f2-01): shared dropbox perms + secure-assert variant.

The same-user (0700) paths remain byte-identical (see the other 126 phase-1 tests); cross-user is
**additive**. The shared ``new/`` dropbox gets mode ``0o3730`` (setgid + sticky + ``730``) and group
``mesh``; ``assert_secure_maildrop`` rejects anything looser (fail-closed).

CI has no real ``mesh`` group, so ``_mesh_gid`` is mocked to the test process's group
(so that ``os.chown(-, -, gid)`` is a no-op and ``st_gid`` matches). This tests the **mode
computation + assert logic**, per the acceptance criteria ("in CI without the group → test the mode computation").
"""

import os
import stat
import tempfile
import unittest
from unittest import mock

from pm_mesh import config, maildir


def _imode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _full_mode(path):
    return os.lstat(path).st_mode


class CrossUserModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.uid = os.geteuid()
        self.addr = f"{self.uid}:gallery"
        # Mock the mesh gid to the process's own group: chown becomes a no-op and st_gid matches,
        # so the assert logic can be tested without a real 'mesh' group.
        self.gid = os.getgid()
        patcher = mock.patch("pm_mesh.maildir._mesh_gid", return_value=self.gid)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_cross(self):
        return maildir.maildrop(self.addr, root=self.root, mode="cross_user")

    # ---- positive: the shared dropbox gets exactly 3730 + setgid + sticky + group mesh ----
    def test_new_dir_has_setgid_sticky_and_730(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        m = _full_mode(new_dir)
        self.assertTrue(m & stat.S_ISGID, "setgid bit must be set (group inheritance)")
        self.assertTrue(m & stat.S_ISVTX, "sticky bit must be set (no cross-delete)")
        self.assertFalse(m & stat.S_ISUID, "setuid must NEVER be set")
        self.assertEqual(stat.S_IMODE(m) & 0o777, 0o730, "perm bits exactly 730 (no group/other read)")
        self.assertEqual(stat.S_IMODE(m), 0o3730, "full mode = setgid|sticky|730")
        st = os.lstat(new_dir)
        self.assertEqual(st.st_uid, self.uid, "new/ owned by the receiver")
        self.assertEqual(st.st_gid, self.gid, "new/ in group mesh")

    def test_cur_held_owner_only_0700(self):
        drop = self._make_cross()
        self.assertEqual(_imode(os.path.join(drop, "cur")), 0o700)
        self.assertEqual(_imode(os.path.join(drop, "held")), 0o700)

    def test_revalidate_passes_second_turn(self):
        drop = self._make_cross()
        # idempotent: second pass stays green (re-validation does not raise).
        self.assertEqual(maildir.maildrop(self.addr, root=self.root, mode="cross_user"), drop)

    # ---- negative: assert rejects any loosening ----
    def test_assert_rejects_group_readable(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        os.chmod(new_dir, 0o3770)  # group +read (730 → 770)
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root, mode="cross_user")

    def test_assert_rejects_other_readable(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        os.chmod(new_dir, 0o3734)  # other +read
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root, mode="cross_user")

    def test_assert_rejects_missing_sticky(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        os.chmod(new_dir, 0o2730)  # setgid but NO sticky
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root, mode="cross_user")

    def test_assert_rejects_missing_setgid(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        os.chmod(new_dir, 0o1730)  # sticky but NO setgid (the literal "1730" from the ticket)
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root, mode="cross_user")

    def test_assert_rejects_wrong_group(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        # Mesh gid now differs from the real group of new/ → reject.
        with mock.patch("pm_mesh.maildir._mesh_gid", return_value=self.gid + 99999):
            with self.assertRaises(maildir.MaildropError):
                maildir.assert_secure_maildrop(new_dir, mode="cross_user", expected_uid=self.uid)

    def test_assert_rejects_wrong_owner(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        with self.assertRaises(maildir.MaildropError):
            maildir.assert_secure_maildrop(new_dir, mode="cross_user", expected_uid=self.uid + 12345)

    def test_assert_rejects_symlink(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        elders = os.path.join(self.root, "elders")
        os.makedirs(elders, mode=0o3730)
        import shutil

        shutil.rmtree(new_dir)
        os.symlink(elders, new_dir)
        with self.assertRaises(maildir.MaildropError):
            maildir.assert_secure_maildrop(new_dir, mode="cross_user", expected_uid=self.uid)

    def test_assert_positive_exact_passes(self):
        drop = self._make_cross()
        new_dir = os.path.join(drop, "new")
        # exactly 3730 + mesh + owner == receiver → no raise.
        maildir.assert_secure_maildrop(new_dir, mode="cross_user", expected_uid=self.uid)


class MissingMeshGroupTest(unittest.TestCase):
    """Without a class-level _mesh_gid mock: a missing 'mesh' group is fail-closed."""

    def test_missing_mesh_group_is_failclosed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        addr = f"{os.geteuid()}:gallery"
        with mock.patch("pm_mesh.maildir.grp.getgrnam", side_effect=KeyError("mesh")):
            with self.assertRaises(maildir.MaildropError):
                maildir.maildrop(addr, root=tmp.name, mode="cross_user")


class ModeSelectionTest(unittest.TestCase):
    """Mode selection: explicit arg, config flag, and the byte-identical same-user default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.addr = f"{os.geteuid()}:gallery"

    def test_default_is_same_user_0700(self):
        # No env, mode=None → same-user, byte-identical to phase 1 (all dirs 0700).
        with mock.patch.dict(os.environ, {}, clear=True):
            drop = maildir.maildrop(self.addr, root=self.root)
        for sub in ("new", "cur", "held"):
            self.assertEqual(_imode(os.path.join(drop, sub)), 0o700)

    def test_explicit_same_user(self):
        drop = maildir.maildrop(self.addr, root=self.root, mode="same_user")
        self.assertEqual(_imode(os.path.join(drop, "new")), 0o700)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            maildir.maildrop(self.addr, root=self.root, mode="bogus")

    def test_env_flag_selects_cross_user(self):
        with mock.patch.dict(os.environ, {"MESH_CROSS_USER": "1"}, clear=True), \
             mock.patch("pm_mesh.maildir._mesh_gid", return_value=os.getgid()):
            drop = maildir.maildrop(self.addr, root=self.root, mode=None)
            self.assertEqual(stat.S_IMODE(_full_mode(os.path.join(drop, "new"))), 0o3730)


class ConfigCrossUserEnabledTest(unittest.TestCase):
    def test_explicit_true(self):
        with mock.patch.dict(os.environ, {"MESH_CROSS_USER": "true"}, clear=True):
            self.assertTrue(config.cross_user_enabled())

    def test_explicit_false_overrides_root(self):
        with mock.patch.dict(os.environ, {"MESH_CROSS_USER": "no", "MESH_ROOT": "/srv/mesh"}, clear=True):
            self.assertFalse(config.cross_user_enabled())

    def test_derived_from_shared_root(self):
        with mock.patch.dict(os.environ, {"MESH_ROOT": "/srv/mesh"}, clear=True):
            self.assertTrue(config.cross_user_enabled())

    def test_default_same_user(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config.cross_user_enabled())


class RunbookExistsTest(unittest.TestCase):
    def test_cross_user_setup_runbook_present(self):
        path = os.path.join(os.path.dirname(__file__), "pm_mesh", "CROSS-USER-SETUP.md")
        self.assertTrue(os.path.isfile(path), "provisioning runbook CROSS-USER-SETUP.md is missing")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        # The runbook must document the core provisioning steps.
        for needle in ("mesh", "1730", "setgid", "sticky", "/srv/mesh"):
            self.assertIn(needle, text, f"runbook is missing {needle!r}")


if __name__ == "__main__":
    unittest.main()
