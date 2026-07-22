"""Tests for pm_mesh.dcp_send — validate-before-send CLI.

Strategy:
- Mock pm_mesh.dcp_send.subprocess.run so no real message is ever sent.
- Mock pm_mesh.dcp.validate so tests don't require Node.js / the DCP repo.
- Use the real dcp.wrap to verify the wrapped body handed to the transport.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pm_mesh import dcp, dcp_send

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_DCP_REPO = os.environ.get("DCP_REPO") or os.path.expanduser("~/development-coordination-protocol")
_EXAMPLE_PATH = Path(_DCP_REPO) / "examples" / "v1" / "task.completed.json"
# Validation is mocked in these tests, so the example only needs to be representative
# JSON. Fall back to an inline envelope when the reference repo isn't checked out.
if _EXAMPLE_PATH.is_file():
    _EXAMPLE = _EXAMPLE_PATH.read_text(encoding="utf-8")
else:
    _EXAMPLE = (
        '{"dcp_version": "1.0", "message_type": "task.completed", '
        '"entity_type": "task", "verb": "completed", '
        '"attributed_to": "agent:example", "entity_id": "task_example"}'
    )

# A deliberately invalid payload (missing required fields).
_INVALID_JSON = '{"dcp_version": "1.0"}'


def _make_run_result(returncode: int = 0, stdout: str = "msg-id-abc\n") -> MagicMock:
    """Return a mock CompletedProcess-like object."""
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    return r


# ---------------------------------------------------------------------------
# Tests — valid message path
# ---------------------------------------------------------------------------


class TestValidMessageFromFile:
    """Valid JSON file → wrapped body forwarded, exit 0."""

    def test_exit_code_zero(self, tmp_path):
        json_file = tmp_path / "msg.json"
        json_file.write_text(_EXAMPLE, encoding="utf-8")

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(0)) as mock_run,
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            rc = dcp_send.main([f"1200:sandbox", str(json_file)])

        assert rc == 0

    def test_transport_called_once(self, tmp_path):
        json_file = tmp_path / "msg.json"
        json_file.write_text(_EXAMPLE, encoding="utf-8")

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(0)) as mock_run,
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            dcp_send.main(["1200:sandbox", str(json_file)])

        mock_run.assert_called_once()

    def test_wrapped_body_on_stdin(self, tmp_path):
        """The subprocess must receive the DCP-wrapped body via *input=*."""
        json_file = tmp_path / "msg.json"
        json_file.write_text(_EXAMPLE, encoding="utf-8")

        captured_kwargs = {}

        def fake_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return _make_run_result(0)

        with (
            patch("pm_mesh.dcp_send.subprocess.run", side_effect=fake_run),
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            dcp_send.main(["1200:sandbox", str(json_file)])

        assert "input" in captured_kwargs
        body = captured_kwargs["input"]
        assert '<dcp v="1.0">' in body
        assert "</dcp>" in body

    def test_addr_in_command(self, tmp_path):
        """The subprocess cmd must include the target address."""
        json_file = tmp_path / "msg.json"
        json_file.write_text(_EXAMPLE, encoding="utf-8")

        captured_cmd = {}

        def fake_run(cmd, **kwargs):
            captured_cmd["cmd"] = cmd
            return _make_run_result(0)

        with (
            patch("pm_mesh.dcp_send.subprocess.run", side_effect=fake_run),
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            dcp_send.main(["1200:sandbox", str(json_file)])

        assert "1200:sandbox" in captured_cmd["cmd"]

    def test_no_shell_true(self, tmp_path):
        """subprocess.run must be called with shell=False (list argv, not shell=True)."""
        json_file = tmp_path / "msg.json"
        json_file.write_text(_EXAMPLE, encoding="utf-8")

        captured_kwargs = {}

        def fake_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return _make_run_result(0)

        with (
            patch("pm_mesh.dcp_send.subprocess.run", side_effect=fake_run),
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            dcp_send.main(["1200:sandbox", str(json_file)])

        assert not captured_kwargs.get("shell", False)


# ---------------------------------------------------------------------------
# Tests — invalid message (transport must NOT be called)
# ---------------------------------------------------------------------------


class TestInvalidMessage:
    """Invalid JSON → non-zero exit, transport not invoked."""

    def test_exit_nonzero(self, tmp_path, capsys):
        json_file = tmp_path / "bad.json"
        json_file.write_text(_INVALID_JSON, encoding="utf-8")

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(0)) as mock_run,
            patch("pm_mesh.dcp.validate", return_value=(False, ["missing required field: message_type"])),
        ):
            rc = dcp_send.main(["1200:sandbox", str(json_file)])

        assert rc != 0

    def test_transport_not_called(self, tmp_path):
        json_file = tmp_path / "bad.json"
        json_file.write_text(_INVALID_JSON, encoding="utf-8")

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(0)) as mock_run,
            patch("pm_mesh.dcp.validate", return_value=(False, ["missing required field: message_type"])),
        ):
            dcp_send.main(["1200:sandbox", str(json_file)])

        mock_run.assert_not_called()

    def test_errors_on_stderr(self, tmp_path, capsys):
        json_file = tmp_path / "bad.json"
        json_file.write_text(_INVALID_JSON, encoding="utf-8")

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(0)),
            patch("pm_mesh.dcp.validate", return_value=(False, ["missing required field: message_type"])),
        ):
            dcp_send.main(["1200:sandbox", str(json_file)])

        captured = capsys.readouterr()
        assert "missing required field" in captured.err


# ---------------------------------------------------------------------------
# Tests — stdin path (source == "-")
# ---------------------------------------------------------------------------


class TestStdinPath:
    """When source is ``-``, JSON is read from stdin."""

    def test_valid_via_stdin(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(_EXAMPLE))

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(0)) as mock_run,
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            rc = dcp_send.main(["1200:sandbox", "-"])

        assert rc == 0
        mock_run.assert_called_once()

    def test_wrapped_body_via_stdin(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(_EXAMPLE))

        captured_kwargs = {}

        def fake_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return _make_run_result(0)

        with (
            patch("pm_mesh.dcp_send.subprocess.run", side_effect=fake_run),
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            dcp_send.main(["1200:sandbox", "-"])

        assert '<dcp v="1.0">' in captured_kwargs["input"]

    def test_invalid_via_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(_INVALID_JSON))

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(0)) as mock_run,
            patch("pm_mesh.dcp.validate", return_value=(False, ["bad payload"])),
        ):
            rc = dcp_send.main(["1200:sandbox", "-"])

        assert rc != 0
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — usage / arg errors
# ---------------------------------------------------------------------------


class TestArgErrors:
    def test_no_args(self, capsys):
        rc = dcp_send.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "usage" in captured.err.lower()

    def test_one_arg(self, capsys):
        rc = dcp_send.main(["1200:sandbox"])
        assert rc != 0

    def test_three_args(self, tmp_path, capsys):
        rc = dcp_send.main(["1200:sandbox", "a", "b"])
        assert rc != 0

    def test_missing_file(self, capsys):
        rc = dcp_send.main(["1200:sandbox", "/nonexistent/path.json"])
        assert rc != 0
        captured = capsys.readouterr()
        assert "could not read" in captured.err


# ---------------------------------------------------------------------------
# Tests — transport failure propagated
# ---------------------------------------------------------------------------


class TestTransportFailure:
    def test_transport_exit_nonzero_propagated(self, tmp_path):
        json_file = tmp_path / "msg.json"
        json_file.write_text(_EXAMPLE, encoding="utf-8")

        with (
            patch("pm_mesh.dcp_send.subprocess.run", return_value=_make_run_result(1)),
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            rc = dcp_send.main(["1200:sandbox", str(json_file)])

        assert rc == 1

    def test_transport_oserror(self, tmp_path, capsys):
        json_file = tmp_path / "msg.json"
        json_file.write_text(_EXAMPLE, encoding="utf-8")

        with (
            patch("pm_mesh.dcp_send.subprocess.run", side_effect=OSError("no such binary")),
            patch("pm_mesh.dcp.validate", return_value=(True, [])),
        ):
            rc = dcp_send.main(["1200:sandbox", str(json_file)])

        assert rc != 0
        captured = capsys.readouterr()
        assert "could not launch" in captured.err
