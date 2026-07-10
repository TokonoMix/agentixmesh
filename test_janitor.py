"""Tests for the cur/ janitor: moving orphaned (claimed-but-not-shown) messages back to new/."""

import os
import tempfile
import unittest

from pm_mesh import maildir

ADDR = "1000:proj"


class JanitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.drop = maildir.maildrop(ADDR, root=self.root)
        self.cur = os.path.join(self.drop, "cur")
        self.new = os.path.join(self.drop, "new")

    def _place_in_cur(self, name, mtime, body=b"x"):
        path = os.path.join(self.cur, name)
        with open(path, "wb") as fh:
            fh.write(body)
        os.utime(path, (mtime, mtime))
        return path

    def test_fresh_cur_file_not_recovered(self):
        now = 10_000
        self._place_in_cur("msgA", mtime=now)
        n = maildir.recover_stale(ADDR, root=self.root, max_age_s=900, now=now)
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(os.path.join(self.cur, "msgA")))
        self.assertFalse(os.path.exists(os.path.join(self.new, "msgA")))

    def test_old_cur_file_recovered_to_new(self):
        now = 10_000
        self._place_in_cur("msgB", mtime=now - 1000)  # older than max_age
        n = maildir.recover_stale(ADDR, root=self.root, max_age_s=900, now=now)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(os.path.join(self.cur, "msgB")))
        self.assertTrue(os.path.exists(os.path.join(self.new, "msgB")))

    def test_old_shown_file_not_recovered(self):
        now = 10_000
        self._place_in_cur("msgC", mtime=now - 1000)
        # shown marker: sidecar <name>.shown
        shown = os.path.join(self.cur, "msgC.shown")
        with open(shown, "wb") as fh:
            fh.write(b"")
        os.utime(shown, (now - 1000, now - 1000))
        n = maildir.recover_stale(ADDR, root=self.root, max_age_s=900, now=now)
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(os.path.join(self.cur, "msgC")))
        self.assertFalse(os.path.exists(os.path.join(self.new, "msgC")))

    def test_shown_sidecar_itself_not_recovered(self):
        now = 10_000
        # only an old sidecar with no corresponding message — must be ignored
        shown = os.path.join(self.cur, "orphan.shown")
        with open(shown, "wb") as fh:
            fh.write(b"")
        os.utime(shown, (now - 5000, now - 5000))
        n = maildir.recover_stale(ADDR, root=self.root, max_age_s=900, now=now)
        self.assertEqual(n, 0)
        self.assertFalse(os.path.exists(os.path.join(self.new, "orphan.shown")))

    def test_boundary_exactly_max_age_not_recovered(self):
        now = 10_000
        self._place_in_cur("msgD", mtime=now - 900)  # age == max_age
        n = maildir.recover_stale(ADDR, root=self.root, max_age_s=900, now=now)
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(os.path.join(self.cur, "msgD")))

    def test_multiple_mixed(self):
        now = 10_000
        self._place_in_cur("fresh", mtime=now)
        self._place_in_cur("old1", mtime=now - 2000)
        self._place_in_cur("old2", mtime=now - 3000)
        n = maildir.recover_stale(ADDR, root=self.root, max_age_s=900, now=now)
        self.assertEqual(n, 2)
        self.assertTrue(os.path.exists(os.path.join(self.new, "old1")))
        self.assertTrue(os.path.exists(os.path.join(self.new, "old2")))
        self.assertTrue(os.path.exists(os.path.join(self.cur, "fresh")))


if __name__ == "__main__":
    unittest.main()
