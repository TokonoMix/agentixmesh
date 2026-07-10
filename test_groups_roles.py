"""Groups & roles (f2-08): validation (>=2 members + manager-member), helpers, and edit authorization.

Edit rights = manager + head leader, enforced via file ownership/perms (not a self-declared role).
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from pm_mesh import groups

REAL_UID = os.geteuid()
HEAD_LEADER = REAL_UID          # head leader in the test config
MANAGER = REAL_UID + 11         # manager of the group
MEMBER = REAL_UID + 22          # plain member


def _cfg(head=HEAD_LEADER):
    return {
        "groups": {
            "roof-coordination": {
                "members": [f"{HEAD_LEADER}:backend", f"{MANAGER}:reviews"],
                "manager": f"{MANAGER}:reviews",
            }
        },
        "head_leader": head,
    }


class ValidationTest(unittest.TestCase):
    def test_valid_group_ok(self):
        groups.validate_config(_cfg())  # did not raise

    def test_too_few_members_rejected(self):
        cfg = {"groups": {"g": {"members": [f"{HEAD_LEADER}:a"], "manager": f"{HEAD_LEADER}:a"}}}
        with self.assertRaises(groups.GroupError):
            groups.validate_config(cfg)

    def test_missing_manager_rejected(self):
        cfg = {"groups": {"g": {"members": [f"{HEAD_LEADER}:a", f"{MANAGER}:b"]}}}
        with self.assertRaises(groups.GroupError):
            groups.validate_config(cfg)

    def test_manager_not_member_rejected(self):
        cfg = {"groups": {"g": {"members": [f"{HEAD_LEADER}:a", f"{MANAGER}:b"], "manager": f"{MEMBER}:x"}}}
        with self.assertRaises(groups.GroupError):
            groups.validate_config(cfg)


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()

    def test_members_and_manager(self):
        self.assertEqual(len(groups.members(self.cfg, "roof-coordination")), 2)
        self.assertEqual(groups.manager_of(self.cfg, "roof-coordination"), MANAGER)

    def test_groups_of_by_address_and_uid(self):
        self.assertEqual(groups.groups_of(self.cfg, f"{MANAGER}:reviews"), ["roof-coordination"])
        self.assertEqual(groups.groups_of(self.cfg, HEAD_LEADER), ["roof-coordination"])
        self.assertEqual(groups.groups_of(self.cfg, MEMBER), [])

    def test_is_leader_recognizes_head(self):
        self.assertTrue(groups.is_leader(self.cfg, HEAD_LEADER))
        self.assertFalse(groups.is_leader(self.cfg, MANAGER))

    def test_head_leader_env_override(self):
        with mock.patch.dict(os.environ, {"MESH_HEAD_LEADER": str(MEMBER)}, clear=False):
            self.assertTrue(groups.is_head_leader(self.cfg, MEMBER))
            self.assertFalse(groups.is_head_leader(self.cfg, HEAD_LEADER))

    def test_authorize_edit(self):
        self.assertTrue(groups.authorize_edit(self.cfg, HEAD_LEADER))   # head leader
        self.assertTrue(groups.authorize_edit(self.cfg, MANAGER))       # manager
        self.assertFalse(groups.authorize_edit(self.cfg, MEMBER))       # plain member


class SaveLoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_save_by_leader_then_load(self):
        # Head leader == REAL_UID (the owner of the written file) → load validates the owner.
        cfg = _cfg(head=REAL_UID)
        groups.save_config(cfg, root=self.root, editor_uid=REAL_UID)
        loaded = groups.load_config(self.root)
        self.assertEqual(groups.manager_of(loaded, "roof-coordination"), MANAGER)

    def test_save_by_non_manager_non_leader_rejected(self):
        cfg = _cfg(head=HEAD_LEADER)
        with self.assertRaises(groups.GroupError):
            groups.save_config(cfg, root=self.root, editor_uid=MEMBER)
        self.assertFalse(os.path.exists(groups.groups_path(self.root)))

    def test_load_rejects_group_writable(self):
        cfg = _cfg(head=REAL_UID)
        path = groups.save_config(cfg, root=self.root, editor_uid=REAL_UID)
        os.chmod(path, 0o664)  # group-writable drift
        with self.assertRaises(groups.GroupError):
            groups.load_config(self.root)

    def test_load_rejects_owner_not_a_role(self):
        # Config written by REAL_UID, but only declares other uids as a role → owner has no role.
        cfg = {
            "groups": {"g": {"members": [f"{MANAGER}:a", f"{MEMBER}:b"], "manager": f"{MANAGER}:a"}},
            "head_leader": MANAGER,
        }
        path = groups.groups_path(self.root)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        os.chmod(path, 0o644)
        with self.assertRaises(groups.GroupError):
            groups.load_config(self.root)

    def test_load_groups_failsafe_on_unsafe(self):
        cfg = _cfg(head=REAL_UID)
        path = groups.save_config(cfg, root=self.root, editor_uid=REAL_UID)
        os.chmod(path, 0o666)
        self.assertEqual(groups.load_groups(self.root), {})  # fail-safe → empty, no crash

    def test_load_missing_is_empty(self):
        self.assertEqual(groups.load_config(self.root), {})


if __name__ == "__main__":
    unittest.main()
