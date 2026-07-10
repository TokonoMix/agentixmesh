"""Per-session presence/heartbeat (f2-06): inject refreshes a heartbeat; is_online is deterministic.

Fail-open: an error in the presence layer must never break inject delivery (exit 0, message still shown).
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from unittest import mock

from pm_mesh import inject, maildir, message, presence

REAL_UID = os.geteuid()
ADDR = f"{REAL_UID}:gallery"


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_inject(root):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("os.getuid", return_value=REAL_UID), \
         mock.patch("os.getcwd", return_value="/home/user/gallery"), \
         mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
        with redirect_stdout(out), redirect_stderr(err):
            rc = inject.main()
    return rc, out.getvalue()


class IsOnlineTest(unittest.TestCase):
    def test_fresh_is_online(self):
        hb = {"last_seen": _iso(1000.0)}
        self.assertTrue(presence.is_online(hb, now=1100.0, max_age_s=600))

    def test_expired_is_offline(self):
        hb = {"last_seen": _iso(1000.0)}
        self.assertFalse(presence.is_online(hb, now=2000.0, max_age_s=600))

    def test_boundary_exactly_max_age_is_online(self):
        hb = {"last_seen": _iso(1000.0)}
        self.assertTrue(presence.is_online(hb, now=1600.0, max_age_s=600))  # exactly on the boundary

    def test_one_past_boundary_is_offline(self):
        hb = {"last_seen": _iso(1000.0)}
        self.assertFalse(presence.is_online(hb, now=1601.0, max_age_s=600))

    def test_missing_or_bad_last_seen_is_offline(self):
        self.assertFalse(presence.is_online({}, now=1000.0, max_age_s=600))
        self.assertFalse(presence.is_online({"last_seen": 123}, now=1000.0, max_age_s=600))
        self.assertFalse(presence.is_online({"last_seen": "garbage"}, now=1000.0, max_age_s=600))
        self.assertFalse(presence.is_online("notadict", now=1000.0, max_age_s=600))


class HeartbeatWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_heartbeat_has_required_fields(self):
        with mock.patch("os.getuid", return_value=REAL_UID), \
             mock.patch("os.getcwd", return_value="/home/user/gallery"):
            path = presence.heartbeat(root=self.root, now="2026-06-26T00:00:00Z")
        with open(path, encoding="utf-8") as fh:
            hb = json.load(fh)
        for field in ("user", "project", "cwd", "pid", "started", "last_seen"):
            self.assertIn(field, hb)
        self.assertEqual(hb["user"], REAL_UID)
        self.assertEqual(hb["project"], "gallery")
        self.assertEqual(hb["pid"], os.getpid())
        self.assertEqual(hb["last_seen"], "2026-06-26T00:00:00Z")

    def test_started_preserved_across_turns(self):
        with mock.patch("os.getuid", return_value=REAL_UID), \
             mock.patch("os.getcwd", return_value="/home/user/gallery"):
            presence.heartbeat(root=self.root, now="2026-06-26T00:00:00Z")
            path = presence.heartbeat(root=self.root, now="2026-06-26T00:05:00Z")
        with open(path, encoding="utf-8") as fh:
            hb = json.load(fh)
        self.assertEqual(hb["started"], "2026-06-26T00:00:00Z", "started stays at the first turn")
        self.assertEqual(hb["last_seen"], "2026-06-26T00:05:00Z", "last_seen follows the latest turn")

    def test_inject_refreshes_heartbeat(self):
        rc, _ = _run_inject(self.root)
        self.assertEqual(rc, 0)
        hb_path = os.path.join(self.root, presence.PRESENCE_SUBDIR, f"{os.getpid()}.json")
        self.assertTrue(os.path.isfile(hb_path), "inject wrote the heartbeat")

    def test_inject_fail_open_when_presence_breaks(self):
        # presence.heartbeat raises → delivery continues (message still shown, exit 0).
        maildir.maildrop(ADDR, root=self.root)
        msg = message.new_message(ADDR, "HB-FAILOPEN-MARK", from_=f"{REAL_UID}:peer")
        maildir.deliver(msg, root=self.root)
        with mock.patch("pm_mesh.presence.heartbeat", side_effect=RuntimeError("boom")):
            rc, out = _run_inject(self.root)
        self.assertEqual(rc, 0)
        self.assertIn("HB-FAILOPEN-MARK", out, "a presence error must not break delivery")


class PruneStaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.dir = presence.presence_dir(self.root)
        self.now = 1_000_000.0

    def _write(self, pid, last_seen_epoch, raw=None):
        path = os.path.join(self.dir, f"{pid}.json")
        with open(path, "w", encoding="utf-8") as fh:
            if raw is not None:
                fh.write(raw)
            else:
                json.dump({"user": REAL_UID, "project": "x", "cwd": "/x", "pid": pid,
                           "started": _iso(last_seen_epoch), "last_seen": _iso(last_seen_epoch)}, fh)
        return path

    def _dead_pid(self):
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()  # reaped → pid is dead
        self.assertFalse(presence._pid_alive(p.pid), "test assumption: pid is dead after wait/reap")
        return p.pid

    def test_dead_pid_removed(self):
        path = self._write(self._dead_pid(), self.now)  # fresh, but dead session
        self.assertEqual(presence.prune_stale(root=self.root, now=self.now), 1)
        self.assertFalse(os.path.exists(path))

    def test_live_fresh_kept(self):
        path = self._write(os.getpid(), self.now)  # live pid + fresh → stays
        self.assertEqual(presence.prune_stale(root=self.root, now=self.now), 0)
        self.assertTrue(os.path.exists(path))

    def test_old_removed_even_if_pid_alive(self):
        # Fallback: live (possibly reused) pid but last_seen > TTL → clean up anyway.
        path = self._write(os.getpid(), self.now - 2 * presence.PRUNE_TTL_S)
        self.assertEqual(presence.prune_stale(root=self.root, now=self.now), 1)
        self.assertFalse(os.path.exists(path))

    def test_corrupt_record_removed(self):
        path = os.path.join(self.dir, "123.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not-valid json")
        self.assertEqual(presence.prune_stale(root=self.root, now=self.now), 1)
        self.assertFalse(os.path.exists(path))

    def test_non_json_left_alone(self):
        keep = os.path.join(self.dir, "readme.txt")
        with open(keep, "w", encoding="utf-8") as fh:
            fh.write("no heartbeat")
        presence.prune_stale(root=self.root, now=self.now)
        self.assertTrue(os.path.exists(keep), "non-.json files are not touched")

    def test_inject_prunes_dead_heartbeat(self):
        # End-to-end: an inject turn cleans up a dead-pid heartbeat AND keeps its own (fresh).
        dead = self._dead_pid()
        self._write(dead, self.now)
        rc, _ = _run_inject(self.root)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, f"{dead}.json")),
                         "inject janitor cleaned up the dead heartbeat")
        self.assertTrue(os.path.exists(os.path.join(self.dir, f"{os.getpid()}.json")),
                        "its own fresh heartbeat remains")


if __name__ == "__main__":
    unittest.main()
