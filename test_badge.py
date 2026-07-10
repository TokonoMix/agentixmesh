"""``mesh-badge`` (harness-independent status-bar indicator) — **strictly read-only**.

Counts only ``new/``+``held/`` of its own address (reuses ``status.gather`` for the counts)
and returns a short, status-bar-friendly line or a JSON object. ``senders`` contains ONLY
kernel-verified sender-uids (never project labels/subjects/body). Fail-closed: any error
(missing maildir, permission error, corrupt file) yields empty output + exit 0 — never a
traceback to stdout/stderr, unless ``--debug``.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pm_mesh import badge, identity, maildir, message

UID = 1000
ADDR = f"{UID}:gallery"


class _AsAddress:
    """Context manager: patch ``os.getuid``/``os.getcwd``/``$MESH_ROOT`` for ``ADDR``."""

    def __init__(self, root):
        self.root = root
        self._stack = []

    def __enter__(self):
        self._stack = [
            mock.patch("os.getuid", return_value=UID),
            mock.patch("os.getcwd", return_value="/home/user/gallery"),
            mock.patch.dict(os.environ, {"MESH_ROOT": self.root}, clear=False),
        ]
        for p in self._stack:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._stack):
            p.__exit__(*exc)


def as_address(root):
    return _AsAddress(root)


def _deliver_new(root, n, from_=f"{UID + 1}:peer"):
    for i in range(n):
        maildir.deliver(message.new_message(ADDR, f"m{i}", from_=from_), root=root)


def _deliver_held(root, n, from_=f"{UID + 2}:peer"):
    """Deliver + park directly in held/ (mirrors ``_setup_held`` in test_mesh_approve.py)."""
    for i in range(n):
        msg = message.new_message(ADDR, f"h{i}", from_=from_)
        path = maildir.deliver(msg, root=root)
        held_path = os.path.join(root, ADDR, "held", os.path.basename(path))
        os.rename(path, held_path)


class BadgeTextModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_text_mode_with_new_and_held(self):
        _deliver_new(self.root, 2)
        _deliver_held(self.root, 1)
        with as_address(self.root):
            out = badge.format_text(badge.gather(), emoji=True)
        self.assertEqual(out, "\U0001f4ec 2 · ⏸ 1")

    def test_text_mode_new_only(self):
        _deliver_new(self.root, 3)
        with as_address(self.root):
            out = badge.format_text(badge.gather(), emoji=True)
        self.assertEqual(out, "\U0001f4ec 3")

    def test_text_mode_held_only(self):
        _deliver_held(self.root, 1)
        with as_address(self.root):
            out = badge.format_text(badge.gather(), emoji=True)
        self.assertEqual(out, "⏸ 1")

    def test_text_mode_nothing_is_empty_string(self):
        with as_address(self.root):
            out = badge.format_text(badge.gather(), emoji=True)
        self.assertEqual(out, "")

    def test_no_emoji_mode_is_pure_ascii(self):
        _deliver_new(self.root, 2)
        _deliver_held(self.root, 1)
        with as_address(self.root):
            out = badge.format_text(badge.gather(), emoji=False)
        self.assertEqual(out, "new:2 held:1")
        out.encode("ascii")  # raises if not pure ASCII

    def test_main_prints_nothing_and_returns_zero_when_empty(self):
        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                rc = badge.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

    def test_main_prints_text_line(self):
        _deliver_new(self.root, 1)
        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                rc = badge.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "\U0001f4ec 1")

    def test_main_no_emoji_flag(self):
        _deliver_new(self.root, 1)
        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                rc = badge.main(["--no-emoji"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "new:1")


class BadgeJsonModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_json_mode_shape(self):
        _deliver_new(self.root, 2, from_=f"{UID + 1}:peer")
        _deliver_held(self.root, 1, from_=f"{UID + 2}:peer")
        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                rc = badge.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["new"], 2)
        self.assertEqual(payload["held"], 1)
        self.assertEqual(payload["address"], ADDR)
        self.assertIsInstance(payload["senders"], list)

    def test_json_mode_prints_even_when_nothing_pending(self):
        # --json is machine-readable and should always emit a full object, unlike the terse
        # default text mode which suppresses output when there's nothing to report.
        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                rc = badge.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload, {"new": 0, "held": 0, "senders": [], "address": ADDR})


class BadgeSendersTest(unittest.TestCase):
    """§2: senders = ONLY kernel-verified sender uids, never project labels/subjects/body."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_senders_are_kernel_verified_uids_as_strings(self):
        # The self-declared `from_` field claims a lying uid + a project label; the real,
        # kernel-verified owner uid of the file (the process that ran `deliver`) is REAL_UID.
        # We mock identity.open_verified to simulate two distinct genuine senders, the way
        # test_mesh_approve.py fakes FOREIGN_UID via mocking rather than real multi-user setup.
        _deliver_new(self.root, 1, from_="9999:evil-project")
        _deliver_held(self.root, 1, from_="9999:evil-project")

        real_open_verified = identity.open_verified

        def fake_open_verified(path):
            fd, _real_uid = real_open_verified(path)
            if os.path.basename(os.path.dirname(path)) == "new":
                return fd, 1002
            return fd, 1003

        with as_address(self.root), \
             mock.patch("pm_mesh.identity.open_verified", side_effect=fake_open_verified):
            info = badge.gather()

        self.assertEqual(set(info["senders"]), {"1002", "1003"})
        for s in info["senders"]:
            self.assertIsInstance(s, str)
            self.assertNotIn("evil-project", s)
            self.assertNotIn(":", s)

    def test_senders_deduplicated(self):
        # `from_` is an unverified, self-declared field; the kernel-verified sender uid is
        # always the real (unmocked) uid of the process that ran `deliver()` — capture it
        # BEFORE entering as_address(), which mocks os.getuid() for address-derivation only.
        real_uid = os.getuid()
        _deliver_new(self.root, 3, from_=f"{UID + 1}:peer")
        with as_address(self.root):
            info = badge.gather()
        self.assertEqual(info["senders"], [str(real_uid)])

    def test_no_body_subject_or_project_label_anywhere_in_output(self):
        secret = "TOP-SECRET-BODY-MARKER-4471"
        maildir.deliver(
            message.new_message(ADDR, secret, from_=f"{UID + 1}:peer", subject="also-secret-subject"),
            root=self.root,
        )
        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                badge.main(["--json"])
        out = buf.getvalue()
        self.assertNotIn(secret, out)
        self.assertNotIn("also-secret-subject", out)
        self.assertNotIn("peer", out)


class BadgeReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_gather_and_main_do_not_touch_new_or_held(self):
        _deliver_new(self.root, 3)
        _deliver_held(self.root, 2)
        new_dir = os.path.join(self.root, ADDR, "new")
        held_dir = os.path.join(self.root, ADDR, "held")
        before_new = sorted(os.listdir(new_dir))
        before_held = sorted(os.listdir(held_dir))
        before_new_bytes = {n: open(os.path.join(new_dir, n), "rb").read() for n in before_new}
        before_held_bytes = {n: open(os.path.join(held_dir, n), "rb").read() for n in before_held}

        with as_address(self.root):
            badge.gather()
            with redirect_stdout(io.StringIO()):
                badge.main([])
                badge.main(["--json"])

        after_new = sorted(os.listdir(new_dir))
        after_held = sorted(os.listdir(held_dir))
        self.assertEqual(before_new, after_new)
        self.assertEqual(before_held, after_held)
        for n in after_new:
            self.assertEqual(before_new_bytes[n], open(os.path.join(new_dir, n), "rb").read())
        for n in after_held:
            self.assertEqual(before_held_bytes[n], open(os.path.join(held_dir, n), "rb").read())


class BadgeFailClosedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_missing_maildir_gives_empty_output_exit_zero(self):
        # Fresh root: the address dir has never been created (no deliver() ever ran).
        buf, errbuf = io.StringIO(), io.StringIO()
        with as_address(os.path.join(self.root, "never-created")):
            with redirect_stdout(buf), redirect_stderr(errbuf):
                rc = badge.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(errbuf.getvalue(), "")

    def test_unexpected_error_is_swallowed_without_debug(self):
        buf, errbuf = io.StringIO(), io.StringIO()
        with as_address(self.root), \
             mock.patch("pm_mesh.badge.gather", side_effect=RuntimeError("boom")):
            with redirect_stdout(buf), redirect_stderr(errbuf):
                rc = badge.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(errbuf.getvalue(), "")

    def test_debug_flag_lets_error_surface(self):
        with as_address(self.root), \
             mock.patch("pm_mesh.badge.gather", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                badge.main(["--debug"])

    def test_corrupt_message_does_not_crash_and_is_skipped_from_senders(self):
        # A symlink in new/ is counted as a file by status._list_files (os.path.isfile follows
        # symlinks) but identity.open_verified (O_NOFOLLOW) refuses it -> IdentityError -> the
        # sender must be silently skipped, never a crash.
        _deliver_new(self.root, 1)
        new_dir = os.path.join(self.root, ADDR, "new")
        target = os.path.join(self.tmp.name, "outside-target.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("not a real mesh message")
        os.symlink(target, os.path.join(new_dir, "z" * 32))

        buf = io.StringIO()
        with as_address(self.root):
            with redirect_stdout(buf):
                rc = badge.main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["new"], 2)  # both files counted
        self.assertEqual(len(payload["senders"]), 1)  # only the genuine message's sender


if __name__ == "__main__":
    unittest.main()
