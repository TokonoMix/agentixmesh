"""Cross-harness delivery: the inject hook must derive its address from the SESSION cwd, even
when the harness runs the hook from a different directory and reports the session cwd out-of-band.

Claude Code runs the hook in the project dir, so ``os.getcwd()`` is correct there. Codex CLI (and
potentially others) pass the session ``cwd`` as a field in the JSON they pipe to the hook's stdin,
which means the hook process cwd is not guaranteed to be the session dir. ``inject._effective_cwd``
resolves the session dir with precedence ``MESH_CWD`` env > stdin JSON ``cwd`` > None (fall back to
``os.getcwd()``), fail-closed: any parse error yields None so delivery degrades to the old behaviour,
never crashes.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from pm_mesh import config, inject, maildir, message


class EffectiveCwdTest(unittest.TestCase):
    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"MESH_CWD": d}, clear=False):
            self.assertEqual(inject._effective_cwd(stdin_text='{"cwd": "/other"}'), d)

    def test_stdin_json_cwd_used_when_no_env(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MESH_CWD", None)
            self.assertEqual(inject._effective_cwd(stdin_text=f'{{"cwd": "{d}"}}'), d)

    def test_nonexistent_cwd_ignored(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MESH_CWD", None)
            self.assertIsNone(inject._effective_cwd(stdin_text='{"cwd": "/no/such/dir/xyz"}'))

    def test_malformed_stdin_is_fail_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MESH_CWD", None)
            for junk in ("not json", "", "[]", '{"cwd": 5}', '{"nope": 1}'):
                self.assertIsNone(inject._effective_cwd(stdin_text=junk))

    def test_no_input_returns_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MESH_CWD", None)
            self.assertIsNone(inject._effective_cwd(stdin_text=None))


class InjectUsesSessionCwdTest(unittest.TestCase):
    """End-to-end: a message delivered to the SESSION-cwd address is shown even when the hook
    process runs from a different directory, as long as MESH_CWD points at the session dir."""

    def test_delivery_follows_mesh_cwd_not_process_cwd(self):
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as session_dir, \
                tempfile.TemporaryDirectory() as hook_dir:
            uid = os.getuid()
            session_addr = f"{uid}:{os.path.basename(session_dir)}"
            with mock.patch.dict(os.environ, {"MESH_ROOT": root, "MESH_CWD": session_dir}, clear=False):
                # deliver to the SESSION address
                maildir.deliver(message.new_message(session_addr, "hello-cross-harness",
                                                    from_=f"{uid}:peer"), root=root)
                # run the hook from an UNRELATED directory (simulating a harness that does not
                # chdir into the session dir before invoking the hook)
                old = os.getcwd()
                os.chdir(hook_dir)
                try:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = inject.main()
                finally:
                    os.chdir(old)
            self.assertEqual(rc, 0)
            self.assertIn("hello-cross-harness", buf.getvalue())

    def test_without_mesh_cwd_uses_process_cwd(self):
        # Backwards-compat: with no override, the address still derives from os.getcwd().
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as session_dir:
            uid = os.getuid()
            proc_addr = f"{uid}:{os.path.basename(session_dir)}"
            env = {"MESH_ROOT": root}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("MESH_CWD", None)
                maildir.deliver(message.new_message(proc_addr, "hi-proc", from_=f"{uid}:peer"), root=root)
                old = os.getcwd()
                os.chdir(session_dir)
                try:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = inject.main()
                finally:
                    os.chdir(old)
            self.assertEqual(rc, 0)
            self.assertIn("hi-proc", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
