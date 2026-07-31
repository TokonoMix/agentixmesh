"""Suite-wide safety net: no test may touch the caller's real home directory.

Several commands in this package deliberately write to `~/.claude/settings.json` — that is
how delivery to Claude Code is turned on. A test that exercises such a command against the
default path edits the *developer's own* live configuration. That is not hypothetical: a test
here asserted that `mesh-poll on` was an unknown subcommand, and the day `on` became real the
same test started enabling delivery on the machine running the suite.

So `HOME` is redirected for every test, unconditionally. A test that wants to inspect the file
it wrote points at `tmp_path` (or the redirected home) explicitly. Nothing in this repo has a
legitimate reason to read or write the real one.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Point HOME (and the env vars derived from it) at a per-test throwaway directory."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("MESH_CWD", raising=False)
    # Resolve `~` from the CURRENT environment on every call, never from a captured value: a
    # test that sets its own HOME must still get its own home, while the default stays isolated.
    # (The stdlib falls back to the *pwd database* when HOME is unset, which would walk straight
    # back to the real directory — that fallback is what this replacement removes.)
    monkeypatch.setattr(
        os.path, "expanduser",
        lambda p: p.replace("~", os.environ.get("HOME", str(home)), 1) if p.startswith("~") else p,
    )
    return home
