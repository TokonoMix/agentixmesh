"""Read-only ``mesh status`` (design §17-dogfood) — introspection of the **own** address.

No §6 presence/discovery, nothing cross-user, nothing that mutates: purely counting/listing ``new/``,
``cur/``, ``held/`` and the ``seen/`` stamps of the own ``uid:project`` address.
"""

import io
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from pm_mesh import maildir, message, replay, status

UID = 1000
ADDR = f"{UID}:gallery"


@contextmanager
def as_address(root):
    with mock.patch("os.getuid", return_value=UID), \
         mock.patch("os.getcwd", return_value="/home/user/gallery"), \
         mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
        yield


def _seed(root, n_new, m_cur, k_seen):
    """Deliver ``n_new+m_cur`` messages, claim ``m_cur`` of them to cur/, and set ``k_seen`` stamps."""
    for i in range(n_new + m_cur):
        maildir.deliver(message.new_message(ADDR, f"m{i}", from_=f"{UID}:peer"), root=root)
    for path in maildir.list_new(ADDR, root=root)[:m_cur]:
        maildir.claim(path)
    store = replay.SeenStore(root=root)
    for i in range(k_seen):
        store.mark_seen(ADDR, f"id-{i}")


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_counts_match_seed(self):
        _seed(self.root, n_new=4, m_cur=2, k_seen=3)
        with as_address(self.root):
            info = status.gather()
        self.assertEqual(info["address"], ADDR)
        self.assertEqual(info["root"], self.root)
        self.assertEqual(info["new"], 4)
        self.assertEqual(info["cur"], 2)
        self.assertEqual(info["held"], 0)
        self.assertEqual(info["seen"], 3)

    def test_read_only_does_not_move_new(self):
        _seed(self.root, n_new=3, m_cur=0, k_seen=0)
        new_dir = os.path.join(self.root, ADDR, "new")
        before = sorted(os.listdir(new_dir))
        with as_address(self.root):
            status.gather()
            with redirect_stdout(io.StringIO()):
                status.main([])
        after = sorted(os.listdir(new_dir))
        self.assertEqual(before, after)  # nothing claimed/moved
        self.assertEqual(len(after), 3)

    def test_missing_held_counts_zero(self):
        _seed(self.root, n_new=1, m_cur=0, k_seen=0)
        shutil.rmtree(os.path.join(self.root, ADDR, "held"))
        with as_address(self.root):
            info = status.gather()
        self.assertEqual(info["held"], 0)  # no crash

    def test_main_prints_and_returns_zero(self):
        _seed(self.root, n_new=2, m_cur=1, k_seen=1)
        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                rc = status.main([])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn(ADDR, out)
        self.assertIn(self.root, out)

    def test_seen_count_ignores_turns_subdir(self):
        # The f1c turn counter lives under seen/turns/ — that doesn't count as a processed message.
        _seed(self.root, n_new=1, m_cur=0, k_seen=2)
        store = replay.SeenStore(root=self.root)
        store.record_turn(ADDR, "th", "x")  # creates seen/turns/...
        with as_address(self.root):
            info = status.gather()
        self.assertEqual(info["seen"], 2)


if __name__ == "__main__":
    unittest.main()
