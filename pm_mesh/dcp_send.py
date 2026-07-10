"""pm_mesh.dcp_send — validate-before-send CLI for DCP messages.

Usage:
    python3 -m pm_mesh.dcp_send <addr> <file|->

Reads a DcpMessage JSON from *file* (or stdin when ``-`` is given),
validates it via ``dcp.validate``, wraps it with ``dcp.wrap``, and
delivers it through the existing mesh transport (``pm_mesh.send``).

Fail-closed: any error before a successful send returns non-zero and
sends nothing.
"""
from __future__ import annotations

import subprocess
import sys

from . import dcp


def main(argv: list[str] | None = None) -> int:
    """Console entry-point; returns an exit code (0 = success, ≠0 = error)."""
    args = argv if argv is not None else sys.argv[1:]

    if len(args) != 2:
        print(
            "usage: python3 -m pm_mesh.dcp_send <addr> <file|->",
            file=sys.stderr,
        )
        return 2

    addr, source = args

    # --- 1. Read JSON text ---------------------------------------------------
    if source == "-":
        try:
            dcp_json = sys.stdin.read()
        except Exception as exc:
            print(f"dcp-send: could not read stdin: {exc}", file=sys.stderr)
            return 1
    else:
        try:
            with open(source, encoding="utf-8") as fh:
                dcp_json = fh.read()
        except OSError as exc:
            print(f"dcp-send: could not read {source!r}: {exc}", file=sys.stderr)
            return 1

    # --- 2. Validate ---------------------------------------------------------
    ok, errors = dcp.validate(dcp_json)
    if not ok:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    # --- 3. Wrap and send via mesh transport ---------------------------------
    # pm_mesh.send reads the body from stdin when no body positional arg is given.
    # Invoke as a subprocess so MESH_ROOT / MESH_ACL env vars propagate naturally.
    # stdout and stderr are inherited (pass-through), so the message-id and any
    # error messages reach the caller.
    wrapped = dcp.wrap(dcp_json)
    cmd = [sys.executable, "-m", "pm_mesh.send", addr]
    try:
        result = subprocess.run(
            cmd,
            input=wrapped,
            text=True,
            # stdout/stderr inherited → message-id printed directly to caller's terminal
        )
    except OSError as exc:
        print(f"dcp-send: could not launch mesh transport: {exc}", file=sys.stderr)
        return 1

    return result.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
