"""leader-gate approve authority (f2-09): manager of the shared group, or the head leader.

A leader-gate message can NOT be released by a plain member (not even the receiver alone).
No shared group → safe refusal (stays in held/).

Setup: the file's kernel owner is REAL_UID (the sender). The **receiver** is a different uid
(RECEIVER) so resolve goes cross-user; the policy sets the sender uid to leader-gate. The manager
of the shared group is REAL_UID (so the test is the owner of groups.json — load_config requires
a role owner). The head leader is a separate uid.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from pm_mesh import approve, groups, maildir, message, trust

REAL_UID = os.geteuid()       # kernel owner of the file = the SENDER
RECEIVER = REAL_UID + 1       # the receiver (different → cross-user resolve)
MANAGER = REAL_UID            # manager of the shared group == the test user (owner of groups.json)
LEADER = REAL_UID + 90        # head leader
OUTSIDER = REAL_UID + 99
ADDR = f"{RECEIVER}:gallery"


def _write_policy(root):
    path = os.path.join(root, "policy.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"by_uid": {str(REAL_UID): trust.LEADER_GATE}}, fh)
    os.chmod(path, 0o600)
    return path


def _write_groups(root, with_group=True):
    cfg = {"head_leader": LEADER}
    if with_group:
        cfg["groups"] = {
            "dak": {
                "members": [f"{REAL_UID}:peer", f"{RECEIVER}:gallery"],
                "manager": f"{MANAGER}:peer",
            }
        }
    path = groups.groups_path(root)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    os.chmod(path, 0o644)
    return cfg


def _setup_held(root, body="LG-BODY"):
    maildir.maildrop(ADDR, root=root)
    msg = message.new_message(ADDR, body, from_=f"{REAL_UID}:peer")
    path = maildir.deliver(msg, root=root)
    os.rename(path, os.path.join(root, ADDR, "held", os.path.basename(path)))
    return msg


def _count(root, sub):
    d = os.path.join(root, ADDR, sub)
    return len([n for n in os.listdir(d) if not n.startswith(".") and not n.endswith(".shown")])


class LeaderGateApproveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.policy = _write_policy(self.root)
        _write_groups(self.root)
        patcher = mock.patch.dict(os.environ, {"MESH_POLICY": self.policy}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _approve(self, msg_id, approver_uid):
        return approve.approve(msg_id, root=self.root, approver_uid=approver_uid, address=ADDR)

    def test_held_level_is_leader_gate(self):
        _setup_held(self.root)
        held_dir = os.path.join(self.root, ADDR, "held")
        held_path = os.path.join(held_dir, os.listdir(held_dir)[0])
        level, owner, _verified_bytes = approve._held_level(held_path, RECEIVER)
        self.assertEqual(level, trust.LEADER_GATE)
        self.assertEqual(owner, REAL_UID)

    def test_manager_can_release(self):
        msg = _setup_held(self.root)
        new_path = self._approve(msg.id, approver_uid=MANAGER)
        self.assertTrue(os.path.isfile(new_path))
        self.assertEqual(_count(self.root, "held"), 0)

    def test_head_leader_can_release(self):
        msg = _setup_held(self.root)
        new_path = self._approve(msg.id, approver_uid=LEADER)
        self.assertTrue(os.path.isfile(new_path))

    def test_receiver_alone_cannot_release(self):
        msg = _setup_held(self.root)
        with self.assertRaises(approve.ApproveError):
            self._approve(msg.id, approver_uid=RECEIVER)
        self.assertEqual(_count(self.root, "new"), 0)
        self.assertEqual(_count(self.root, "held"), 1)

    def test_outsider_rejected(self):
        msg = _setup_held(self.root)
        with self.assertRaises(approve.ApproveError):
            self._approve(msg.id, approver_uid=OUTSIDER)
        self.assertEqual(_count(self.root, "held"), 1)

    def test_no_shared_group_safe_refusal(self):
        # Valid group (owner=manager=REAL_UID) but one that does NOT contain the RECEIVER → no shared group.
        cfg = {
            "groups": {"andere": {"members": [f"{REAL_UID}:peer", f"{OUTSIDER}:x"],
                                  "manager": f"{MANAGER}:peer"}},
            "head_leader": LEADER,
        }
        path = groups.groups_path(self.root)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        os.chmod(path, 0o644)
        msg = _setup_held(self.root)
        with self.assertRaises(approve.ApproveError):
            self._approve(msg.id, approver_uid=MANAGER)
        self.assertEqual(_count(self.root, "held"), 1)
        # the head leader CAN co-approve it, even without a shared group.
        new_path = self._approve(msg.id, approver_uid=LEADER)
        self.assertTrue(os.path.isfile(new_path))


if __name__ == "__main__":
    unittest.main()
