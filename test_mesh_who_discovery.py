"""mesh who / mesh list discovery (f2-07): visibility only for fellow group members; leader sees everything.

Expired heartbeats are not shown; empty presence → empty output, no crash.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from pm_mesh import groups, presence, who

NOW = 1_000_000.0


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _put(root, uid, project, last_seen_epoch):
    pdir = os.path.join(root, presence.PRESENCE_SUBDIR)
    os.makedirs(pdir, exist_ok=True)
    rec = {
        "user": uid,
        "project": project,
        "cwd": f"/home/{uid}/{project}",
        "pid": uid,  # unique enough for the test
        "started": _iso(last_seen_epoch),
        "last_seen": _iso(last_seen_epoch),
    }
    with open(os.path.join(pdir, f"{uid}.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


CALLER = 1001
MATE = 1002       # same group as CALLER
STRANGER = 1003   # no shared group
LEADER = 1009


CFG = {
    "groups": {"infra": {"members": [CALLER, MATE], "manager": CALLER}},
    "head_leader": LEADER,
}


class GroupsQueryTest(unittest.TestCase):
    def test_shares_group(self):
        self.assertTrue(groups.shares_group(CFG, CALLER, MATE))
        self.assertFalse(groups.shares_group(CFG, CALLER, STRANGER))

    def test_is_head_leader(self):
        self.assertTrue(groups.is_head_leader(CFG, LEADER))
        self.assertFalse(groups.is_head_leader(CFG, CALLER))

    def test_load_groups_missing_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(groups.load_groups(root), {})


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        _put(self.root, MATE, "infra", NOW - 10)
        _put(self.root, STRANGER, "secret", NOW - 10)

    def _uids(self, caller):
        return {
            hb["user"]
            for hb in who.visible_sessions(caller, root=self.root, now=NOW, groups_cfg=CFG)
        }

    def test_sees_group_mate(self):
        self.assertIn(MATE, self._uids(CALLER))

    def test_non_group_mate_hidden(self):
        self.assertNotIn(STRANGER, self._uids(CALLER))

    def test_head_leader_sees_all(self):
        seen = self._uids(LEADER)
        self.assertIn(MATE, seen)
        self.assertIn(STRANGER, seen)

    def test_self_visible(self):
        _put(self.root, CALLER, "gallery", NOW - 5)
        self.assertIn(CALLER, self._uids(CALLER))

    def test_expired_heartbeat_not_shown(self):
        _put(self.root, MATE, "infra", NOW - 10_000)  # well expired
        self.assertNotIn(MATE, self._uids(CALLER))


class EmptyPresenceTest(unittest.TestCase):
    def test_no_presence_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(who.active_sessions(root=root, now=NOW), [])
            self.assertEqual(who.visible_sessions(CALLER, root=root, now=NOW, groups_cfg=CFG), [])


if __name__ == "__main__":
    unittest.main()
