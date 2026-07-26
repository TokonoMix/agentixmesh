"""mesh-resolve surfaces recipient languages WITHOUT changing the machine-readable stdout contract:
single-resolve stdout stays the bare address, the language hint goes to stderr; --list gains a
langs column.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _run(*args):
    env = {**os.environ, "MESH_ROOT": "/nonexistent-root-so-only-seed-loads"}
    return subprocess.run([sys.executable, "-m", "pm_mesh.resolve", *args],
                          capture_output=True, text=True, env=env)


def test_single_resolve_stdout_is_bare_address():
    r = _run("reviewer")
    assert r.stdout.strip() == "1200:reviews"          # machine contract: address only
    assert "en" in r.stderr and "es" in r.stderr        # langs hint on stderr


def test_list_has_langs_column():
    r = _run("--list")
    assert "en,es" in r.stdout                          # 1200's comma-joined codes
