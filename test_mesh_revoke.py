"""mesh revoke <uid> + offboarding/cleanup (f2-12, F6): groups + presence + held cleaned up, idempotent.

Authorization: head leader or manager. The OS group step (gpasswd -d) is documented in the runbook.
"""

import json
import os
import tempfile
import unittest

from pm_mesh import audit, groups, maildir, message, revoke

REAL_UID = os.geteuid()        # head leader + owner of groups.json + revoker
LEADER = REAL_UID
TARGET = REAL_UID + 5
KEEP1 = REAL_UID + 6           # stays, manager of the group
KEEP2 = REAL_UID + 7
OUTSIDER = REAL_UID + 8


def _write_groups(root):
    cfg = {
        "groups": {
            "dak": {
                "members": [f"{TARGET}:a", f"{KEEP1}:b", f"{KEEP2}:c"],
                "manager": f"{KEEP1}:b",
            }
        },
        "head_leader": LEADER,
    }
    path = groups.groups_path(root)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    os.chmod(path, 0o644)


def _put_presence(root, uid):
    pdir = os.path.join(root, "presence")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, f"{uid}.json"), "w", encoding="utf-8") as fh:
        json.dump({"user": uid, "project": "x", "cwd": "/x", "pid": uid,
                   "started": "2026-01-01T00:00:00Z", "last_seen": "2026-01-01T00:00:00Z"}, fh)


def _put_held(root, address):
    maildir.maildrop(address, root=root)
    msg = message.new_message(address, "held-body", from_=f"{REAL_UID}:peer")
    path = maildir.deliver(msg, root=root)
    os.rename(path, os.path.join(root, address, "held", os.path.basename(path)))


def _held_count(root, address):
    d = os.path.join(root, address, "held")
    try:
        return len([n for n in os.listdir(d) if not n.startswith(".")])
    except OSError:
        return 0


class RevokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        _write_groups(self.root)

    def test_removes_from_groups(self):
        revoke.revoke(TARGET, root=self.root, revoker_uid=LEADER)
        cfg = groups.load_groups(self.root)
        self.assertNotIn(TARGET, groups.member_uids(cfg))
        self.assertIn("dak", cfg.get("groups", {}), "group continues to exist (≥2 members left)")

    def test_cleans_presence(self):
        _put_presence(self.root, TARGET)
        _put_presence(self.root, KEEP1)
        summary = revoke.revoke(TARGET, root=self.root, revoker_uid=LEADER)
        self.assertEqual(summary["presence_removed"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, "presence", f"{TARGET}.json")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "presence", f"{KEEP1}.json")))

    def test_cleans_held_to_target(self):
        _put_held(self.root, f"{TARGET}:a")   # message TO target
        revoke.revoke(TARGET, root=self.root, revoker_uid=LEADER)
        self.assertEqual(_held_count(self.root, f"{TARGET}:a"), 0)

    def test_cleans_held_from_target_owner(self):
        # message FROM target (kernel owner) in someone else's held/ — test target == REAL_UID (the file owner).
        _put_held(self.root, f"{KEEP2}:x")
        removed = revoke._clean_held(self.root, REAL_UID)
        self.assertEqual(removed, 1)
        self.assertEqual(_held_count(self.root, f"{KEEP2}:x"), 0)

    def test_idempotent(self):
        _put_presence(self.root, TARGET)
        _put_held(self.root, f"{TARGET}:a")
        first = revoke.revoke(TARGET, root=self.root, revoker_uid=LEADER)
        self.assertTrue(first["groups_removed_from"])
        second = revoke.revoke(TARGET, root=self.root, revoker_uid=LEADER)
        self.assertEqual(second["groups_removed_from"], [])
        self.assertEqual(second["presence_removed"], 0)
        self.assertEqual(second["held_removed"], 0)

    def test_unauthorized_rejected(self):
        with self.assertRaises(revoke.RevokeError):
            revoke.revoke(TARGET, root=self.root, revoker_uid=OUTSIDER)

    def test_manager_can_revoke(self):
        # KEEP1 is manager → may revoke.
        revoke.revoke(TARGET, root=self.root, revoker_uid=KEEP1)
        self.assertNotIn(TARGET, groups.member_uids(groups.load_groups(self.root)))

    def test_audit_written(self):
        revoke.revoke(TARGET, root=self.root, revoker_uid=LEADER)
        with open(audit.audit_path(self.root), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("revoke", log)
        self.assertIn(str(TARGET), log)

    def test_manager_dropped_group_when_manager_revoked(self):
        # Revoke the manager → group becomes invalid → removed entirely.
        revoke.revoke(KEEP1, root=self.root, revoker_uid=LEADER)
        cfg = groups.load_groups(self.root)
        self.assertNotIn("dak", cfg.get("groups", {}))


class RunbookTest(unittest.TestCase):
    def test_runbook_documents_os_step(self):
        path = os.path.join(os.path.dirname(__file__), "pm_mesh", "CROSS-USER-SETUP.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("gpasswd -d", text)
        self.assertIn("revoke", text)


if __name__ == "__main__":
    unittest.main()
