"""Leader-read consent (f2-14, F4): fail-closed default OFF; valid subject-owned artifact = ON.

Obtaining the subject's actual signature is human-gated (parked); this test covers the template +
the fail-closed gate + the revocation coupling with mesh revoke.
"""

import json
import os
import tempfile
import unittest

from pm_mesh import consent, groups, maildir, message, revoke

REAL_UID = os.geteuid()           # = subject (the file owner in the test)
SUBJECT = REAL_UID
LEADER = REAL_UID + 90


def _write_consent(root, subject_uid=SUBJECT, leader_uid=LEADER, granted=True, revoked=False,
                   expires=None, mode=0o600):
    d = consent.consent_dir(root)
    os.makedirs(d, exist_ok=True)
    rec = {
        "subject_uid": subject_uid,
        "leader_uid": leader_uid,
        "granted": granted,
        "revoked": revoked,
        "ts_utc": "2026-06-26T10:00:00Z",
        "expires_utc": expires,
        "signed_by": "bob",
    }
    path = consent.consent_path(subject_uid, root)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.chmod(path, mode)
    return path


class LeaderReadGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_default_is_off_no_artifact(self):
        self.assertFalse(consent.leader_read_allowed(SUBJECT, root=self.root))

    def test_valid_consent_allows(self):
        _write_consent(self.root)
        self.assertTrue(consent.leader_read_allowed(SUBJECT, root=self.root, leader_uid=LEADER))

    def test_revoked_consent_is_off(self):
        _write_consent(self.root, revoked=True)
        self.assertFalse(consent.leader_read_allowed(SUBJECT, root=self.root))

    def test_not_granted_is_off(self):
        _write_consent(self.root, granted=False)
        self.assertFalse(consent.leader_read_allowed(SUBJECT, root=self.root))

    def test_wrong_leader_is_off(self):
        _write_consent(self.root, leader_uid=LEADER)
        self.assertFalse(consent.leader_read_allowed(SUBJECT, root=self.root, leader_uid=LEADER + 7))

    def test_expired_consent_is_off(self):
        _write_consent(self.root, expires="2000-01-01T00:00:00Z")
        self.assertFalse(consent.leader_read_allowed(SUBJECT, root=self.root, now=4_000_000_000.0))

    def test_future_expiry_allows(self):
        _write_consent(self.root, expires="2999-01-01T00:00:00Z")
        self.assertTrue(consent.leader_read_allowed(SUBJECT, root=self.root, now=1_000_000_000.0))

    def test_group_writable_artifact_rejected(self):
        _write_consent(self.root, mode=0o664)
        self.assertFalse(consent.leader_read_allowed(SUBJECT, root=self.root))

    def test_wrong_owner_rejected(self):
        # Artifact NOT owned by the subject → ignored (subject_uid diverges from owner).
        _write_consent(self.root, subject_uid=SUBJECT)
        # ask for consent for a different subject uid (whose owner doesn't match)
        self.assertFalse(consent.leader_read_allowed(SUBJECT + 12345, root=self.root))


class RevokeCouplingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # groups.json with REAL_UID as head leader (revoker) + a group with SUBJECT.
        cfg = {
            "groups": {"g": {"members": [f"{SUBJECT}:a", f"{REAL_UID + 6}:b"], "manager": f"{REAL_UID + 6}:b"}},
            "head_leader": REAL_UID,
        }
        path = groups.groups_path(self.root)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        os.chmod(path, 0o644)

    def test_revoke_clears_consent(self):
        _write_consent(self.root, subject_uid=SUBJECT)
        self.assertTrue(consent.leader_read_allowed(SUBJECT, root=self.root))
        summary = revoke.revoke(SUBJECT, root=self.root, revoker_uid=REAL_UID)
        self.assertTrue(summary["consent_revoked"])
        self.assertFalse(consent.leader_read_allowed(SUBJECT, root=self.root))


class DocsTest(unittest.TestCase):
    def test_consent_template_present(self):
        path = os.path.join(os.path.dirname(__file__), "pm_mesh", "LEADER-READ-CONSENT.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("PARKED", text)
        self.assertIn("mesh revoke", text)
        self.assertIn("subject_uid", text)

    def test_operating_has_fase2_privacy_section(self):
        path = os.path.join(os.path.dirname(__file__), "OPERATING.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("leader-read", text)
        self.assertIn("accountability", text.lower())


if __name__ == "__main__":
    unittest.main()
