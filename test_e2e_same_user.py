"""End-to-end same-user smoke test (phase 1): one uid, two projects, the whole chain.

Proves: send (project A) → deliver → inject (project B) shows a DATA frame → dedup on a second
inject → janitor recovers an orphaned cur/ message and still delivers it.
"""

import io
import os
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from pm_mesh import inject, maildir, send

UID = 1000
ADDR_A = f"{UID}:projectA"
ADDR_B = f"{UID}:projectB"


@contextmanager
def as_project(uid, project, root):
    """Pretend we're running as ``uid`` in a cwd with basename ``project``, against ``root``."""
    cwd = f"/home/user/{project}"
    with mock.patch("os.getuid", return_value=uid), \
         mock.patch("os.getcwd", return_value=cwd), \
         mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
        yield


def _run_inject(uid, project, root):
    buf = io.StringIO()
    with as_project(uid, project, root):
        with redirect_stdout(buf):
            rc = inject.main()
    return rc, buf.getvalue()


def _send(uid, project, root, to, body):
    out = io.StringIO()
    with as_project(uid, project, root):
        with redirect_stdout(out):
            rc = send.main([to, body])
    return rc, out.getvalue().strip()


class E2ESameUserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_full_chain_send_inject_dedup(self):
        # 1. project A → project B
        rc, msg_id = _send(UID, "projectA", self.root, ADDR_B, "hello from A\nline2")
        self.assertEqual(rc, 0)
        self.assertTrue(msg_id)

        # 2. project B injects: DATA frame with (framed) body
        rc, out = _run_inject(UID, "projectB", self.root)
        self.assertEqual(rc, 0)
        self.assertIn("mesh-msg", out)
        self.assertIn("hello from A", out)
        self.assertIn("line2", out)
        # per-line framing: both body lines behind the prefix
        self.assertIn(frame_prefixed("hello from A"), out)
        self.assertIn(frame_prefixed("line2"), out)
        # kernel-verified sender uid in the header
        self.assertIn(str(UID), out)

        # 3. second inject from B → nothing (dedup/replay)
        rc2, out2 = _run_inject(UID, "projectB", self.root)
        self.assertEqual(rc2, 0)
        self.assertEqual(out2, "")

    def test_janitor_recovers_orphaned_and_delivers(self):
        # Send A→B, claim the message manually into cur/ (as if a session crashed before
        # showing), age it, and prove that a later inject still shows it via the janitor.
        rc, _ = _send(UID, "projectA", self.root, ADDR_B, "forgotten message")
        self.assertEqual(rc, 0)

        with as_project(UID, "projectB", self.root):
            new_paths = maildir.list_new(ADDR_B)
            self.assertEqual(len(new_paths), 1)
            cur_path = maildir.claim(new_paths[0])
            self.assertIsNotNone(cur_path)
            old = time.time() - 10_000  # well older than max_age (900s), no .shown marker
            os.utime(cur_path, (old, old))

        rc, out = _run_inject(UID, "projectB", self.root)
        self.assertEqual(rc, 0)
        self.assertIn("forgotten message", out)


def frame_prefixed(line):
    from pm_mesh import frame
    return frame.LINE_PREFIX + line


if __name__ == "__main__":
    unittest.main()
