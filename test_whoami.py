"""``mesh-whoami``: show a participant their OWN address.

A new participant may not know their own uid — the skill used to hardcode ``1100`` as "the uid".
One command must show it instead of guessing a number.
"""
from __future__ import annotations

import os

from pm_mesh import whoami


def test_address_uses_uid_and_cwd_basename():
    uid = os.getuid()
    assert whoami.address(cwd="/work/agentixmesh") == f"{uid}:agentixmesh"
    assert whoami.address(cwd="/work/backend") == f"{uid}:backend"


def test_render_shows_address_and_actionable_reply_syntax():
    uid = os.getuid()
    text = whoami.render(cwd="/work/backend")
    assert f"{uid}:backend" in text              # the address is shown verbatim
    assert f"mesh-send {uid}:backend" in text    # how others reach you — copy/pasteable
    assert "uid" in text.lower()


def test_main_prints_current_address_and_exits_zero(capsys):
    rc = whoami.main([])
    out = capsys.readouterr().out
    uid = os.getuid()
    assert rc == 0
    assert f"{uid}:" in out
