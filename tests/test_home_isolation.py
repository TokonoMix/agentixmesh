"""The suite may never touch the caller's real home directory — proven, not assumed.

`conftest.py` redirects HOME for every test. This file is the proof that the redirect is
actually in force, so the guarantee cannot rot silently: without it, a command that writes to
`~/.claude/settings.json` (delivery on/off does exactly that) edits the live configuration of
whoever runs `pytest`.
"""

from __future__ import annotations

import os


def _real_home() -> str:
    """The home the OS would hand us if nothing were patched — read from the pwd database, so
    it is independent of the (redirected) HOME env var."""
    import pwd
    return pwd.getpwuid(os.getuid()).pw_dir


def test_home_env_is_redirected():
    assert os.environ["HOME"] != _real_home()


def test_tilde_expansion_is_redirected():
    assert not os.path.expanduser("~/.claude/settings.json").startswith(_real_home() + "/")


def test_the_redirected_home_is_writable_and_empty_of_real_config(tmp_path):
    settings = os.path.expanduser("~/.claude/settings.json")
    assert not os.path.exists(settings)          # a fresh, throwaway home every test
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with open(settings, "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert os.path.exists(settings)


def test_a_test_may_still_choose_its_own_home(monkeypatch, tmp_path):
    """The redirect resolves `~` from the CURRENT environment, so a test that sets its own HOME
    keeps working (several do) — it just can never be the real one."""
    mine = tmp_path / "elsewhere"
    mine.mkdir()
    monkeypatch.setenv("HOME", str(mine))

    assert os.path.expanduser("~/x") == str(mine / "x")
