"""Consent CLI (f2-14 follow-up) — one command for leader-read consent instead of hand-JSON + chmod.

``grant_consent`` writes a valid artifact owned by the subject itself so that
``leader_read_allowed`` accepts it (round-trip). The module CLI (``grant``/``revoke``/``status``)
matches the existing module-CLI style (``def main(argv=None)``). Temp root as ``MESH_ROOT`` — never
touch ``/srv/mesh``.
"""

import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from pm_mesh import consent

LEADER = 1001


class GrantConsentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.uid = os.getuid()

    def test_grant_then_leader_read_allowed_round_trip(self):
        path = consent.grant_consent(LEADER, subject_uid=self.uid, root=self.root)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(consent.leader_read_allowed(self.uid, root=self.root))

    def test_written_mode_is_640_and_owned_by_self(self):
        path = consent.grant_consent(LEADER, subject_uid=self.uid, root=self.root)
        st = os.stat(path)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)
        self.assertEqual(st.st_uid, self.uid)

    def test_leader_uid_filter_matches_and_rejects(self):
        consent.grant_consent(LEADER, subject_uid=self.uid, root=self.root)
        self.assertTrue(
            consent.leader_read_allowed(self.uid, root=self.root, leader_uid=LEADER)
        )
        self.assertFalse(
            consent.leader_read_allowed(self.uid, root=self.root, leader_uid=9999)
        )

    def test_revoke_makes_leader_read_false_again(self):
        consent.grant_consent(LEADER, subject_uid=self.uid, root=self.root)
        self.assertTrue(consent.leader_read_allowed(self.uid, root=self.root))
        self.assertTrue(consent.revoke_consent(self.uid, root=self.root))
        self.assertFalse(consent.leader_read_allowed(self.uid, root=self.root))

    def test_expired_consent_is_rejected(self):
        consent.grant_consent(
            LEADER,
            subject_uid=self.uid,
            root=self.root,
            expires_utc="2000-01-01T00:00:00Z",
        )
        self.assertFalse(consent.leader_read_allowed(self.uid, root=self.root))

    def test_grant_creates_missing_consent_dir(self):
        # The temp root has no consent/ subdir yet; grant creates it.
        self.assertFalse(os.path.isdir(consent.consent_dir(self.root)))
        consent.grant_consent(LEADER, subject_uid=self.uid, root=self.root)
        self.assertTrue(os.path.isdir(consent.consent_dir(self.root)))

    def test_grant_is_overwritable_regrant(self):
        consent.grant_consent(LEADER, subject_uid=self.uid, root=self.root)
        # Re-grant with a different leader may overwrite the existing artifact.
        consent.grant_consent(2002, subject_uid=self.uid, root=self.root)
        self.assertTrue(
            consent.leader_read_allowed(self.uid, root=self.root, leader_uid=2002)
        )

    def test_content_fields_present(self):
        import json

        path = consent.grant_consent(
            LEADER, subject_uid=self.uid, root=self.root, now=datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
        )
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["subject_uid"], self.uid)
        self.assertEqual(data["leader_uid"], LEADER)
        self.assertIs(data["granted"], True)
        self.assertIs(data["revoked"], False)
        self.assertEqual(data["ts_utc"], "2026-06-30T12:00:00Z")
        self.assertIsNone(data["expires_utc"])
        self.assertEqual(data["signed_by"], str(self.uid))
        self.assertIsInstance(data["confirmation"], str)
        self.assertTrue(data["confirmation"])


class ConsentCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.uid = os.getuid()
        patcher = mock.patch.dict(os.environ, {"MESH_ROOT": self.root}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = consent.main(argv)
        return rc, buf.getvalue()

    def test_cli_grant_returns_zero_and_enables_leader_read(self):
        rc, out = self._run(
            ["grant", "--leader-uid", str(LEADER), "--subject-uid", str(self.uid)]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(consent.leader_read_allowed(self.uid, root=self.root))
        self.assertIn("True", out)

    def test_cli_status_reports_state(self):
        self._run(["grant", "--leader-uid", str(LEADER), "--subject-uid", str(self.uid)])
        rc, out = self._run(["status", "--subject-uid", str(self.uid)])
        self.assertEqual(rc, 0)
        self.assertIn("True", out)

    def test_cli_revoke_disables_leader_read(self):
        self._run(["grant", "--leader-uid", str(LEADER), "--subject-uid", str(self.uid)])
        rc, out = self._run(["revoke", "--subject-uid", str(self.uid)])
        self.assertEqual(rc, 0)
        self.assertFalse(consent.leader_read_allowed(self.uid, root=self.root))

    def test_cli_status_after_revoke_false(self):
        self._run(["grant", "--leader-uid", str(LEADER), "--subject-uid", str(self.uid)])
        self._run(["revoke", "--subject-uid", str(self.uid)])
        rc, out = self._run(["status", "--subject-uid", str(self.uid)])
        self.assertEqual(rc, 0)
        self.assertIn("False", out)


if __name__ == "__main__":
    unittest.main()
