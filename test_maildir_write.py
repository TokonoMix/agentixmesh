import os
import re
import stat
import tempfile
import unittest

from pm_mesh import maildir, message


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class MaildropTest(unittest.TestCase):
    def test_maildrop_creates_subdirs_0700(self):
        with tempfile.TemporaryDirectory() as root:
            drop = maildir.maildrop("1000:gallery", root=root)
            self.assertEqual(drop, os.path.join(root, "1000:gallery"))
            for sub in ("new", "cur", "held"):
                p = os.path.join(drop, sub)
                self.assertTrue(os.path.isdir(p), sub)
                self.assertEqual(_mode(p), 0o700, sub)
            self.assertEqual(_mode(drop), 0o700)

    def test_maildrop_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            a = maildir.maildrop("1000:gallery", root=root)
            b = maildir.maildrop("1000:gallery", root=root)
            self.assertEqual(a, b)
            self.assertTrue(os.path.isdir(os.path.join(b, "new")))

    def test_maildrop_address_with_colon_is_safe(self):
        with tempfile.TemporaryDirectory() as root:
            drop = maildir.maildrop("42:peer", root=root)
            self.assertTrue(os.path.isdir(drop))
            self.assertEqual(os.path.basename(drop), "42:peer")


class DeliverTest(unittest.TestCase):
    def _msg(self, to="1000:gallery", body="hoi"):
        return message.new_message(to, body, from_="1000:peer")

    def test_deliver_lands_in_new(self):
        with tempfile.TemporaryDirectory() as root:
            msg = self._msg()
            path = maildir.deliver(msg, root=root)
            self.assertEqual(
                os.path.dirname(path),
                os.path.join(root, "1000:gallery", "new"),
            )
            self.assertTrue(os.path.isfile(path))

    def test_deliver_content_roundtrips(self):
        with tempfile.TemporaryDirectory() as root:
            msg = self._msg(body="regel1\nregel2 — café ☕")
            path = maildir.deliver(msg, root=root)
            with open(path, encoding="utf-8") as fh:
                back = message.from_json(fh.read())
            self.assertEqual(back, msg)

    def test_deliver_file_mode_0600(self):
        with tempfile.TemporaryDirectory() as root:
            path = maildir.deliver(self._msg(), root=root)
            self.assertEqual(_mode(path), 0o600)

    def test_deliver_name_has_128bit_entropy(self):
        with tempfile.TemporaryDirectory() as root:
            path = maildir.deliver(self._msg(), root=root)
            name = os.path.basename(path)
            # >=128 bits == >=32 hex chars
            hexpart = re.match(r"^[0-9a-f]+", name).group(0)
            self.assertGreaterEqual(len(hexpart), 32)

    def test_deliver_twice_no_collision(self):
        with tempfile.TemporaryDirectory() as root:
            p1 = maildir.deliver(self._msg(body="one"), root=root)
            p2 = maildir.deliver(self._msg(body="two"), root=root)
            self.assertNotEqual(p1, p2)
            new_dir = os.path.join(root, "1000:gallery", "new")
            self.assertEqual(len(os.listdir(new_dir)), 2)

    def test_deliver_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as root:
            maildir.deliver(self._msg(), root=root)
            new_dir = os.path.join(root, "1000:gallery", "new")
            names = os.listdir(new_dir)
            self.assertEqual(len(names), 1)
            for n in names:
                self.assertFalse(n.startswith("."), n)
                self.assertNotIn("tmp", n.lower())


if __name__ == "__main__":
    unittest.main()
