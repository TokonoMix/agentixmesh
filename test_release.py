"""Tests for pm_mesh/release.py — TDD, unittest, tmp dirs only.

Covers:
- stage writes a 0600 O_EXCL entry with token_hex name + correct envelope
- stage on a colliding name raises FileExistsError
- drain yields (path, owner_uid, msg) and round-trips the message
- drain fail-closed: missing dir, not 0700, owner-mismatch, symlink
- drain discards+skips malformed envelope (bad JSON, non-int owner_uid, missing msg)
- two stages produce distinct names
- stale .taken (>300s) re-processed; fresh .taken not double-drained
- discard unlinks
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from pm_mesh import config, message, release
from pm_mesh.message import new_message, to_json


def _make_msg(to="1100:proj") -> message.Message:
    return new_message(to=to, body="hello", from_="1200:other")


def _stage_msg(address, owner_uid, msg, root):
    verified_bytes = to_json(msg).encode("utf-8")
    return release.stage(address, owner_uid, verified_bytes, root=root)


class TestReleaseDir(unittest.TestCase):
    def test_returns_correct_path(self):
        with tempfile.TemporaryDirectory() as root:
            addr = "1100:proj"
            expected = os.path.join(root, addr, "release")
            self.assertEqual(release.release_dir(addr, root=root), expected)


class TestStage(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.address = f"{os.getuid()}:proj"

    def tearDown(self):
        self._td.cleanup()

    def test_creates_release_dir_0700(self):
        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()
        release.stage(self.address, os.getuid(), verified, root=self.root)
        rdir = release.release_dir(self.address, root=self.root)
        st = os.stat(rdir)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)

    def test_entry_mode_0600(self):
        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()
        entry = release.stage(self.address, os.getuid(), verified, root=self.root)
        st = os.stat(entry)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_entry_name_is_token_hex_32chars(self):
        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()
        entry = release.stage(self.address, os.getuid(), verified, root=self.root)
        name = os.path.basename(entry)
        self.assertEqual(len(name), 32)
        int(name, 16)  # should not raise

    def test_envelope_content_correct(self):
        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()
        owner_uid = os.getuid()
        entry = release.stage(self.address, owner_uid, verified, root=self.root)
        with open(entry) as f:
            envelope = json.load(f)
        self.assertEqual(envelope["owner_uid"], owner_uid)
        reconstructed = message.from_json(envelope["msg"])
        self.assertEqual(reconstructed.id, msg.id)
        self.assertEqual(reconstructed.body, msg.body)

    def test_two_stages_distinct_names(self):
        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()
        e1 = release.stage(self.address, os.getuid(), verified, root=self.root)
        e2 = release.stage(self.address, os.getuid(), verified, root=self.root)
        self.assertNotEqual(e1, e2)

    def test_collision_raises_file_exists_error(self):
        """Force a collision by pre-creating the target entry name."""
        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()

        fixed_token = secrets.token_hex(16)
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        # Pre-create the file to simulate a collision
        collision_path = os.path.join(rdir, fixed_token)
        open(collision_path, "w").close()

        with patch("secrets.token_hex", return_value=fixed_token):
            with self.assertRaises(FileExistsError):
                release.stage(self.address, os.getuid(), verified, root=self.root)

    def test_stage_normalizes_drifted_mode_so_drain_reads_it(self):
        """Consensus 2026-07-01 (Opus HIGH-1/2): if release/ exists receiver-owned but with a drifted
        group-permissive mode, stage must repair it to 0700 so drain (which requires 0700) still reads
        the staged body — otherwise an approved body is silently lost and left group-readable."""
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        os.chmod(rdir, 0o770)  # simulate drift to group-permissive
        self.assertEqual(stat.S_IMODE(os.stat(rdir).st_mode), 0o770)

        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()
        release.stage(self.address, os.getuid(), verified, root=self.root)

        # stage must have repaired the mode to 0700 (stage/drain agree)
        self.assertEqual(stat.S_IMODE(os.stat(rdir).st_mode), 0o700,
                         "stage must normalize a drifted release/ mode to 0700")
        # and drain must actually yield the entry (no silent loss)
        with patch.object(config, "parse_address", return_value=(os.getuid(), "proj")):
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(len(results), 1, "drain must read the staged entry after mode repair")

    def test_stage_preserves_original_verified_bytes(self):
        """Verify original bytes are staged (TOCTOU contract §9.1)."""
        msg = _make_msg(to=self.address)
        original_bytes = to_json(msg).encode()
        entry = release.stage(self.address, os.getuid(), original_bytes, root=self.root)
        with open(entry) as f:
            envelope = json.load(f)
        self.assertEqual(envelope["msg"].encode("utf-8"), original_bytes)


class TestDrain(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.uid = os.getuid()
        self.address = f"{self.uid}:proj"

    def tearDown(self):
        self._td.cleanup()

    def _patch_parse(self):
        """Patch parse_address to return (os.getuid(), 'proj') for our address."""
        return patch.object(
            config,
            "parse_address",
            return_value=(self.uid, "proj"),
        )

    def test_drain_yields_staged_entry(self):
        msg = _make_msg(to=self.address)
        verified = to_json(msg).encode()
        _stage_msg(self.address, self.uid, msg, self.root)
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(len(results), 1)
        path, owner_uid, got_msg = results[0]
        self.assertEqual(owner_uid, self.uid)
        self.assertEqual(got_msg.id, msg.id)
        self.assertEqual(got_msg.body, msg.body)

    def test_drain_missing_dir_yields_nothing(self):
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])

    def test_drain_wrong_mode_yields_nothing(self):
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o755, exist_ok=True)
        # stage something
        msg = _make_msg(to=self.address)
        _stage_msg(self.address, self.uid, msg, self.root)
        os.chmod(rdir, 0o755)  # wrong mode
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])

    def test_drain_drift_warns_on_stderr_but_stays_failclosed(self):
        """Consensus 2026-07-01 second pass (Opus): a fail-closed drift must not be SILENT.
        drain on a wrong-mode release/ yields nothing AND emits a visible stderr notice."""
        import io
        from contextlib import redirect_stderr
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        _stage_msg(self.address, self.uid, _make_msg(to=self.address), self.root)
        os.chmod(rdir, 0o750)  # drift
        err = io.StringIO()
        with self._patch_parse(), redirect_stderr(err):
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [], "drift must stay fail-closed (yield nothing)")
        self.assertIn("release-spool", err.getvalue())
        self.assertIn("WITHHELD", err.getvalue(), "the pause must be visible on stderr, not silent")

    def test_drain_owner_mismatch_yields_nothing(self):
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        msg = _make_msg(to=self.address)
        _stage_msg(self.address, self.uid, msg, self.root)
        # Mock parse_address to return a different uid
        with patch.object(config, "parse_address", return_value=(self.uid + 1, "proj")):
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])

    def test_drain_symlink_yields_nothing(self):
        # Create a real dir then replace with a symlink
        real_dir = os.path.join(self.root, "real_release")
        os.makedirs(real_dir, mode=0o700)
        sym_target = os.path.join(self.root, self.address, "release")
        os.makedirs(os.path.dirname(sym_target), exist_ok=True)
        os.symlink(real_dir, sym_target)
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])

    def test_drain_bad_json_discards_skips(self):
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        bad_entry = os.path.join(rdir, secrets.token_hex(16))
        with open(bad_entry, "w") as f:
            f.write("not-json{{{{")
        os.chmod(bad_entry, 0o600)
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])
        # The bad entry should be gone (discarded)
        self.assertFalse(os.path.exists(bad_entry))

    def test_drain_non_int_owner_uid_discards(self):
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        msg = _make_msg(to=self.address)
        envelope = json.dumps({"owner_uid": "notanint", "msg": to_json(msg)})
        entry = os.path.join(rdir, secrets.token_hex(16))
        with open(entry, "w") as f:
            f.write(envelope)
        os.chmod(entry, 0o600)
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])

    def test_drain_missing_msg_field_discards(self):
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        envelope = json.dumps({"owner_uid": self.uid})
        entry = os.path.join(rdir, secrets.token_hex(16))
        with open(entry, "w") as f:
            f.write(envelope)
        os.chmod(entry, 0o600)
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])

    def test_drain_negative_owner_uid_discards(self):
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        msg = _make_msg(to=self.address)
        envelope = json.dumps({"owner_uid": -1, "msg": to_json(msg)})
        entry = os.path.join(rdir, secrets.token_hex(16))
        with open(entry, "w") as f:
            f.write(envelope)
        os.chmod(entry, 0o600)
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])

    def test_drain_claimed_entry_removed(self):
        msg = _make_msg(to=self.address)
        _stage_msg(self.address, self.uid, msg, self.root)
        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(len(results), 1)
        claimed_path, _, _ = results[0]
        # The claimed path (.taken) should still exist (caller calls discard)
        # but the original should not
        rdir = release.release_dir(self.address, root=self.root)
        entries = os.listdir(rdir)
        # Only .taken should be present
        self.assertTrue(all(e.endswith(".taken") for e in entries))


class TestStaleRecovery(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.uid = os.getuid()
        self.address = f"{self.uid}:proj"

    def tearDown(self):
        self._td.cleanup()

    def _patch_parse(self):
        return patch.object(config, "parse_address", return_value=(self.uid, "proj"))

    def test_stale_taken_reprocessed(self):
        """A .taken entry older than 300s is re-drained exactly once."""
        msg = _make_msg(to=self.address)
        _stage_msg(self.address, self.uid, msg, self.root)

        rdir = release.release_dir(self.address, root=self.root)
        # Simulate a crash: manually rename to .taken and back-date mtime
        entries = [e for e in os.listdir(rdir) if not e.endswith(".taken")]
        self.assertEqual(len(entries), 1)
        original = os.path.join(rdir, entries[0])
        taken = original + ".taken"
        os.rename(original, taken)
        # Back-date to 400 seconds ago
        old_time = time.time() - 400
        os.utime(taken, (old_time, old_time))

        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(len(results), 1)
        _, _, got_msg = results[0]
        self.assertEqual(got_msg.id, msg.id)

    def test_fresh_taken_not_double_drained(self):
        """A recently-created .taken entry is NOT re-drained."""
        msg = _make_msg(to=self.address)
        _stage_msg(self.address, self.uid, msg, self.root)

        rdir = release.release_dir(self.address, root=self.root)
        entries = [e for e in os.listdir(rdir) if not e.endswith(".taken")]
        original = os.path.join(rdir, entries[0])
        taken = original + ".taken"
        os.rename(original, taken)
        # mtime is now (recent) — not stale

        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))
        self.assertEqual(results, [])


class TestStaleTakenMutualExclusion(unittest.TestCase):
    """Finding 1: stale .taken recovery must have mutual exclusion (no double-render)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.uid = os.getuid()
        self.address = f"{self.uid}:proj"

    def tearDown(self):
        self._td.cleanup()

    def _patch_parse(self):
        return patch.object(config, "parse_address", return_value=(self.uid, "proj"))

    def test_concurrent_drains_yield_stale_entry_exactly_once(self):
        """Two drain generators racing over a single stale .taken entry must yield
        the entry exactly once total — mutual exclusion via unique-target rename."""
        msg = _make_msg(to=self.address)
        _stage_msg(self.address, self.uid, msg, self.root)

        rdir = release.release_dir(self.address, root=self.root)
        # Simulate crash: rename to .taken and back-date mtime
        entries = [e for e in os.listdir(rdir) if not e.endswith(".taken")]
        self.assertEqual(len(entries), 1)
        original = os.path.join(rdir, entries[0])
        stale_taken = original + ".taken"
        os.rename(original, stale_taken)
        old_time = time.time() - 400
        os.utime(stale_taken, (old_time, old_time))

        # Advance both generators past the claim step by collecting results
        with self._patch_parse():
            gen1 = release.drain(self.address, root=self.root)
            gen2 = release.drain(self.address, root=self.root)
            results1 = list(gen1)
            results2 = list(gen2)

        total = len(results1) + len(results2)
        self.assertEqual(total, 1, f"Expected exactly 1 yield across both drains, got {total}")


class TestEnvelopeReadLimit(unittest.TestCase):
    """Finding 2: drain read bound must be sized for the double-escaped envelope."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.uid = os.getuid()
        self.address = f"{self.uid}:proj"

    def tearDown(self):
        self._td.cleanup()

    def _patch_parse(self):
        return patch.object(config, "parse_address", return_value=(self.uid, "proj"))

    def test_escape_heavy_body_near_max_roundtrips(self):
        """A body near MAX_BODY_BYTES made of escape-heavy chars (quotes/backslashes)
        must survive drain (not be silently discarded due to undersized read bound)."""
        from pm_mesh.message import MAX_BODY_BYTES

        # Build a body of ~30KB of backslashes and quotes — each char doubles in JSON
        escape_chars = '\\"' * (30 * 1024 // 2)  # 30KB raw, ~60KB+ when JSON-escaped
        body = escape_chars  # within MAX_BODY_BYTES (64KB)

        msg = message.new_message(to=self.address, body=body, from_="1200:other")
        verified_bytes = message.to_json(msg).encode("utf-8")
        # Verify the message body is within limits
        self.assertLessEqual(len(body.encode("utf-8")), MAX_BODY_BYTES)

        release.stage(self.address, self.uid, verified_bytes, root=self.root)

        with self._patch_parse():
            results = list(release.drain(self.address, root=self.root))

        self.assertEqual(len(results), 1, "Escape-heavy body near MAX_BODY_BYTES was silently discarded")
        _, _, got_msg = results[0]
        self.assertEqual(got_msg.body, body)


class TestStageSymlinkGuard(unittest.TestCase):
    """Finding 3: stage should lstat-revalidate release/ dir."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.uid = os.getuid()
        self.address = f"{self.uid}:proj"

    def tearDown(self):
        self._td.cleanup()

    def test_stage_rejects_symlink_release_dir(self):
        """If release/ is a symlink, stage must raise OSError (not write through it)."""
        # Create a real dir elsewhere and make release/ a symlink to it
        real_dir = os.path.join(self.root, "attacker_dir")
        os.makedirs(real_dir, mode=0o700)
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(os.path.dirname(rdir), exist_ok=True)
        os.symlink(real_dir, rdir)

        msg = _make_msg(to=self.address)
        verified = message.to_json(msg).encode("utf-8")

        with self.assertRaises(OSError):
            release.stage(self.address, self.uid, verified, root=self.root)

    def test_stage_rejects_wrong_owner_existing_release_dir(self):
        """If release/ exists but is owned by a different uid, stage must raise OSError."""
        rdir = release.release_dir(self.address, root=self.root)
        os.makedirs(rdir, mode=0o700, exist_ok=True)

        msg = _make_msg(to=self.address)
        verified = message.to_json(msg).encode("utf-8")

        # Mock lstat to report a different owner
        real_lstat = os.lstat

        def fake_lstat(path):
            st = real_lstat(path)
            if path == rdir:
                # Return a stat result with wrong uid by constructing via os.stat_result
                # We patch st_uid by using a MagicMock
                from unittest.mock import MagicMock
                fake_st = MagicMock()
                fake_st.st_mode = st.st_mode
                fake_st.st_uid = self.uid + 999  # wrong owner
                return fake_st
            return st

        with patch("os.lstat", side_effect=fake_lstat):
            with self.assertRaises(OSError):
                release.stage(self.address, self.uid, verified, root=self.root)


class TestDiscard(unittest.TestCase):
    def test_discard_unlinks(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        self.assertTrue(os.path.exists(path))
        release.discard(path)
        self.assertFalse(os.path.exists(path))

    def test_discard_swallows_oserror(self):
        # Should not raise even for nonexistent path
        release.discard("/nonexistent/path/abc123")
