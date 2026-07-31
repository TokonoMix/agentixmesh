"""Verifiable, independently-anchored provenance manifest for foreign-agent onboarding.

A skeptical agent being onboarded onto agentixmesh must be able to *check* the delivery mechanism, not
merely read a "trust me" claim. This module builds a small machine-readable manifest the agent verifies
with TWO checks:

* ``binary_sha256`` — hash of the exact file the delivery mechanism executes. A same-author sanity check
  (the onboarder wrote both the file and the hash), sufficient under the Tier 1 "operator has root anyway"
  assumption but NOT independent on its own.
* ``source_commit`` — the git HEAD of ``source_root``. This is the genuinely-independent anchor: git
  content-addressing is non-malleable, so an agent that can read the repo can confirm the executed script
  really is that source. The agent-facing skill instructs this cross-check as non-bypassable.

"""
from __future__ import annotations

import hashlib
import json
import subprocess

from . import message

SCHEMA = "agentixmesh-provenance/1"

_CHUNK = 65536


def _sha256_file(path: str) -> str:
    """Streamed sha256 of the file at ``path``. Raises (FileNotFoundError/OSError) if it is missing —
    the caller MUST pass the real file the delivery mechanism executes (an absent binary is a wiring bug,
    not something to paper over)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _git_head(source_root: str) -> str:
    """``git -C source_root rev-parse HEAD`` or ``"unknown"`` on any error (never raises)."""
    try:
        out = subprocess.run(
            ["git", "-C", source_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        commit = out.stdout.strip()
        if out.returncode == 0 and commit:
            return commit
    except Exception:
        pass
    return "unknown"


def build_manifest(
    *,
    binary_path: str,
    source_root: str,
    address: str,
    capabilities: list[str],
    restrictions: list[str],
    now_iso: str,
) -> dict:
    """Build the provenance manifest dict.

    ``binary_path`` is stored VERBATIM and hashed; it MUST be the exact file the delivery mechanism runs
    (the harness adapter's responsibility). ``source_commit`` anchors provenance independently.
    """
    return {
        "schema": SCHEMA,
        "address": address,
        "binary_path": binary_path,
        "binary_sha256": _sha256_file(binary_path),
        "source_root": source_root,
        "source_commit": _git_head(source_root),
        "capabilities": list(capabilities),
        "restrictions": list(restrictions),
        "protocol_version": str(message.SCHEMA_VERSION),
        "generated_at": now_iso,
    }


def manifest_json(manifest: dict) -> str:
    """Stable, human-diffable serialisation (sorted keys, 2-space indent)."""
    return json.dumps(manifest, sort_keys=True, indent=2)
