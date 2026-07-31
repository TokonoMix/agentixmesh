"""PQ-03 — send-side: path addressing + refuse-on-ambiguity .

- ``mesh-send <uid>:/abs/path`` targets the session IN that directory: resolved to that
  session's live (possibly qualified) label, else to the dir's basename label. Humans think
  in folders; the mesh translates. Sender-side convenience only — never forges identity.
- Sending to a BASE label while ≥2 live sessions (different dirs) claim it is AMBIGUOUS and
  refused ("two live sessions carry that name — which one do you mean?"): exit 4, nothing delivered, every
  live variant listed with its path. ``--base`` delivers to the shared base box deliberately.
- One live claimant (or none) → base delivery works exactly as today.
"""

from __future__ import annotations

import json
import os

import pytest

from pm_mesh import maildir, presence, send

MY_PID = 999999901


@pytest.fixture
def mesh_env(monkeypatch, tmp_path):
    root = tmp_path / "mesh-root"
    root.mkdir()
    monkeypatch.setenv("MESH_ROOT", str(root))
    monkeypatch.delenv("MESH_CWD", raising=False)
    monkeypatch.delenv("MESH_CROSS_USER", raising=False)
    sender = tmp_path / "senderdir"
    sender.mkdir()
    monkeypatch.chdir(sender)
    monkeypatch.setattr(presence, "session_pid", lambda *a, **k: MY_PID)
    return root


def _session(root, cwd, project, base, pid):
    directory = presence.presence_dir(str(root))
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{pid}.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "user": os.getuid(), "project": project, "project_base": base,
            "cwd": str(cwd), "pid": pid,
            "started": "2026-07-30T16:00:00Z", "last_seen": "2026-07-30T16:00:00Z",
        }))


def _mk(tmp_path, rel):
    d = tmp_path / rel
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_path_addressing_resolves_live_qualified_label(mesh_env, tmp_path):
    d = _mk(tmp_path, "projects/shop2/proj")
    _session(mesh_env, d, "proj--shop2", "proj", pid=1)

    rc = send.main([f"{os.getuid()}:{d}", "hoi"])

    assert rc == 0
    assert len(maildir.list_new(f"{os.getuid()}:proj--shop2")) == 1


def test_path_addressing_without_session_uses_basename(mesh_env, tmp_path):
    d = _mk(tmp_path, "projects/shop2/proj")

    rc = send.main([f"{os.getuid()}:{d}", "hoi"])

    assert rc == 0
    assert len(maildir.list_new(f"{os.getuid()}:proj")) == 1


def test_ambiguous_base_send_is_refused_with_variants(mesh_env, tmp_path, capsys):
    a = _mk(tmp_path, "projects/shop2/proj")
    b = _mk(tmp_path, "projects/other/proj")
    _session(mesh_env, a, "proj--shop2", "proj", pid=1)
    _session(mesh_env, b, "proj--other", "proj", pid=os.getpid())

    rc = send.main([f"{os.getuid()}:proj", "hoi"])

    assert rc == 4
    assert maildir.list_new(f"{os.getuid()}:proj") == []
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "proj--shop2" in err and str(a) in err
    assert "proj--other" in err and str(b) in err
    assert "--base" in err


def test_base_flag_delivers_to_shared_base_box(mesh_env, tmp_path):
    a = _mk(tmp_path, "projects/shop2/proj")
    b = _mk(tmp_path, "projects/other/proj")
    _session(mesh_env, a, "proj--shop2", "proj", pid=1)
    _session(mesh_env, b, "proj--other", "proj", pid=os.getpid())

    rc = send.main(["--base", f"{os.getuid()}:proj", "hoi"])

    assert rc == 0
    assert len(maildir.list_new(f"{os.getuid()}:proj")) == 1


def test_single_claimant_base_send_works(mesh_env, tmp_path):
    a = _mk(tmp_path, "projects/shop2/proj")
    _session(mesh_env, a, "proj--shop2", "proj", pid=1)

    rc = send.main([f"{os.getuid()}:proj", "hoi"])

    assert rc == 0
    assert len(maildir.list_new(f"{os.getuid()}:proj")) == 1


def test_qualified_target_is_never_ambiguous(mesh_env, tmp_path):
    a = _mk(tmp_path, "projects/shop2/proj")
    b = _mk(tmp_path, "projects/other/proj")
    _session(mesh_env, a, "proj--shop2", "proj", pid=1)
    _session(mesh_env, b, "proj--other", "proj", pid=os.getpid())

    rc = send.main([f"{os.getuid()}:proj--other", "hoi"])

    assert rc == 0
    assert len(maildir.list_new(f"{os.getuid()}:proj--other")) == 1
