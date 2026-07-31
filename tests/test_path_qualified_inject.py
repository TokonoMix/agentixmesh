"""PQ-02 — a path-qualified session reads BOTH its boxes (qualified + base).

Once the heartbeat registers a qualified label (live same-basename collision, PQ-01) the
session owns two mailboxes: the qualified box (precise replies) and the base box (legacy /
deliberate ``--base`` traffic). Pinned here:

- inject relays base ``new/`` (and ``held/``, incl. ``.shown`` companions) into the qualified
  box atomically, then runs the turn on the qualified address — a message dropped on either
  label reaches this session in the same turn.
- base traffic keeps today's first-reader semantics between siblings (the relay IS the read).
- fail-open: without a collision nothing changes (covered by the whole existing suite).
"""

from __future__ import annotations

import json
import os

import pytest

from pm_mesh import config, inject, maildir, message, presence

MY_PID = 999999901
SIBLING_PID = 1


@pytest.fixture
def mesh_env(monkeypatch, tmp_path):
    root = tmp_path / "mesh-root"
    root.mkdir()
    monkeypatch.setenv("MESH_ROOT", str(root))
    monkeypatch.delenv("MESH_CWD", raising=False)
    monkeypatch.delenv("MESH_CROSS_USER", raising=False)
    monkeypatch.delenv("MESH_POSTBODE_RUN", raising=False)
    monkeypatch.setattr(presence, "session_pid", lambda *a, **k: MY_PID)
    mine = tmp_path / "projects" / "shop2" / "proj"
    mine.mkdir(parents=True)
    sib = tmp_path / "projects" / "other" / "proj"
    sib.mkdir(parents=True)
    monkeypatch.chdir(mine)
    directory = presence.presence_dir(str(root))
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{SIBLING_PID}.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "user": os.getuid(), "project": "proj--other", "project_base": "proj",
            "cwd": str(sib), "pid": SIBLING_PID,
            "started": "2026-07-30T16:00:00Z", "last_seen": "2026-07-30T16:00:00Z",
        }))
    return root


def _uid():
    return os.getuid()


def _drop(to_addr, body="hoi"):
    msg = message.new_message(to_addr, body, from_=f"{_uid()}:sender")
    maildir.deliver(msg)
    return msg


def test_base_box_message_reaches_qualified_session(mesh_env, capsys):
    msg = _drop(f"{_uid()}:proj")                 # sender uses the BASE label

    rc = inject.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert msg.body in out                        # rendered this very turn
    # consumed into the QUALIFIED box's cur/, not left in base
    qual_cur = os.path.join(maildir.maildrop(f"{_uid()}:proj--shop2"), "cur")
    assert any(not n.endswith(".shown") for n in os.listdir(qual_cur))
    base_new = os.path.join(maildir.maildrop(f"{_uid()}:proj"), "new")
    assert [n for n in os.listdir(base_new) if not n.startswith(".")] == []


def test_qualified_box_message_is_read(mesh_env, capsys):
    msg = _drop(f"{_uid()}:proj--shop2")      # sender targets THIS session precisely

    rc = inject.main()

    assert rc == 0
    assert msg.body in capsys.readouterr().out


def test_base_held_state_moves_along(mesh_env):
    base_drop = maildir.maildrop(f"{_uid()}:proj")
    held = os.path.join(base_drop, "held")
    with open(os.path.join(held, "aabbccdd"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    with open(os.path.join(held, "aabbccdd.shown"), "w", encoding="utf-8") as fh:
        fh.write("")

    assert inject.main() == 0

    qual_held = os.path.join(maildir.maildrop(f"{_uid()}:proj--shop2"), "held")
    assert sorted(os.listdir(qual_held)) == ["aabbccdd", "aabbccdd.shown"]
    assert os.listdir(held) == []
