"""Regression pins for defects a review found in team-init.

The theme: the tool must inspect and mutate *the same thing the session actually uses*. Deriving
identity or the root a second, different way is how a provisioning tool ends up reporting on one
address and writing to another.
"""

from __future__ import annotations

import json
import os

import pytest

from pm_mesh import maildir, presence, team_init, team_init_cli


@pytest.fixture
def host(monkeypatch, tmp_path):
    root = tmp_path / "mesh-root"
    root.mkdir()
    monkeypatch.setenv("MESH_ROOT", str(root))
    monkeypatch.delenv("MESH_CWD", raising=False)
    monkeypatch.delenv("MESH_CROSS_USER", raising=False)
    project = tmp_path / "checkout" / "proj"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)
    return root


def _step(steps, name):
    return next(s for s in steps if s["name"] == name)


# --- defect 1: identity came from the cwd basename, not from the session ------------------------

def test_plan_uses_the_session_address_not_the_cwd_basename(host, monkeypatch):
    """A path-qualified session owns `<base>--<segment>`; checking `<base>` inspects a stranger."""
    (host / presence.PRESENCE_SUBDIR).mkdir()  # a session with a heartbeat has one
    monkeypatch.setattr(presence, "session_heartbeat_record",
                        lambda *a, **k: {"project": "proj--checkout", "project_base": "proj"})

    detail = _step(team_init.plan(), "own_dropbox")["detail"]

    assert f"{os.getuid()}:proj--checkout" in detail
    assert f"{os.getuid()}:proj " not in detail


def test_apply_creates_the_session_dropbox_not_the_basename_one(host, monkeypatch):
    (host / presence.PRESENCE_SUBDIR).mkdir()
    monkeypatch.setattr(presence, "session_heartbeat_record",
                        lambda *a, **k: {"project": "proj--checkout", "project_base": "proj"})
    monkeypatch.setattr(team_init_cli, "_root_usable", lambda steps: True)

    team_init_cli.main(["--apply"])

    assert (host / f"{os.getuid()}:proj--checkout" / "new").is_dir()
    assert not (host / f"{os.getuid()}:proj").exists()


def test_mesh_cwd_wins_over_the_working_directory(host, monkeypatch, tmp_path):
    """MESH_CWD is the explicit-identity channel; a tool that ignores it addresses the wrong box."""
    other = tmp_path / "elsewhere" / "relaybox"
    other.mkdir(parents=True)
    monkeypatch.setenv("MESH_CWD", str(other))

    assert f"{os.getuid()}:relaybox" in _step(team_init.plan(), "own_dropbox")["detail"]


# --- defect 2: the inspected root and the mutated root were resolved differently -----------------

def test_apply_writes_into_the_root_that_was_inspected(host, monkeypatch, tmp_path):
    """The plan resolved MESH_ROOT; the mutation must not fall back to a different active root."""
    monkeypatch.setattr(team_init_cli, "_root_usable", lambda steps: True)
    seen = {}
    real = maildir.maildrop

    def spy(address, root=None, mode=None):
        seen["root"] = root
        seen["mode"] = mode
        return real(address, root=root, mode=mode)

    monkeypatch.setattr(maildir, "maildrop", spy)
    team_init_cli.main(["--apply"])

    assert seen["root"] == team_init.resolve_root()
    # …and the mode is derived from config, exactly like the expectations the plan just printed —
    # not hardcoded to cross_user while the plan was checking same-user modes.
    assert seen["mode"] is None


# --- defect 3: a dropbox with the right modes but the wrong owner was reported correct -----------

def test_dropbox_owned_by_someone_else_is_not_reported_ok(host, monkeypatch):
    addr = team_init.own_address()
    drop = host / addr
    for sub in ("new", "cur", "held"):
        (drop / sub).mkdir(parents=True)
    monkeypatch.setattr(team_init.os, "lstat", _fake_lstat_foreign_owner(drop))

    step = _step(team_init.plan(), "own_dropbox")

    assert step["ok"] is not True
    assert "owner" in step["detail"]


def _fake_lstat_foreign_owner(drop):
    real = os.lstat
    target = str(drop)

    def fake(path, *a, **k):
        st = real(path, *a, **k)
        if str(path).startswith(target):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                   os.getuid() + 1, st.st_gid, st.st_size,
                                   int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
        return st

    return fake


def test_symlinked_dropbox_is_refused(host, tmp_path):
    """The shared-root step refuses a symlink; the dropbox step must hold the same line."""
    real = tmp_path / "somewhere-else"
    for sub in ("new", "cur", "held"):
        (real / sub).mkdir(parents=True)
    os.symlink(real, host / team_init.own_address())

    step = _step(team_init.plan(), "own_dropbox")

    assert step["ok"] is not True
    assert "symlink" in step["detail"]


# --- defect 4: checks that could not fail, and checks that passed on the wrong evidence ----------

def test_python_path_step_can_actually_fail(host, monkeypatch):
    """It reported 'importable' by construction — true in every running process, so never a signal."""
    monkeypatch.setattr(team_init, "_site_dirs", lambda: [])

    assert _step(team_init.plan(), "python_path")["ok"] is not True


def test_wrapper_must_be_executable_not_merely_present(host, monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("mesh-send", "mesh-inject"):
        (bindir / name).write_text("#!/bin/sh\n")  # present, but not executable
    monkeypatch.setattr(team_init, "_WRAPPER_DIR", str(bindir))

    step = _step(team_init.plan(), "wrappers")

    assert step["ok"] is not True
    assert "executable" in step["detail"]

    for name in ("mesh-send", "mesh-inject"):
        os.chmod(bindir / name, 0o755)
    assert _step(team_init.plan(), "wrappers")["ok"] is True


def test_primary_group_membership_counts(host, monkeypatch):
    """getgroups() omits the primary gid on some systems — reporting 'not a member' is a false alarm."""
    monkeypatch.setattr(team_init.grp, "getgrnam",
                        lambda name: type("G", (), {"gr_gid": 4242})())
    monkeypatch.setattr(team_init.os, "getgroups", lambda: [1, 2, 3])
    monkeypatch.setattr(team_init.os, "getgid", lambda: 4242)

    assert _step(team_init.plan(), "caller_in_group")["ok"] is True


def test_json_output_still_parses(host):
    """The shape other tooling reads must survive all of the above."""
    steps = team_init.plan()
    parsed = json.loads(json.dumps({"steps": steps, "summary": team_init.summary(steps)}))
    assert {s["name"] for s in parsed["steps"]} == {s["name"] for s in steps}
