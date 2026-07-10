"""Re-validation of maildrop ownership/perms on every turn (design §14, fail-closed).

A drifted/replaced maildrop dir (group/other bits, wrong owner, or replaced by a
symlink) must not be silently trusted: ``maildir.maildrop()`` re-validates the drop dir and raises
``MaildropError`` on drift. The inject hook catches that globally (exit 0, show nothing).
"""

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pm_mesh import inject, maildir


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class RevalidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.addr = "1000:gallery"

    def _make(self):
        """Create a fresh, secure maildrop and return its path."""
        return maildir.maildrop(self.addr, root=self.root)

    def test_fresh_maildrop_is_secure(self):
        # A fresh 0700 drop passes validation without raising.
        drop = self._make()
        self.assertEqual(_mode(drop), 0o700)
        # Second turn stays green (idempotent + re-validation).
        self.assertEqual(maildir.maildrop(self.addr, root=self.root), drop)

    def test_group_writable_rejected(self):
        drop = self._make()
        os.chmod(drop, 0o770)  # drift: group rwx
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root)

    def test_other_readable_rejected(self):
        drop = self._make()
        os.chmod(drop, 0o704)  # drift: other r
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root)

    def test_symlink_rejected(self):
        drop = self._make()
        # Replace the drop dir with a symlink to a (itself perfectly fine) other dir.
        elders = os.path.join(self.root, "elders")
        os.makedirs(os.path.join(elders, "new"), mode=0o700)
        os.makedirs(os.path.join(elders, "cur"), mode=0o700)
        os.makedirs(os.path.join(elders, "held"), mode=0o700)
        os.chmod(elders, 0o700)
        # Tear down the real drop and put a symlink in its place.
        import shutil

        shutil.rmtree(drop)
        os.symlink(elders, drop)
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root)

    def test_restored_mode_passes_again(self):
        drop = self._make()
        os.chmod(drop, 0o770)
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root)
        # Restore to 0700 → passes again.
        os.chmod(drop, 0o700)
        self.assertEqual(maildir.maildrop(self.addr, root=self.root), drop)

    def test_wrong_owner_rejected(self):
        drop = self._make()
        # Force a mismatch between st_uid and euid by giving geteuid a different value.
        with mock.patch("os.geteuid", return_value=os.geteuid() + 12345):
            with self.assertRaises(maildir.MaildropError):
                maildir.maildrop(self.addr, root=self.root)

    def test_inject_fail_closed_on_drift(self):
        # Fresh drop for address "1000:projectB", then drift → inject.main() exit 0, empty stdout.
        with mock.patch("os.getuid", return_value=1000), \
             mock.patch("os.getcwd", return_value="/home/user/projectB"), \
             mock.patch.dict(os.environ, {"MESH_ROOT": self.root}, clear=False):
            drop = maildir.maildrop("1000:projectB")
            os.chmod(drop, 0o777)  # drift
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = inject.main()
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
