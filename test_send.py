import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pm_mesh import message, send


class SendTest(unittest.TestCase):
    def _run(self, argv, stdin=None):
        """Run send.main(argv) with mocked identity; return (rc, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        ctx = [
            mock.patch("os.getuid", return_value=1000),
            mock.patch("os.getcwd", return_value="/home/user/peer"),
        ]
        if stdin is not None:
            ctx.append(mock.patch("sys.stdin", io.StringIO(stdin)))
        for c in ctx:
            c.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = send.main(argv)
        finally:
            for c in reversed(ctx):
                c.stop()
        return rc, out.getvalue(), err.getvalue()

    def _new_dir(self, root, addr):
        return os.path.join(root, addr, "new")

    def test_body_arg_delivers_to_inbox(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                rc, out, err = self._run(["1000:gallery", "hoi daar"])
            self.assertEqual(rc, 0, err)
            files = os.listdir(self._new_dir(root, "1000:gallery"))
            self.assertEqual(len(files), 1)
            path = os.path.join(self._new_dir(root, "1000:gallery"), files[0])
            with open(path, encoding="utf-8") as fh:
                msg = message.from_json(fh.read())
            self.assertEqual(msg.body, "hoi daar")
            self.assertEqual(msg.to, "1000:gallery")
            self.assertEqual(msg.from_, "1000:peer")
            self.assertEqual(msg.kind, "request")
            # stdout carries the message id
            self.assertEqual(out.strip(), msg.id)

    def test_body_read_from_stdin(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                rc, out, err = self._run(["1000:gallery"], stdin="from stdin\n")
            self.assertEqual(rc, 0, err)
            files = os.listdir(self._new_dir(root, "1000:gallery"))
            path = os.path.join(self._new_dir(root, "1000:gallery"), files[0])
            with open(path, encoding="utf-8") as fh:
                msg = message.from_json(fh.read())
            self.assertEqual(msg.body, "from stdin\n")

    def test_kind_and_thread_options(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                rc, out, err = self._run(
                    ["--kind", "notify", "--thread", "t-1", "1000:gallery", "x"]
                )
            self.assertEqual(rc, 0, err)
            files = os.listdir(self._new_dir(root, "1000:gallery"))
            path = os.path.join(self._new_dir(root, "1000:gallery"), files[0])
            with open(path, encoding="utf-8") as fh:
                msg = message.from_json(fh.read())
            self.assertEqual(msg.kind, "notify")
            self.assertEqual(msg.thread, "t-1")

    def test_invalid_to_exits_nonzero_no_file(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                rc, out, err = self._run(["notanaddress", "hoi"])
            self.assertNotEqual(rc, 0)
            self.assertTrue(err.strip())
            self.assertFalse(os.path.isdir(os.path.join(root, "notanaddress")))

    def test_body_after_thread_option(self):
        """Regression: `mesh-send <addr> --thread <id> "body"` must parse body correctly."""
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                rc, out, err = self._run(
                    ["1000:gallery", "--thread", "t-42", "reply body text"]
                )
            self.assertEqual(rc, 0, err)
            files = os.listdir(self._new_dir(root, "1000:gallery"))
            self.assertEqual(len(files), 1)
            path = os.path.join(self._new_dir(root, "1000:gallery"), files[0])
            with open(path, encoding="utf-8") as fh:
                msg = message.from_json(fh.read())
            self.assertEqual(msg.body, "reply body text")
            self.assertEqual(msg.thread, "t-42")

    def test_oversized_body_exits_nonzero_no_file(self):
        with tempfile.TemporaryDirectory() as root:
            big = "a" * (message.MAX_BODY_BYTES + 1)
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                rc, out, err = self._run(["1000:gallery", big])
            self.assertNotEqual(rc, 0)
            self.assertTrue(err.strip())
            drop_new = self._new_dir(root, "1000:gallery")
            # inbox may or may not have been created, but must contain no message
            if os.path.isdir(drop_new):
                self.assertEqual(os.listdir(drop_new), [])


if __name__ == "__main__":
    unittest.main()
