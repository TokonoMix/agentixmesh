"""XU-RT-06 — defense-in-depth for the same-uid `auto` shortcut (flagged by a consensus review).

The consensus review flagged that `trust.resolve` reaches `auto` via `sender_uid == my_uid` BEFORE the
cross-user clamp, guarded only by a *comment*-contract that the caller passes the kernel-verified
`owner_uid`. All live callers (inject.py, approve.py) do pass the fstat-verified uid — but the guarantee
lived in prose, not code. This suite pins the hardened contract: the same-uid `auto` path now requires the
caller to EXPLICITLY assert verification (`sender_verified=True`); without it the resolver is fail-safe
(never silently `auto`). A future caller that forgets degrades to human-gate, not to autonomous action.
"""

import os
import unittest

from pm_mesh import trust

MY_UID = os.geteuid()
OTHER_UID = MY_UID + 1000  # a different (cross-user) principal


class SameUidAutoRequiresVerificationTest(unittest.TestCase):
    def test_same_uid_without_verification_is_failsafe_not_auto(self):
        # The dangerous path: sender_uid == my_uid but the caller did NOT assert kernel-verification.
        # Fail-safe: must NOT be auto. (Was: unconditionally auto — the comment-only contract.)
        level = trust.resolve({}, MY_UID, "gallery", MY_UID)
        self.assertNotEqual(level, trust.AUTO, "same-uid without verification must not silently auto")
        self.assertEqual(level, trust.HUMAN_GATE)

    def test_same_uid_with_verification_is_auto(self):
        # The legitimate own-session path: an fstat-verified same-uid sender still resolves to auto.
        level = trust.resolve({}, MY_UID, "gallery", MY_UID, sender_verified=True)
        self.assertEqual(level, trust.AUTO)

    def test_cross_user_never_auto_even_when_verified_flag_set(self):
        # sender_verified only unlocks the genuine same-uid path; it can NEVER grant a cross-user uid auto.
        level = trust.resolve({}, OTHER_UID, "gallery", MY_UID, sender_verified=True)
        self.assertEqual(level, trust.HUMAN_GATE)

    def test_cross_user_with_auto_policy_and_verified_flag_still_clamped(self):
        # Belt-and-suspenders: even a malicious policy granting the foreign uid auto, with the verified
        # flag set, is still floored — the engine clamp is independent of the same-uid shortcut.
        pol = {"by_uid": {str(OTHER_UID): trust.AUTO}}
        level = trust.resolve(pol, OTHER_UID, "gallery", MY_UID, sender_verified=True)
        self.assertEqual(level, trust.HUMAN_GATE)


class LiveCallersAssertVerificationTest(unittest.TestCase):
    """The two security-relevant callers must pass sender_verified=True (they hold the fstat owner_uid);
    otherwise same-user delivery would fail-safe to human-gate and the own-session auto path would break."""

    def test_inject_passes_sender_verified(self):
        import inspect
        from pm_mesh import inject
        src = inspect.getsource(inject)
        self.assertIn("sender_verified=True", src,
                      "inject.py must assert verification when resolving its fstat owner_uid")

    def test_approve_passes_sender_verified(self):
        import inspect
        from pm_mesh import approve
        src = inspect.getsource(approve)
        self.assertIn("sender_verified=True", src,
                      "approve.py must assert verification when resolving its fstat owner_uid")


if __name__ == "__main__":
    unittest.main()
