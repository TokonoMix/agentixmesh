"""Tests for pm_mesh.identity — unforgeable sender-uid via fstat-on-fd (anti-TOCTOU)."""

import os
import stat
import tempfile
import unittest

from pm_mesh import identity, message


def _open_fd_count():
    """Number of open fds of this process (counts /proc/self/fd) — to detect fd leaks."""
    return len(os.listdir("/proc/self/fd"))


class OpenVerifiedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _write(self, name, data=b"hoi"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_regular_file_yields_owner_uid(self):
        path = self._write("regular")
        fd, owner_uid = identity.open_verified(path)
        try:
            self.assertEqual(owner_uid, os.getuid())
            self.assertIsInstance(fd, int)
            # fd is actually readable
            self.assertEqual(os.read(fd, 3), b"hoi")
        finally:
            os.close(fd)

    def test_hardlink_rejected(self):
        path = self._write("orig")
        link = os.path.join(self.dir, "hardlink")
        os.link(path, link)
        with self.assertRaises(identity.IdentityError):
            identity.open_verified(link)

    def test_fifo_rejected(self):
        path = os.path.join(self.dir, "fifo")
        os.mkfifo(path)
        with self.assertRaises(identity.IdentityError):
            identity.open_verified(path)

    def test_symlink_to_regular_rejected(self):
        target = self._write("target")
        link = os.path.join(self.dir, "symlink")
        os.symlink(target, link)
        with self.assertRaises(identity.IdentityError):
            identity.open_verified(link)

    def test_missing_path_raises_identity_error(self):
        with self.assertRaises(identity.IdentityError):
            identity.open_verified(os.path.join(self.dir, "nope"))

    def test_no_fd_leak_on_success(self):
        path = self._write("leaktest")
        before = _open_fd_count()
        for _ in range(200):
            fd, _uid = identity.open_verified(path)
            os.close(fd)
        self.assertEqual(_open_fd_count(), before)

    def test_reject_paths_do_not_leak_fds(self):
        fifo = os.path.join(self.dir, "fifo2")
        os.mkfifo(fifo)
        orig = self._write("orig2")
        link = os.path.join(self.dir, "hl2")
        os.link(orig, link)
        before = _open_fd_count()
        for _ in range(200):
            with self.assertRaises(identity.IdentityError):
                identity.open_verified(fifo)
            with self.assertRaises(identity.IdentityError):
                identity.open_verified(link)
        self.assertEqual(_open_fd_count(), before)


class ReadVerifiedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def test_reads_content_and_owner(self):
        path = os.path.join(self.dir, "msg")
        with open(path, "wb") as fh:
            fh.write(b"payload-bytes")
        data, owner_uid = identity.read_verified(path)
        self.assertEqual(data, b"payload-bytes")
        self.assertEqual(owner_uid, os.getuid())

    def test_read_rejects_hardlink(self):
        path = os.path.join(self.dir, "orig")
        with open(path, "wb") as fh:
            fh.write(b"x")
        link = os.path.join(self.dir, "hl")
        os.link(path, link)
        with self.assertRaises(identity.IdentityError):
            identity.read_verified(link)

    def test_read_bounded_by_max_body(self):
        path = os.path.join(self.dir, "big")
        oversized = b"a" * (message.MAX_BODY_BYTES + 10 * 1024)
        with open(path, "wb") as fh:
            fh.write(oversized)
        with self.assertRaises(identity.IdentityError):
            identity.read_verified(path)

    def test_read_no_fd_leak(self):
        path = os.path.join(self.dir, "msg2")
        with open(path, "wb") as fh:
            fh.write(b"y")
        before = _open_fd_count()
        for _ in range(200):
            data, _uid = identity.read_verified(path)
            self.assertEqual(data, b"y")
        self.assertEqual(_open_fd_count(), before)


if __name__ == "__main__":
    unittest.main()
