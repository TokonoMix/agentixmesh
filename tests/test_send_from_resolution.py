"""Send-side from-address resolution + foreign-live-owner guard (gateway-impersonation incident).

The from-label used to be raw ``basename(os.getcwd())`` — any uid-shared process that happened
to stand in (or cd into) another session's project dir sent mail wearing THAT session's
identity, and replies routed to a session that never spoke (a real incident: a gateway agent
sharing the uid answered a colleague under another live session's label).

New resolution order, pinned here:

1. ``MESH_CWD`` env — explicit identity (harness/relay/cron discipline), always wins ("env").
2. The session presence-heartbeat of the agent ancestor — the session's REGISTERED address,
   immune to shell cwd drift ("session").
3. ``basename(os.getcwd())`` — legacy fallback ("cwd").

Guard, pinned here: a "cwd"-resolved from-address that a live FOREIGN session (presence record
with an alive pid outside our process ancestry) is registered on is refused — the send would
impersonate that session by accident. ``MESH_CWD`` (explicit) and matching-session sends are
never blocked; ``MESH_FROM_GUARD=off`` is the killswitch.
"""

from __future__ import annotations

import json
import os

import pytest

from pm_mesh import maildir, presence, send, whoami

FAKE_SESSION_PID = 999999901
#: pid 1 (init) is always alive and never in our process ancestry set-under-test.
FOREIGN_LIVE_PID = 1


@pytest.fixture
def mesh_env(monkeypatch, tmp_path):
    root = tmp_path / "mesh-root"
    root.mkdir()
    monkeypatch.setenv("MESH_ROOT", str(root))
    monkeypatch.delenv("MESH_CROSS_USER", raising=False)
    monkeypatch.delenv("MESH_CWD", raising=False)
    monkeypatch.delenv("MESH_FROM_GUARD", raising=False)
    proj = tmp_path / "projdir"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(presence, "session_pid", lambda *a, **k: FAKE_SESSION_PID)
    return root


def _write_heartbeat(root, project, uid=None, pid=FAKE_SESSION_PID, cwd="/some/session/dir"):
    directory = presence.presence_dir(str(root))
    path = os.path.join(directory, f"{pid}.json")
    record = {
        "user": uid if uid is not None else os.getuid(),
        "project": project,
        "cwd": cwd,
        "pid": pid,
        "started": "2026-07-30T15:00:00Z",
        "last_seen": "2026-07-30T15:00:00Z",
    }
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record))
    return path


def _delivered_from(to="1100:peer"):
    files = maildir.list_new(to)
    assert len(files) == 1
    with open(files[0], encoding="utf-8") as fh:
        return json.load(fh)["from"]


def test_env_mesh_cwd_wins(mesh_env, monkeypatch):
    """MESH_CWD is the explicit identity: it beats both the heartbeat and the shell cwd."""
    _write_heartbeat(mesh_env, "sessionproject")
    monkeypatch.setenv("MESH_CWD", "/var/lib/relays/gateway")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:gateway"


def test_session_heartbeat_beats_drifted_cwd(mesh_env, capsys):
    """A shell cwd that drifted away from the session dir no longer poisons the from-label:
    the send is stamped with the session's registered address, with a stderr note."""
    _write_heartbeat(mesh_env, "sessionproject")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:sessionproject"
    err = capsys.readouterr().err
    assert "session" in err                       # the note names the session identity
    assert f"{os.getuid()}:sessionproject" in err


def test_session_heartbeat_matching_cwd_is_silent(mesh_env, capsys):
    _write_heartbeat(mesh_env, "projdir")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:projdir"
    assert capsys.readouterr().err == ""


def test_no_heartbeat_falls_back_to_cwd(mesh_env, capsys):
    maildir.maildrop(f"{os.getuid()}:projdir")   # own mailbox exists → no tier-2 warning noise

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:projdir"
    assert capsys.readouterr().err == ""


def test_guard_refuses_foreign_live_owned_address(mesh_env, capsys):
    """No own heartbeat, but a LIVE foreign session is registered on the cwd-derived address →
    the send is refused: nothing delivered, an actionable error on stderr."""
    _write_heartbeat(mesh_env, "projdir", pid=FOREIGN_LIVE_PID)

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 3
    assert maildir.list_new("1100:peer") == []
    err = capsys.readouterr().err
    assert "refusing" in err
    assert f"{os.getuid()}:projdir" in err
    assert "MESH_CWD" in err                     # the remedy is named


def test_guard_killswitch(mesh_env, monkeypatch, capsys):
    _write_heartbeat(mesh_env, "projdir", pid=FOREIGN_LIVE_PID)
    monkeypatch.setenv("MESH_FROM_GUARD", "off")
    maildir.maildrop(f"{os.getuid()}:projdir")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:projdir"


def test_guard_skipped_for_explicit_mesh_cwd(mesh_env, monkeypatch):
    """MESH_CWD is deliberate identity: not blocked even when that address has a live owner
    (same-uid impersonation is not preventable — the guard only stops ACCIDENTS)."""
    _write_heartbeat(mesh_env, "projdir", pid=FOREIGN_LIVE_PID)
    monkeypatch.setenv("MESH_CWD", str(os.getcwd()))

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:projdir"


def test_guard_skipped_for_own_session(mesh_env, capsys):
    """Two sessions on the same address: our own heartbeat resolves the identity, a second
    live session on that address never blocks us."""
    _write_heartbeat(mesh_env, "projdir")                          # ours (session source)
    _write_heartbeat(mesh_env, "projdir", pid=FOREIGN_LIVE_PID)    # a second, live session

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:projdir"


def test_guard_ignores_dead_foreign_owner(mesh_env, capsys):
    """A stale heartbeat with a dead pid does not block anyone."""
    _write_heartbeat(mesh_env, "projdir", pid=999999902)           # not alive
    maildir.maildrop(f"{os.getuid()}:projdir")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert _delivered_from() == f"{os.getuid()}:projdir"


def test_whoami_follows_session_heartbeat(mesh_env):
    """mesh-whoami must report the SAME address mesh-send would stamp (single source of truth)."""
    _write_heartbeat(mesh_env, "sessionproject")

    assert whoami.address() == f"{os.getuid()}:sessionproject"


def test_whoami_explicit_cwd_param_still_wins(mesh_env):
    """Callers that pass an explicit cwd (inject/hooks) keep exact legacy behaviour."""
    _write_heartbeat(mesh_env, "sessionproject")

    assert whoami.address("/tmp/otherdir") == f"{os.getuid()}:otherdir"
