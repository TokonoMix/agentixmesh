"""Tests for the mesh-inject hook entry: showing fresh messages, dedup, fail-closed."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from unittest import mock

from pm_mesh import config, inject, maildir, message, messages


class InjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_root = os.environ.get("MESH_ROOT")
        os.environ["MESH_ROOT"] = self.tmp.name
        self.addCleanup(self._restore_root)
        # Own address = what inject looks at; deliver messages there.
        self.addr = config.current_address()

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("MESH_ROOT", None)
        else:
            os.environ["MESH_ROOT"] = self._old_root

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = inject.main()
        return rc, buf.getvalue()

    def _deliver(self, body="hello world", **kw):
        msg = message.new_message(to=self.addr, body=body, **kw)
        maildir.deliver(msg)
        return msg

    def test_empty_inbox_exit0_no_output(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_fresh_message_renders_frame(self):
        self._deliver(body="a test message")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("mesh-msg", out)
        self.assertIn("a test message", out)
        # kernel-verified owner_uid in the header
        self.assertIn(str(os.getuid()), out)

    def test_second_run_dedup_no_output(self):
        self._deliver(body="once-only")
        rc1, out1 = self._run()
        self.assertIn("once-only", out1)
        rc2, out2 = self._run()
        self.assertEqual(rc2, 0)
        self.assertEqual(out2, "")

    def test_message_marked_shown_after_render(self):
        self._deliver(body="mark me")
        self._run()
        drop = maildir.maildrop(self.addr)
        cur = os.path.join(drop, "cur")
        files = os.listdir(cur)
        # exactly one message + its .shown sidecar
        shown = [f for f in files if f.endswith(".shown")]
        msgs = [f for f in files if not f.endswith(".shown")]
        self.assertEqual(len(msgs), 1)
        self.assertIn(msgs[0] + ".shown", files)
        self.assertEqual(len(shown), 1)

    def test_corrupt_file_exit0_no_frame(self):
        # Place an unverifiable file: a hardlink (identity refuses st_nlink>1).
        drop = maildir.maildrop(self.addr)
        new_dir = os.path.join(drop, "new")
        orig = os.path.join(self.tmp.name, "outside")
        with open(orig, "wb") as fh:
            fh.write(b'{"not": "valid"}')
        os.link(orig, os.path.join(new_dir, "hardlinked"))
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn("mesh-msg", out)

    def test_invalid_json_exit0_no_frame(self):
        drop = maildir.maildrop(self.addr)
        new_dir = os.path.join(drop, "new")
        path = os.path.join(new_dir, "garbage")
        with open(path, "wb") as fh:
            fh.write(b"this is not json {{{")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn("mesh-msg", out)

    def test_multiple_fresh_all_rendered(self):
        self._deliver(body="message-A")
        self._deliver(body="message-B")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("message-A", out)
        self.assertIn("message-B", out)

    def test_dedup_keys_on_receive_address_not_attacker_to(self):
        # FIX 1 (P2): inject dedups on the VERIFIED receive address (config.current_address),
        # not on the attacker-supplied `to` in the body. Two messages with the same `id` but
        # DIFFERENT `to`, both physically in MY maildrop → the second must NOT act again.
        m1 = self._deliver(body="replay-once")
        rc1, out1 = self._run()
        self.assertIn("replay-once", out1)
        # Second drop: same id, different `to` — written directly into new/ (attacker scenario).
        m2 = message.new_message(to="9999:elders", body="replay-once")
        m2.id = m1.id
        drop = maildir.maildrop(self.addr)
        new_dir = os.path.join(drop, "new")
        path = os.path.join(new_dir, "attacker-varied-to")
        with open(path, "wb") as fh:
            fh.write(message.to_json(m2).encode("utf-8"))
        rc2, out2 = self._run()
        self.assertEqual(rc2, 0)
        # Deduplicated on the receive address: the body frame does not reappear.
        self.assertNotIn("replay-once", out2)


class WelcomeTest(unittest.TestCase):
    def test_catalog_returns_english_and_interpolates(self):
        s = messages.t("welcome_address", address="1001:proj")
        self.assertIn("1001:proj", s)

    def test_welcome_content_isolation_from_marker(self):
        # The marker is a boolean trigger; its CONTENT must never be interpolated into the welcome (sec-4).
        import tempfile, os
        from pm_mesh import config
        with tempfile.TemporaryDirectory() as tmp:
            mesh = os.path.join(tmp, "mesh")
            os.makedirs(mesh)
            with mock.patch.dict(os.environ, {"MESH_ROOT": mesh, "HOME": tmp}, clear=False):
                pending = config.onboarding_marker_path(tmp)
                done = config.onboarding_done_path(tmp)
                os.makedirs(os.path.dirname(pending), exist_ok=True)
                with open(pending, "w") as f:
                    f.write("INJECTED-SENTINEL")
                import io
                from contextlib import redirect_stdout
                with mock.patch("pm_mesh.config.onboarding_marker_path", return_value=pending), \
                     mock.patch("pm_mesh.config.onboarding_done_path", return_value=done):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        inject._maybe_show_welcome("1001:proj")
                    output = buf.getvalue()
                    self.assertNotIn("INJECTED-SENTINEL", output)  # content is boolean trigger only
                    self.assertIn("mesh-welcome", output)
                    self.assertIn("1001:proj", output)

    def test_welcome_has_frame_prefix_and_tags(self):
        # render_welcome goes through trusted frame surface: each line has │ prefix, wrapped in tags.
        out = inject.render_welcome("1001:proj")
        self.assertIn("<mesh-welcome>", out)
        self.assertIn("</mesh-welcome>", out)
        self.assertIn("│ ", out)
        self.assertIn("1001:proj", out)
        self.assertIn("mesh-send", out)

    def test_platform_ok_returns_bool_never_raises(self):
        # arch-7: the guard returns a status; it must not throw across the hook boundary.
        with mock.patch("pm_mesh.platform.posix_structural_ok", side_effect=RuntimeError("boom")):
            self.assertFalse(inject._platform_ok())

    def test_failed_guard_returns_0_and_no_delivery(self):
        # Unit: named entry point with an injected platform (not a subprocess) — §10.
        fake_plat = mock.Mock()
        fake_plat.posix_ok = False
        rc = inject.main(argv=[], plat=fake_plat)
        self.assertEqual(rc, 0)


class WelcomeOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.mesh = os.path.join(self.tmp.name, "mesh")
        os.makedirs(self.mesh)
        self.env = mock.patch.dict(
            os.environ, {"MESH_ROOT": self.mesh, "HOME": self.home}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def _marker_paths(self):
        from pm_mesh import config
        return config.onboarding_marker_path(self.home), config.onboarding_done_path(self.home)

    def test_welcome_shown_once_then_sentinel(self):
        from pm_mesh import config
        pending, done = self._marker_paths()
        os.makedirs(os.path.dirname(pending), exist_ok=True)
        open(pending, "w").close()
        import io
        from contextlib import redirect_stdout
        with mock.patch("pm_mesh.config.onboarding_marker_path", return_value=pending), \
             mock.patch("pm_mesh.config.onboarding_done_path", return_value=done):
            buf = io.StringIO()
            with redirect_stdout(buf):
                inject._maybe_show_welcome("1001:proj")
            self.assertIn("agentixmesh", buf.getvalue())
            self.assertTrue(os.path.isfile(done))
            self.assertFalse(os.path.isfile(pending))
            # second call: sentinel present, marker gone -> nothing shown
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                inject._maybe_show_welcome("1001:proj")
            self.assertEqual(buf2.getvalue(), "")

    def test_guard_fail_preserves_pending_marker(self):
        from pm_mesh import config
        pending, done = self._marker_paths()
        os.makedirs(os.path.dirname(pending), exist_ok=True)
        open(pending, "w").close()
        fake_plat = mock.Mock(); fake_plat.posix_ok = False
        rc = inject.main(argv=[], plat=fake_plat)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(pending))  # §11.6: pending survives a failed guard


if __name__ == "__main__":
    unittest.main()
