"""t06 — claim-race-test (test-only): atomic claim, consume_new owner_uid, hardlink-skip.

The functionality (``claim`` / ``consume_new``) already exists (introduced as a dependency of t10);
these are t06's own tests that pin down the race/identity behavior. A tmp root via the
``root=`` parameter keeps the real mesh root untouched.
"""

import os
import tempfile
import unittest

from pm_mesh import maildir, message


class ClaimRaceTest(unittest.TestCase):
    def _deliver(self, root, to="1000:gallery", body="hoi"):
        msg = message.new_message(to, body, from_="1000:peer")
        return maildir.deliver(msg, root=root)

    def test_claim_atomic_exactly_one_winner(self):
        """Two claims on the same new/ path: exactly one wins (cur/ path), the other gets None."""
        with tempfile.TemporaryDirectory() as root:
            path = self._deliver(root)
            first = maildir.claim(path)
            second = maildir.claim(path)

            self.assertIsNotNone(first, "the first claim should win")
            self.assertIsNone(second, "the second claim should give None (source already gone)")
            # Winner points to cur/, with the same basename.
            self.assertEqual(os.path.dirname(first), os.path.join(root, "1000:gallery", "cur"))
            self.assertEqual(os.path.basename(first), os.path.basename(path))
            self.assertTrue(os.path.isfile(first))
            self.assertFalse(os.path.exists(path), "source in new/ has been moved")

    def test_consume_new_yields_owner_uid_and_body(self):
        """consume_new yields (Message, owner_uid, cur_path) with owner_uid == euid and body intact."""
        with tempfile.TemporaryDirectory() as root:
            self._deliver(root, body="regel1\nregel2 — café ☕")
            items = list(maildir.consume_new("1000:gallery", root=root))

            self.assertEqual(len(items), 1)
            msg, owner_uid, cur_path = items[0]
            self.assertEqual(owner_uid, os.geteuid())
            self.assertEqual(msg.body, "regel1\nregel2 — café ☕")
            self.assertEqual(os.path.dirname(cur_path), os.path.join(root, "1000:gallery", "cur"))
            self.assertTrue(os.path.isfile(cur_path))

    def test_consume_new_quarantines_hardlink_without_crash(self):
        """A hardlinked file in new/ is skipped (identity refuses st_nlink > 1),
        no crash, and the file is quarantined to held/ (NOT left in cur/ — that would
        make the janitor pull it back to new/ endlessly)."""
        with tempfile.TemporaryDirectory() as root:
            path = self._deliver(root)
            # Create a hardlink → the new/ file gets st_nlink == 2 and becomes unverifiable.
            link_path = os.path.join(root, "outside-link")
            os.link(path, link_path)
            self.assertGreater(os.stat(path).st_nlink, 1)

            # No crash; the hardlinked message is not yielded.
            items = list(maildir.consume_new("1000:gallery", root=root))
            self.assertEqual(items, [])

            # The file has been claimed (new/ → cur/) and then quarantined to held/.
            new_dir = os.path.join(root, "1000:gallery", "new")
            cur_dir = os.path.join(root, "1000:gallery", "cur")
            held_dir = os.path.join(root, "1000:gallery", "held")
            self.assertEqual(os.listdir(new_dir), [])
            self.assertEqual([n for n in os.listdir(cur_dir) if not n.startswith(".")], [])
            held_files = [n for n in os.listdir(held_dir) if not n.startswith(".")]
            self.assertEqual(held_files, [os.path.basename(path)])

            # The janitor does NOT pull it back (no churn).
            self.assertEqual(maildir.recover_stale("1000:gallery", root=root, max_age_s=0), 0)


if __name__ == "__main__":
    unittest.main()
