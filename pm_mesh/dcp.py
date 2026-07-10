"""pm_mesh.dcp — DCP wire-format helpers + reference validator bridge.

Provides:
- wrap/extract: pure wire-format helpers (no I/O).
- validate:     subprocess bridge to the DCP reference validator (Node.js).

Wire format:
    <dcp v="1.0">
    { ...DcpMessage JSON... }
    </dcp>
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

# Matches a well-formed open tag: <dcp v="...">
_OPEN_RE = re.compile(r'<dcp v="[^"]*">')

# Full frame pattern: open tag, inner content, close tag.
# Anchored to the start/end of the stripped body.
_FRAME_RE = re.compile(
    r'^<dcp v="([^"]*)">\n(.*)\n</dcp>$',
    re.DOTALL,
)


def wrap(dcp_json: str) -> str:
    """Wrap *dcp_json* in the DCP wire-format frame.

    Returns:
        ``'<dcp v="1.0">\\n' + dcp_json + '\\n</dcp>'``
    """
    return f'<dcp v="1.0">\n{dcp_json}\n</dcp>'


def extract(body: str) -> str | None:
    """Extract the inner JSON string from a DCP-framed *body*.

    Returns the inner JSON string if *body* is a well-formed DCP frame,
    ``None`` otherwise.

    Robustness rules (all must hold):
    - Plain text with no marker → None.
    - Missing open or close tag → None.
    - A body that merely mentions ``<dcp`` without being a real frame → None.
    - More than one top-level ``<dcp ...>`` open tag → None (reject smuggling).
    - More than one ``</dcp>`` close tag → None (symmetric with the open check:
      otherwise a trailing extra ``</dcp>`` would be greedily folded into ``inner``).
    - Nested ``<dcp …>`` inside the block → None.
    - Leading/trailing whitespace around the whole frame is tolerated.
    - The returned string is byte-faithful to what ``wrap`` put in.
    - Any ``v="..."`` version is accepted; the tag must be well-formed.

    Accepted, improbable, fail-safe limitation: a payload whose JSON string
    values literally contain ``<dcp v="...">`` (with unescaped quotes) or
    ``</dcp>`` is rejected here. Valid JSON escapes interior quotes, so the open
    case is effectively unreachable; the close case only bites a message that
    literally embeds the close marker — rare for a structured DcpMessage, and it
    fails closed (not delivered as DCP) rather than mis-parsing.
    """
    stripped = body.strip()

    # Count how many well-formed open tags exist in the entire (stripped) body.
    open_tags = _OPEN_RE.findall(stripped)
    if len(open_tags) != 1:
        # Zero → not a frame; 2+ → smuggled second frame
        return None

    # Symmetric close-tag guard: exactly one ``</dcp>``. Without this, a body with
    # one open tag but a trailing extra ``</dcp>`` (e.g. ``…</dcp>\nX\n</dcp>``) has
    # the greedy ``.*`` anchor to the LAST close, folding ``</dcp>\nX`` into inner
    # (internal review + cross-vendor consensus, 2026-07-01, req 11a04496).
    if stripped.count("</dcp>") != 1:
        return None

    # Match the entire stripped body as one frame.
    m = _FRAME_RE.match(stripped)
    if m is None:
        return None

    inner = m.group(2)

    # Reject nested open tags inside the inner content.
    if _OPEN_RE.search(inner):
        return None

    return inner


# ---------------------------------------------------------------------------
# Reference validator bridge
# ---------------------------------------------------------------------------

_DEFAULT_DCP_REPO = os.path.expanduser("~/development-coordination-protocol")


def validate(dcp_json: str, *, dcp_repo: str | None = None) -> tuple[bool, list[str]]:
    """Validate *dcp_json* against the DCP reference validator.

    Resolution order for the repo path:
    1. *dcp_repo* argument
    2. ``DCP_REPO`` environment variable
    3. ``~/development-coordination-protocol`` (neutral default)

    Returns:
        ``(True, [])`` on success, ``(False, [reason, ...])`` on failure.

    Fail-closed: any subprocess error, timeout, missing node/validator, or
    unparseable output returns ``(False, [clear reason])`` — never raises.
    """
    repo = dcp_repo or os.environ.get("DCP_REPO") or _DEFAULT_DCP_REPO
    validator = os.path.join(repo, "reference", "validate.mjs")

    # Check prerequisites before spawning a process.
    node = shutil.which("node")
    if not node:
        return (False, ["node not found in PATH"])
    if not os.path.isfile(validator):
        return (False, [f"DCP validator not found: {validator}"])

    # Write the JSON to a temp file so we can pass it as a file-path argument,
    # matching the CLI contract: node validate.mjs <file.json>
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", encoding="utf-8", delete=False
        ) as tf:
            tmp_path = tf.name
            tf.write(dcp_json)
    except Exception as exc:  # fail-closed on ANY create/write error incl. non-str input (req a607bef2)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return (False, [f"could not stage temp file: {exc}"])

    try:
        result = subprocess.run(
            [node, validator, tmp_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return (False, ["DCP validator timed out"])
    except OSError as exc:
        return (False, [f"could not launch node: {exc}"])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Exit 0 = PASS, exit 1 = FAIL, exit 2 = usage error.
    if result.returncode == 0:
        return (True, [])

    # Collect error lines from stderr (the validator writes errors there).
    # Strip the "FAIL <file>" header line and leading whitespace from each error.
    errors: list[str] = []
    fail_header = f"FAIL {os.path.basename(tmp_path)}"
    for line in (result.stderr + result.stdout).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Exact-match the header only — a broad startswith/endswith could silently drop
        # a real validation-error line that happens to end with the temp basename
        # (internal review + consensus, req a607bef2).
        if stripped == fail_header:
            continue
        errors.append(stripped)

    if not errors:
        errors = [f"DCP validation failed (exit {result.returncode})"]

    return (False, errors)
