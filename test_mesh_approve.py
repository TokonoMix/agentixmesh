"""``mesh approve <id>`` (f2-05) — Design B (release spool): gate release by the receiver.

After approve, the kernel-verified bytes are staged into ``release/``; the next inject
drains the spool and shows the full body (§3.2). No more rename to ``new/``.

TDD additions for §9.1 (verified-bytes threading), §9.11 (BLOCK-reject), §9.2 (stale claim),
and the cross-user path that was previously missing.
"""

import datetime
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from pm_mesh import approve, audit, inject, maildir, message, release, trust

REAL_UID = os.geteuid()
ADDR = f"{REAL_UID}:gallery"
MARKER = "APPROVE-BODY-MARKER-7c1a"

FOREIGN_UID = REAL_UID + 7777  # kernel-foreign sender


def _old_iso(days=3):
    """A UTC ``ts_utc`` stamp ``days`` days in the past (older than ``max_age_s`` = 1 day)."""
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_held(root, body=MARKER, thread="t-h", sender_uid=None):
    """Deliver a message and park it in held/ (simulate the gate). Return the msg object."""
    if sender_uid is None:
        sender_uid = REAL_UID
    maildir.maildrop(ADDR, root=root)
    msg = message.new_message(ADDR, body, thread=thread, from_=f"{sender_uid}:peer")
    path = maildir.deliver(msg, root=root)
    held_path = os.path.join(root, ADDR, "held", os.path.basename(path))
    os.rename(path, held_path)
    return msg


def _count(root, sub):
    d = os.path.join(root, ADDR, sub)
    try:
        return len([n for n in os.listdir(d) if not n.startswith(".") and not n.endswith(".shown")])
    except FileNotFoundError:
        return 0


def _run_inject(root):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("os.getuid", return_value=REAL_UID), \
         mock.patch("os.getcwd", return_value="/home/user/gallery"), \
         mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
        with redirect_stdout(out), redirect_stderr(err):
            inject.main()
    return out.getvalue()


class ApproveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    # --- Existing tests updated for Design B ---

    def test_receiver_approve_stages_to_release_not_new(self):
        """Design B: approve stages to release/, NOT new/; held is removed."""
        msg = _setup_held(self.root)
        self.assertEqual(_count(self.root, "held"), 1)
        result = approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)
        # held is gone
        self.assertEqual(_count(self.root, "held"), 0)
        # NO file in new/ (Design B invariant: approve never touches new/)
        self.assertEqual(_count(self.root, "new"), 0)
        # release/ entry exists
        rdir = release.release_dir(ADDR, root=self.root)
        entries = [n for n in os.listdir(rdir) if not n.endswith(".taken")]
        self.assertEqual(len(entries), 1, "release/ spool entry must exist after approve")

    def test_next_inject_shows_body_after_approve(self):
        """After approve + inject drain, the full body must appear."""
        msg = _setup_held(self.root)
        approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)
        out = _run_inject(self.root)
        self.assertIn(MARKER, out, "inject must drain release/ and show the full body")

    def test_unauthorized_uid_rejected_nothing_staged(self):
        """Unauthorized approver: no stage in release/, held still present."""
        msg = _setup_held(self.root)
        with self.assertRaises(approve.ApproveError):
            approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID + 7777)
        # held unchanged
        self.assertEqual(_count(self.root, "held"), 1)
        # nothing in new/
        self.assertEqual(_count(self.root, "new"), 0)
        # nothing staged in release/
        rdir = release.release_dir(ADDR, root=self.root)
        if os.path.isdir(rdir):
            entries = [n for n in os.listdir(rdir) if not n.endswith(".taken")]
            self.assertEqual(len(entries), 0)

    def test_unknown_id_raises(self):
        _setup_held(self.root)
        with self.assertRaises(approve.ApproveError):
            approve.approve("does-not-exist", root=self.root, address=ADDR, approver_uid=REAL_UID)

    def test_audit_entry_written(self):
        msg = _setup_held(self.root)
        approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)
        with open(audit.audit_path(self.root), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("approve", log)
        self.assertIn(msg.id, log)

    def test_manager_path_is_failclosed_hook(self):
        # Until f2-08/09, NO non-receiver may approve, not even as "manager".
        self.assertFalse(approve._manager_can_approve("leader-gate", REAL_UID + 1, REAL_UID, ADDR))

    # --- New tests for Design B / §9 contracts ---

    def test_block_reject_raises_nothing_staged(self):
        """§9.11 (consensus-4): BLOCK verdict ⇒ ApproveError; no release/ entry; held untouched."""
        msg = _setup_held(self.root)
        with mock.patch("pm_mesh.trust.resolve", return_value=trust.BLOCK), \
             mock.patch("pm_mesh.trust.load_policy_or_default", return_value={}):
            with self.assertRaises(approve.ApproveError) as ctx:
                approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)
        self.assertIn("blocked", str(ctx.exception).lower())
        # held is still there
        self.assertEqual(_count(self.root, "held"), 1)
        # nothing staged
        rdir = release.release_dir(ADDR, root=self.root)
        if os.path.isdir(rdir):
            entries = [n for n in os.listdir(rdir) if not n.endswith(".taken")]
            self.assertEqual(len(entries), 0)

    def test_verified_bytes_threaded_not_reread(self):
        """§9.1: the bytes returned by identity.read_verified are staged verbatim; mutating
        the held file after read_verified does NOT affect what is staged."""
        msg = _setup_held(self.root)

        # Capture the real held path before approve
        held_dir = os.path.join(self.root, ADDR, "held")
        held_files = [f for f in os.listdir(held_dir) if not f.startswith(".")]
        self.assertEqual(len(held_files), 1)

        original_bytes = message.to_json(msg).encode("utf-8")

        # We will intercept identity.read_verified to record the bytes returned,
        # then mutate the file, and verify the staged envelope has the original bytes.
        real_read_verified = __import__("pm_mesh.identity", fromlist=["read_verified"]).read_verified
        captured_bytes = {}

        def patched_read_verified(path):
            data, uid = real_read_verified(path)
            captured_bytes["data"] = data
            captured_bytes["uid"] = uid
            return data, uid

        with mock.patch("pm_mesh.identity.read_verified", side_effect=patched_read_verified):
            approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)

        # After approve, held is gone but release/ exists — read the envelope
        rdir = release.release_dir(ADDR, root=self.root)
        entries = [n for n in os.listdir(rdir)]
        self.assertEqual(len(entries), 1)
        with open(os.path.join(rdir, entries[0]), encoding="utf-8") as fh:
            envelope = json.load(fh)

        staged_msg_bytes = envelope["msg"].encode("utf-8")
        # The staged bytes match the original returned by read_verified
        self.assertEqual(staged_msg_bytes, captured_bytes["data"])

    def test_held_removed_after_stage_no_new_file(self):
        """After approve: held file GONE, no file in new/, release/ entry present (covers §3.1 steps 3-5)."""
        msg = _setup_held(self.root)
        approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)

        self.assertEqual(_count(self.root, "held"), 0, "held must be removed after stage")
        self.assertEqual(_count(self.root, "new"), 0, "approve must NOT write to new/")

        rdir = release.release_dir(ADDR, root=self.root)
        entries = [n for n in os.listdir(rdir) if not n.endswith(".taken")]
        self.assertGreater(len(entries), 0, "release/ spool entry must exist")

    def test_inject_dedup_no_double_show(self):
        """Second inject after body is shown does NOT re-show it; spool entry gone."""
        msg = _setup_held(self.root)
        approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)

        out1 = _run_inject(self.root)
        self.assertIn(MARKER, out1)

        out2 = _run_inject(self.root)
        self.assertNotIn(MARKER, out2, "second inject must not re-show the body (dedup)")

    def test_inject_dedup_spool_entry_gone_after_drain(self):
        """After inject drains the spool, the release/ entry is discarded."""
        msg = _setup_held(self.root)
        approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)

        rdir = release.release_dir(ADDR, root=self.root)
        before = [n for n in os.listdir(rdir)]
        self.assertGreater(len(before), 0)

        _run_inject(self.root)

        after = [n for n in os.listdir(rdir)]
        self.assertEqual(len(after), 0, "spool entry must be discarded after drain")

    def test_block_check_is_before_any_stage(self):
        """BLOCK check happens before any authz or write — stage is never called for BLOCK."""
        msg = _setup_held(self.root)

        stage_called = []

        with mock.patch("pm_mesh.trust.resolve", return_value=trust.BLOCK), \
             mock.patch("pm_mesh.trust.load_policy_or_default", return_value={}), \
             mock.patch("pm_mesh.release.stage", side_effect=lambda *a, **kw: stage_called.append(a)):
            with self.assertRaises(approve.ApproveError):
                approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)

        self.assertEqual(stage_called, [], "release.stage must not be called when trust level is BLOCK")

    # --- Cross-user (foreign-uid) end-to-end: the path the fix actually exists for (§9.7 / §6.1) ---

    def test_cross_user_approve_drain_shows_body_and_foreign_uid(self):
        """§9.7 / §6.1 — the regression the AUTO test cannot catch.

        Force a GENUINE cross-user resolve: ``identity.read_verified`` reports a FOREIGN owner at
        approve-time, so ``trust.resolve`` returns ``human-gate`` (not AUTO) and the spool stores
        ``owner_uid = FOREIGN_UID``. The next inject drain must (a) release the full body and
        (b) attribute the FOREIGN kernel uid in the frame — the stored ``owner_uid`` is rendered
        without re-verifying kernel ownership (§9.6), which the same-uid AUTO test structurally
        cannot exercise (both uids are REAL_UID).
        """
        msg = _setup_held(self.root)  # file kernel-owned by REAL_UID
        verified = message.to_json(msg).encode("utf-8")

        # Mock only spans approve; the drain never calls read_verified. This makes _held_level
        # resolve human-gate while the spool dir on disk stays genuinely receiver-owned (0700).
        with mock.patch("pm_mesh.identity.read_verified", return_value=(verified, FOREIGN_UID)):
            level, owner_uid, _ = approve._held_level(
                os.path.join(self.root, ADDR, "held",
                             [f for f in os.listdir(os.path.join(self.root, ADDR, "held"))
                              if not f.startswith(".")][0]),
                REAL_UID,
            )
            self.assertEqual(level, trust.HUMAN_GATE,
                             "sanity: a FOREIGN owner must resolve to human-gate, not AUTO")
            self.assertEqual(owner_uid, FOREIGN_UID)
            approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)

        out = _run_inject(self.root)
        self.assertIn(MARKER, out, "cross-user approve must release the body via the drain")
        self.assertIn(f"owner_uid={FOREIGN_UID}", out,
                      "frame must attribute the FOREIGN kernel uid stored at approve-time (§9.6)")

    def test_drain_render_error_preserves_entry_no_silent_loss(self):
        """Consensus 2026-07-01 (Opus MEDIUM-2): if frame.render raises for one drained entry, that
        approved body must NOT be discarded (it stays for stale-reclaim); other entries still show."""
        marker_a, marker_b = "BODY-A-keep-on-error", "BODY-B-shows-fine"
        msg_a = _setup_held(self.root, body=marker_a, thread="t-a")
        msg_b = _setup_held(self.root, body=marker_b, thread="t-b")
        approve.approve(msg_a.id, root=self.root, address=ADDR, approver_uid=REAL_UID)
        approve.approve(msg_b.id, root=self.root, address=ADDR, approver_uid=REAL_UID)

        real_render = __import__("pm_mesh.frame", fromlist=["render"]).render

        def flaky_render(msg, owner_uid):
            if marker_a in msg.body:
                raise RuntimeError("simulated render failure")
            return real_render(msg, owner_uid)

        with mock.patch("pm_mesh.frame.render", side_effect=flaky_render):
            out = _run_inject(self.root)

        self.assertIn(marker_b, out, "a healthy entry must still be shown")
        self.assertNotIn(marker_a, out, "the failing entry must not be shown this turn")
        # the failing entry is NOT discarded — it remains (as a claimed .taken) for stale-reclaim
        rdir = release.release_dir(ADDR, root=self.root)
        remaining = os.listdir(rdir)
        self.assertEqual(len(remaining), 1, "the un-shown approved body must be preserved, not lost")
        self.assertTrue(remaining[0].endswith(".taken"))

    def test_old_message_body_shows_after_approve(self):
        """§6.2 / §9.10 — a deliberately-approved message shows REGARDLESS of age.

        The drain deduplicates (``is_seen``) but MUST NOT age-gate (``is_fresh``): approval *is* the
        freshness decision (invariant 7). A >24h ``ts_utc`` must still render. This guards
        inject.py's drain against any age-gate silently creeping back into the release path — a
        gap the unit-level ``SeenStore.is_seen`` test cannot see (it never runs the drain).
        """
        maildir.maildrop(ADDR, root=self.root)
        msg = message.new_message(ADDR, MARKER, thread="t-old", from_=f"{REAL_UID}:peer")
        msg.ts_utc = _old_iso(days=3)  # older than max_age_s (86400)
        path = maildir.deliver(msg, root=self.root)
        held_path = os.path.join(self.root, ADDR, "held", os.path.basename(path))
        os.rename(path, held_path)

        approve.approve(msg.id, root=self.root, address=ADDR, approver_uid=REAL_UID)
        out = _run_inject(self.root)
        self.assertIn(
            MARKER, out,
            "an old (>24h) approved message must still be released by the drain (is_seen, not is_fresh)",
        )


class ApproveCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("os.getuid", return_value=REAL_UID), \
             mock.patch("os.getcwd", return_value="/home/user/gallery"), \
             mock.patch.dict(os.environ, {"MESH_ROOT": self.root}, clear=False):
            with redirect_stdout(out), redirect_stderr(err):
                rc = approve.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_cli_success(self):
        msg = _setup_held(self.root)
        rc, out, err = self._cli(msg.id)
        self.assertEqual(rc, 0)
        self.assertIn("released", out)

    def test_cli_unknown_id_clean_error(self):
        _setup_held(self.root)
        rc, out, err = self._cli("nope")
        self.assertEqual(rc, 1)
        self.assertIn("mesh-approve", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
