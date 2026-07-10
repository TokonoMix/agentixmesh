"""pm_mesh.dcp_recv — Receive and surface DCP-framed mesh messages.

A received DCP message is an inert CLAIM about a project change.
This module NEVER executes, evaluates, or acts on any field value.
It only reads, validates structure, and prints a compact summary.

CLI usage:
    python3 -m pm_mesh.dcp_recv              # read body from stdin
    python3 -m pm_mesh.dcp_recv <file>       # read body from file (for testing)

Exit codes:
    0 — plain non-DCP body (silently ignored) OR valid DCP message summarised
    non-zero — DCP frame found but failed structural validation
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from pm_mesh import frame, dcp


def _print_summary(parsed: dict) -> None:
    """Print a compact structured summary of *parsed* DcpMessage to stdout.

    Fields surfaced and their source paths:
        message_type   <- parsed["message_type"]
        entity_type    <- parsed["body"]["entity_type"]
        verb           <- parsed["body"]["verb"]
        attributed_to  <- parsed["body"]["attributed_to"]
        entity_id      <- parsed["body"]["entity_id"]

    Missing optional fields are omitted — no KeyError, no crash.
    This function is pure read + print; it never executes field values.

    SECURITY: schema validation confirms a field is a *string* but does NOT
    forbid newlines, ANSI escapes, ``Human:``-prefixes or fake frame tags inside
    it. Printing such a value raw would launder attacker-controlled bytes, with a
    trusted-looking label, into a consuming agent's context (prompt-injection /
    output-spoofing). So every value is routed through the SAME single-line field
    sanitizer the mesh frame uses (``frame._sanitize_field``: confusable-fold +
    ANSI/zero-width/control strip + ``Human:``/tag defang + newline→space) — the
    output stays one ``key: value`` line per field and cannot break framing.
    Internal review + cross-vendor consensus, 2026-07-01 (req 29bab040).
    """
    body = parsed.get("body") or {}
    fields = {
        "message_type": parsed.get("message_type"),
        "entity_type": body.get("entity_type"),
        "verb": body.get("verb"),
        "attributed_to": body.get("attributed_to"),
        "entity_id": body.get("entity_id"),
    }
    # Print only fields that are present (not None); sanitize each value.
    for key, value in fields.items():
        if value is not None:
            print(f"{key}: {frame._sanitize_field(value)}")


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for dcp_recv.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        0 on success or silent ignore; non-zero on validation failure.
    """
    if argv is None:
        argv = sys.argv[1:]

    # Read body from file arg (testing convenience) or stdin.
    if argv:
        try:
            with open(argv[0], encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            print(f"dcp_recv: cannot open {argv[0]!r}: {exc}", file=sys.stderr)
            return 1
    else:
        body = sys.stdin.read()

    # Step 1: extract inner JSON from DCP frame.
    inner = dcp.extract(body)
    if inner is None:
        # Plain non-DCP body — silently ignore, no output, return 0.
        return 0

    # Step 2: validate structure via reference validator.
    ok, errors = dcp.validate(inner)
    if not ok:
        reason = "; ".join(errors) if errors else "unknown validation error"
        print(f"dcp_recv: invalid DCP message: {reason}", file=sys.stderr)
        return 2

    # Step 3: parse and print structured summary.
    # SAFETY: we only READ field values and pass them to print().
    # We never eval(), exec(), subprocess, or otherwise act on any value.
    try:
        parsed = json.loads(inner)
    except json.JSONDecodeError as exc:
        # Inner passed validate() but is not valid JSON — defensive path.
        print(f"dcp_recv: JSON parse error after validation: {exc}", file=sys.stderr)
        return 2

    _print_summary(parsed)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
