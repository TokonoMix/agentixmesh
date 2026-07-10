"""Trust policy engine (f2-03): uid-keyed, uid:project restrict-only, cross-user default human-gate.

The security-load-bearing invariants (not the exact leader-gate position):
  * ``auto`` is strictly the loosest; **no cross-user path ever reaches ``auto``** — the engine clamp
    (``_clamp_cross_user``) floors a foreign-uid auto to ``human-gate`` (condition 1, §18);
  * ``uid:project`` is monotone-restrict-only (``rank[proj] >= rank[uid]`` or it is ignored);
  * an insecure/corrupt policy file → ``human-gate`` (fail-closed), no crash, entries fully ignored.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from pm_mesh import trust

MY_UID = 1000


class ResolveTest(unittest.TestCase):
    def test_same_uid_is_auto(self):
        # Own session → auto, regardless of policy — but only with the kernel-verified assertion
        # (sender_verified=True); the own-session auto path is defense-in-depth gated.
        self.assertEqual(trust.resolve({}, MY_UID, "gallery", MY_UID, sender_verified=True), trust.AUTO)
        # Even a (absurd) block on the own uid does not change that.
        pol = {"by_uid": {str(MY_UID): trust.BLOCK}}
        self.assertEqual(trust.resolve(pol, MY_UID, "gallery", MY_UID, sender_verified=True), trust.AUTO)

    def test_unknown_cross_user_is_human_gate(self):
        # Unknown cross-user uid → human-gate (condition 1: NEVER auto by default).
        self.assertEqual(trust.resolve({}, 2002, "gallery", MY_UID), trust.HUMAN_GATE)

    def test_cross_user_auto_is_clamped_to_human_gate(self):
        # Condition 1 (engine-HARD): even if the receiver sets `auto` at uid level for a foreign
        # uid, that is floored to human-gate. Cross-user auto-handling is made impossible.
        pol = {"by_uid": {"2002": trust.AUTO}}
        with mock.patch("pm_mesh.trust._log"):
            self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.HUMAN_GATE)

    def test_cross_user_notify_only_is_kept(self):
        # The clamp affects ONLY `auto`; a deliberately-set notify-only (display-only, never autonomous)
        # simply stays notify-only cross-user — f2-11 remains usable.
        pol = {"by_uid": {"2002": trust.NOTIFY_ONLY}}
        self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.NOTIFY_ONLY)

    def test_uid_project_cannot_elevate_to_auto(self):
        # uid:project=auto on top of default human-gate → ignored, stays human-gate (F1).
        pol = {"by_uid_project": {"2002:gallery": trust.AUTO}}
        with mock.patch("pm_mesh.trust._log"):
            self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.HUMAN_GATE)

    def test_uid_project_cannot_elevate_above_uid_level(self):
        # uid=block, uid:project=auto → override ignored, stays block.
        pol = {"by_uid": {"2002": trust.BLOCK}, "by_uid_project": {"2002:gallery": trust.AUTO}}
        with mock.patch("pm_mesh.trust._log"):
            self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.BLOCK)

    def test_uid_project_notify_only_cannot_downgrade_human_gate(self):
        # SECURITY: notify-only (passes through without a hold) must not downgrade a human-gate uid.
        pol = {"by_uid": {"2002": trust.HUMAN_GATE}, "by_uid_project": {"2002:gallery": trust.NOTIFY_ONLY}}
        with mock.patch("pm_mesh.trust._log"):
            self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.HUMAN_GATE)

    def test_uid_project_can_restrict(self):
        # uid=human-gate, uid:project=block → more restrictive, so honored.
        pol = {"by_uid": {"2002": trust.HUMAN_GATE}, "by_uid_project": {"2002:gallery": trust.BLOCK}}
        self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.BLOCK)

    def test_uid_project_equal_rank_honored(self):
        pol = {"by_uid": {"2002": trust.HUMAN_GATE}, "by_uid_project": {"2002:gallery": trust.HUMAN_GATE}}
        self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.HUMAN_GATE)

    def test_cross_user_auto_clamped_but_project_restrict_still_applies(self):
        # uid=auto is floored to human-gate (clamp); a more restrictive project stays more restrictive.
        pol = {"by_uid": {"2002": trust.AUTO}, "by_uid_project": {"2002:risky": trust.BLOCK}}
        with mock.patch("pm_mesh.trust._log"):
            self.assertEqual(trust.resolve(pol, 2002, "risky", MY_UID), trust.BLOCK)        # restrict honored
            self.assertEqual(trust.resolve(pol, 2002, "safe", MY_UID), trust.HUMAN_GATE)    # auto → clamp

    def test_unknown_level_string_ignored(self):
        pol = {"by_uid": {"2002": "totally-bogus"}}
        with mock.patch("pm_mesh.trust._log"):
            self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.HUMAN_GATE)


class LoadPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "policy.json")

    def _write(self, data, mode=0o600):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(self.path, mode)

    def test_missing_file_is_empty_policy(self):
        self.assertEqual(trust.load_policy(self.path), {})

    def test_valid_0600_loads(self):
        self._write({"by_uid": {"2002": "auto"}}, mode=0o600)
        self.assertEqual(trust.load_policy(self.path), {"by_uid": {"2002": "auto"}})

    def test_group_writable_rejected(self):
        self._write({"by_uid": {"2002": "auto"}}, mode=0o620)
        with self.assertRaises(trust.TrustError):
            trust.load_policy(self.path)

    def test_other_readable_rejected(self):
        self._write({"by_uid": {"2002": "auto"}}, mode=0o604)
        with self.assertRaises(trust.TrustError):
            trust.load_policy(self.path)

    def test_wrong_owner_rejected(self):
        self._write({"by_uid": {"2002": "auto"}}, mode=0o600)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 4242):
            with self.assertRaises(trust.TrustError):
                trust.load_policy(self.path)

    def test_symlink_rejected(self):
        target = os.path.join(self.tmp.name, "real.json")
        with open(target, "w", encoding="utf-8") as fh:
            json.dump({"by_uid": {}}, fh)
        os.chmod(target, 0o600)
        os.symlink(target, self.path)
        with self.assertRaises(trust.TrustError):
            trust.load_policy(self.path)

    def test_corrupt_json_raises(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        os.chmod(self.path, 0o600)
        with self.assertRaises(ValueError):
            trust.load_policy(self.path)


class LoadPolicyOrDefaultTest(unittest.TestCase):
    """Fail-closed wrapper: any error → safe default, no crash, insecure entries ignored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "policy.json")

    def test_bad_perms_policy_with_auto_entries_is_fully_ignored(self):
        # THE fail-closed attack: a group-writable file full of auto entries must elevate NOTHING.
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"by_uid": {"2002": "auto"}}, fh)
        os.chmod(self.path, 0o666)
        with mock.patch("pm_mesh.trust._log"):
            pol = trust.load_policy_or_default(self.path)
        self.assertEqual(pol, {})  # nothing taken over from the insecure file
        # …and resolve falls back to human-gate for that sender.
        self.assertEqual(trust.resolve(pol, 2002, "gallery", MY_UID), trust.HUMAN_GATE)

    def test_corrupt_json_safe_default(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("}{")
        os.chmod(self.path, 0o600)
        with mock.patch("pm_mesh.trust._log"):
            self.assertEqual(trust.load_policy_or_default(self.path), {})

    def test_missing_safe_default(self):
        self.assertEqual(trust.load_policy_or_default(self.path), {})


if __name__ == "__main__":
    unittest.main()
