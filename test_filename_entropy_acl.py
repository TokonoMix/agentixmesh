"""F5 (f2-02): ≥128-bit filename entropy hard-assert + optional receiver-only read ACL.

For group-readable cross-user drops, confidentiality between fellow senders rests on the
unguessability of the filename. This pins that down hard (a lowering breaks a test) and provides
the ACL hardening that makes filename secrecy no longer load-bearing. Same-user stays ``0600``.
"""

import os
import re
import stat
import tempfile
import unittest
from unittest import mock

from pm_mesh import maildir, message


def _imode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class EntropyTest(unittest.TestCase):
    def test_final_name_is_128_bits(self):
        name = maildir._final_name()
        self.assertEqual(len(name), 32, "32 hex chars == 128 bits")
        self.assertRegex(name, r"\A[0-9a-f]{32}\Z")

    def test_deliver_uses_min_entropy(self):
        with tempfile.TemporaryDirectory() as root:
            msg = message.new_message("1000:gallery", "hoi", from_="1000:peer")
            path = maildir.deliver(msg, root=root)
            self.assertGreaterEqual(len(os.path.basename(path)), 32)

    def test_lowered_entropy_breaks_failclosed(self):
        # A hypothetical lowering < 128 bits fails the assert (and deliver leaves nothing behind).
        with mock.patch.object(maildir, "_NAME_BYTES", 8):
            with self.assertRaises(ValueError):
                maildir._final_name()
            with tempfile.TemporaryDirectory() as root:
                msg = message.new_message("1000:gallery", "hoi", from_="1000:peer")
                with self.assertRaises(ValueError):
                    maildir.deliver(msg, root=root)
                new_dir = os.path.join(root, "1000:gallery", "new")
                leftovers = [n for n in os.listdir(new_dir) if not n.startswith(".")]
                self.assertEqual(leftovers, [], "a failed deliver leaves no message in new/")


class CrossUserReadModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.uid = os.geteuid()
        self.addr = f"{self.uid}:gallery"
        patcher = mock.patch("pm_mesh.maildir._mesh_gid", return_value=os.getgid())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _deliver_cross(self):
        msg = message.new_message(self.addr, "hoi", from_=f"{self.uid}:peer")
        return maildir.deliver(msg, root=self.root, mode="cross_user")

    def test_same_user_stays_0600(self):
        msg = message.new_message(self.addr, "hoi", from_=f"{self.uid}:peer")
        path = maildir.deliver(msg, root=self.root, mode="same_user")
        self.assertEqual(_imode(path), 0o600)

    def test_cross_user_default_is_group_read(self):
        # ACL off (default): message is group-read (0640) so the receiver (mesh member) can read it.
        with mock.patch.dict(os.environ, {"MESH_ACL": "0"}, clear=False):
            path = self._deliver_cross()
        m = _imode(path)
        self.assertEqual(m, 0o640, "group-read, no other-read")
        self.assertFalse(m & 0o007, "never other-readable")

    def test_acl_mode_receiver_only_when_setfacl_succeeds(self):
        # ACL hardening succeeds → 0600 (no group-read); the ACL handles the receiver read.
        with mock.patch.dict(os.environ, {"MESH_ACL": "1"}, clear=False), \
             mock.patch("pm_mesh.maildir._try_setfacl", return_value=True) as setfacl:
            path = self._deliver_cross()
        self.assertEqual(_imode(path), 0o600, "no group-read in ACL mode")
        setfacl.assert_called_once()
        # the ACL is set for the receiver uid.
        self.assertEqual(setfacl.call_args.args[1], self.uid)

    def test_acl_mode_falls_back_to_group_read_when_setfacl_unavailable(self):
        # setfacl is missing → graceful fallback to group-read + no crash.
        with mock.patch.dict(os.environ, {"MESH_ACL": "1"}, clear=False), \
             mock.patch("pm_mesh.maildir._try_setfacl", return_value=False):
            path = self._deliver_cross()
        self.assertEqual(_imode(path), 0o640, "fallback: group-read, drop stays functional")

    def test_try_setfacl_missing_binary_returns_false(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(maildir._try_setfacl("/nonexistent", self.uid))

    @unittest.skipUnless(
        __import__("shutil").which("setfacl") and __import__("shutil").which("getfacl"),
        "setfacl/getfacl not available",
    )
    def test_acl_receiver_read_is_effective_not_masked(self):
        # REGRESSION (real setfacl, NO mock): a chmod AFTER setfacl nukes the ACL mask (--> #effective:---),
        # leaving the receiver unable to read their own message. Verify the receiver ACL is EFFECTIVE.
        import subprocess

        with mock.patch.dict(os.environ, {"MESH_ACL": "1"}, clear=False):
            path = self._deliver_cross()
        if _imode(path) != 0o600:
            self.skipTest("fs without ACL support → group-read fallback; mask test n/a")
        out = subprocess.run(
            ["getfacl", "-p", path], capture_output=True, text=True, timeout=10
        ).stdout
        self.assertIn(f"user:{self.uid}:r--", out, "receiver ACL entry is missing")
        self.assertIn("mask::r--", out, "mask must not mask away the receiver ACL (bug: mask::---)")
        self.assertNotIn("#effective:---", out, "receiver ACL entry has been masked away (unreadable)")


if __name__ == "__main__":
    unittest.main()
