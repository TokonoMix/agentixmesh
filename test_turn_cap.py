"""Advisory per-thread turn cap (design §8) — log-only anti-ping-loop, fail-open.

In B1 this is a **detection aid, not enforcement**: when ``MAX_TURNS_PER_THREAD`` is exceeded,
one advisory line appears on **stderr**; ALL messages still just get shown on stdout and
nothing is blocked or onderdrukt. Any error in the counting/logging layer is swallowed (fail-open).
"""

import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest import mock

from pm_mesh import config, inject, maildir, message, replay

UID = 1000
ADDR = f"{UID}:gallery"


@contextmanager
def as_address(root):
    with mock.patch("os.getuid", return_value=UID), \
         mock.patch("os.getcwd", return_value="/home/user/gallery"), \
         mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
        yield


def _deliver_thread(root, thread, n):
    """Deliver ``n`` fresh messages in a single ``thread`` to ADDR."""
    for i in range(n):
        msg = message.new_message(ADDR, f"turn {i}", thread=thread, from_=f"{UID}:peer")
        maildir.deliver(msg, root=root)


def _run_inject(root):
    out, err = io.StringIO(), io.StringIO()
    with as_address(root):
        with redirect_stdout(out), redirect_stderr(err):
            rc = inject.main()
    return rc, out.getvalue(), err.getvalue()


def _frame_count(stdout):
    return stdout.count("</mesh-msg>")


class TurnCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_default_cap_is_50(self):
        self.assertEqual(config.MAX_TURNS_PER_THREAD, 50)

    def test_over_cap_warns_but_shows_all(self):
        n = config.MAX_TURNS_PER_THREAD + 1
        _deliver_thread(self.root, "thread-loop", n)
        rc, out, err = _run_inject(self.root)
        self.assertEqual(rc, 0)
        # all messages shown — nothing is onderdrukt
        self.assertEqual(_frame_count(out), n)
        # exactly one advisory line on stderr, with thread id and the cap
        self.assertIn("thread-loop", err)
        self.assertIn(str(config.MAX_TURNS_PER_THREAD), err)
        self.assertIn("ping-loop", err)
        self.assertEqual(err.strip().count("\n"), 0)  # one line

    def test_under_cap_is_silent(self):
        _deliver_thread(self.root, "thread-short", 3)
        rc, out, err = _run_inject(self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(_frame_count(out), 3)
        self.assertEqual(err, "")

    def test_counter_persists_across_runs(self):
        # First run: just under the cap → silent.
        _deliver_thread(self.root, "th", config.MAX_TURNS_PER_THREAD)
        rc, out, err = _run_inject(self.root)
        self.assertEqual(_frame_count(out), config.MAX_TURNS_PER_THREAD)
        self.assertEqual(err, "")
        # Second run: two more messages → total goes over the cap → advisory.
        _deliver_thread(self.root, "th", 2)
        rc, out, err = _run_inject(self.root)
        self.assertEqual(_frame_count(out), 2)  # only the new ones shown
        self.assertIn("th", err)
        self.assertIn("ping-loop", err)

    def test_fail_open_on_broken_counter(self):
        _deliver_thread(self.root, "thread-x", 2)
        # Break the counting layer: record_turn raises an error.
        with mock.patch.object(replay.SeenStore, "record_turn", side_effect=OSError("boom")):
            rc, out, err = _run_inject(self.root)
        # Messages are still shown; no exception, exit 0.
        self.assertEqual(rc, 0)
        self.assertEqual(_frame_count(out), 2)


if __name__ == "__main__":
    unittest.main()
