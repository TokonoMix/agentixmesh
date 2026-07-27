"""mesh-send warns when the from-address is dead (replies would be silently lost).

The from-label is derived from the shell cwd at send time. When that cwd drifted away from
the session's real project dir (``cd /somewhere/else`` persisting in the tool shell), every
send stamps a from-label no session is reading — and the receiver's reply-with line points
straight at that dead address. Two-tier advisory on stderr, never blocking:

* **Tier 1 (cwd-drift, precise):** this process runs under a live agent session whose
  presence heartbeat records the session's REAL address; a differing cwd-derived from-label
  means replies go to a mailbox nobody reads → warn, naming both addresses.
* **Tier 2 (fallback, no heartbeat):** no live-session heartbeat for this process chain
  (cron, plain terminal) → warn only when the from-address has no existing mailbox.

The warning may never change the send outcome: exit code stays 0, the message is delivered,
stdout stays exactly the message id, and any internal error in the check is swallowed.
"""

from __future__ import annotations

import json
import os

import pytest

from pm_mesh import maildir, presence, send

FAKE_SESSION_PID = 999999901


@pytest.fixture
def mesh_env(monkeypatch, tmp_path):
    """Isolated mesh root + a project cwd whose basename is the from-label."""
    root = tmp_path / "mesh-root"
    root.mkdir()
    monkeypatch.setenv("MESH_ROOT", str(root))
    monkeypatch.delenv("MESH_CROSS_USER", raising=False)
    proj = tmp_path / "projdir"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(presence, "session_pid", lambda *a, **k: FAKE_SESSION_PID)
    return root


def _write_heartbeat(root, project, uid=None, pid=FAKE_SESSION_PID, raw=None):
    directory = presence.presence_dir(str(root))
    path = os.path.join(directory, f"{pid}.json")
    if raw is not None:
        data = raw
    else:
        record = {
            "user": uid if uid is not None else os.getuid(),
            "project": project,
            "cwd": "/irrelevant",
            "pid": pid,
            "started": "2026-07-27T05:00:00Z",
            "last_seen": "2026-07-27T05:00:00Z",
        }
        data = json.dumps(record)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)
    return path


def _own_address():
    return f"{os.getuid()}:projdir"


def test_drift_warns_naming_both_addresses(mesh_env, capsys):
    """Heartbeat says the session lives elsewhere → cwd-drift warning with both addresses."""
    _write_heartbeat(mesh_env, "realproject")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "cwd-drift" in err
    assert _own_address() in err                     # the dead from-label
    assert f"{os.getuid()}:realproject" in err       # the session's real address


def test_matching_heartbeat_stays_silent(mesh_env, capsys):
    """Heartbeat matches the cwd-derived address → live address, no warning (even without a mailbox)."""
    _write_heartbeat(mesh_env, "projdir")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert capsys.readouterr().err == ""


def test_no_heartbeat_and_no_mailbox_warns(mesh_env, capsys):
    """No session heartbeat (cron/terminal) and no own mailbox → tier-2 warning."""
    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "no mailbox" in err
    assert _own_address() in err


def test_no_heartbeat_but_mailbox_exists_stays_silent(mesh_env, capsys):
    """No heartbeat but the from-address has a mailbox → plausible async owner, no warning."""
    maildir.maildrop(_own_address())

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert capsys.readouterr().err == ""


def test_other_uids_heartbeat_is_ignored(mesh_env, capsys):
    """A heartbeat for the same pid but ANOTHER uid (shared presence dir / pid reuse) is not
    our session → falls through to tier 2."""
    _write_heartbeat(mesh_env, "realproject", uid=os.getuid() + 1)

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "cwd-drift" not in err
    assert "no mailbox" in err


def test_malformed_heartbeat_falls_through_to_tier2(mesh_env, capsys):
    """Unparseable heartbeat is treated as absent: tier-2 check, no crash."""
    _write_heartbeat(mesh_env, None, raw="{not json")

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert "no mailbox" in capsys.readouterr().err


def test_check_failure_never_breaks_the_send(mesh_env, monkeypatch, capsys):
    """Any internal error in the advisory (here: session_pid raising) is swallowed —
    the send succeeds silently."""
    def boom(*a, **k):
        raise RuntimeError("proc walk exploded")
    monkeypatch.setattr(presence, "session_pid", boom)

    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    assert capsys.readouterr().err == ""


def test_warning_goes_to_stderr_stdout_stays_message_id(mesh_env, capsys):
    """The warning never pollutes stdout: stdout is exactly the delivered message id."""
    rc = send.main(["1100:peer", "hoi"])

    assert rc == 0
    out = capsys.readouterr().out
    delivered = maildir.list_new("1100:peer")
    assert len(delivered) == 1
    with open(delivered[0], encoding="utf-8") as fh:
        msg_id = json.load(fh)["id"]
    assert out == f"{msg_id}\n"
