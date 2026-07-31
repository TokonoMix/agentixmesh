"""PQ-01 — path-qualified addressing: collision-triggered, sticky, readable qualifier.

Two live sessions of the same uid whose dirs share a basename used to register the SAME
address (worktree case, the classic worktree case): one mailbox, two
sessions, replies read by whichever sibling's hook runs first.

Pinned here (design the path-qualified addressing design):

- No collision → nothing changes: base label, no qualifier (the 99% case).
- Live collision → the session's heartbeat registers ``<base>--<qual>`` where ``qual`` is the
  first path component distinguishing it from every colliding sibling, walking from the
  parent dir upward (readable and self-describing on purpose: a path segment, not a hash).
- Hash fallback (4 hex of sha256(realpath)) only when no distinguishing component survives,
  or when the computed label would collide with a live sibling's label.
- Sticky: once qualified, a session keeps its qualified label for its lifetime, even after
  the sibling exits. A fresh session alone in the same dir starts unqualified again.
- ``resolve_own_address`` (and therefore mesh-send/whoami/read-side) returns the qualified
  address automatically.
- Fail-open: presence unreadable → base label, never a crash.
"""

from __future__ import annotations

import json
import os

import pytest

from pm_mesh import config, presence

MY_PID = 999999901
SIBLING_PID = 1  # init: always alive, never our session


@pytest.fixture
def mesh_env(monkeypatch, tmp_path):
    root = tmp_path / "mesh-root"
    root.mkdir()
    monkeypatch.setenv("MESH_ROOT", str(root))
    monkeypatch.delenv("MESH_CWD", raising=False)
    monkeypatch.setattr(presence, "session_pid", lambda *a, **k: MY_PID)
    return root


def _mkdirs(tmp_path, rel):
    d = tmp_path / rel.lstrip("/")
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _sibling(root, cwd, project=None, project_base=None, pid=SIBLING_PID):
    directory = presence.presence_dir(str(root))
    os.makedirs(directory, exist_ok=True)
    base = project_base or config.parse_address(config.current_address(cwd))[1]
    record = {
        "user": os.getuid(),
        "project": project or base,
        "project_base": base,
        "cwd": cwd,
        "pid": pid,
        "started": "2026-07-30T16:00:00Z",
        "last_seen": "2026-07-30T16:00:00Z",
    }
    path = os.path.join(directory, f"{pid}.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record))
    return path


def _my_record(root):
    path = os.path.join(presence.presence_dir(str(root)), f"{MY_PID}.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_no_collision_keeps_base_label(mesh_env, tmp_path):
    mine = _mkdirs(tmp_path, "projects/shop2/webshop")

    presence.heartbeat(cwd=mine)

    rec = _my_record(mesh_env)
    assert rec["project"] == "webshop"
    assert rec["project_base"] == "webshop"


def test_collision_qualifies_with_parent_dir(mesh_env, tmp_path):
    mine = _mkdirs(tmp_path, "projects/shop2/webshop")
    sib = _mkdirs(tmp_path, "projects/webshop/webshop")
    _sibling(mesh_env, sib)

    presence.heartbeat(cwd=mine)

    rec = _my_record(mesh_env)
    assert rec["project"] == "webshop--shop2"
    assert rec["project_base"] == "webshop"


def test_equal_parents_walk_higher(mesh_env, tmp_path):
    mine = _mkdirs(tmp_path, "alpha/x/proj")
    sib = _mkdirs(tmp_path, "beta/x/proj")
    _sibling(mesh_env, sib)

    presence.heartbeat(cwd=mine)

    assert _my_record(mesh_env)["project"] == "proj--alpha"


def test_sticky_after_sibling_leaves(mesh_env, tmp_path):
    mine = _mkdirs(tmp_path, "projects/shop2/webshop")
    sib = _mkdirs(tmp_path, "projects/webshop/webshop")
    sib_path = _sibling(mesh_env, sib)

    presence.heartbeat(cwd=mine)
    os.unlink(sib_path)                      # sibling session gone
    presence.heartbeat(cwd=mine)             # next turn

    assert _my_record(mesh_env)["project"] == "webshop--shop2"


def test_dead_sibling_does_not_qualify(mesh_env, tmp_path):
    mine = _mkdirs(tmp_path, "projects/shop2/webshop")
    sib = _mkdirs(tmp_path, "projects/webshop/webshop")
    _sibling(mesh_env, sib, pid=999999902)   # not alive

    presence.heartbeat(cwd=mine)

    assert _my_record(mesh_env)["project"] == "webshop"


def test_same_path_sibling_is_no_collision(mesh_env, tmp_path):
    """A second session in the SAME dir shares the address on purpose (not a collision)."""
    mine = _mkdirs(tmp_path, "projects/shop2/webshop")
    _sibling(mesh_env, mine)

    presence.heartbeat(cwd=mine)

    assert _my_record(mesh_env)["project"] == "webshop"


def test_label_clash_falls_back_to_hash(mesh_env, tmp_path):
    """The computed readable label already taken by a live sibling → hash qualifier instead."""
    mine = _mkdirs(tmp_path, "projects/shop2/webshop")
    sib = _mkdirs(tmp_path, "other/webshop")
    _sibling(mesh_env, sib, project="webshop--shop2", project_base="webshop")

    presence.heartbeat(cwd=mine)

    rec = _my_record(mesh_env)
    assert rec["project"].startswith("webshop--")
    assert rec["project"] != "webshop--shop2"
    qual = rec["project"].split("--", 1)[1]
    assert len(qual) == 4 and all(c in "0123456789abcdef" for c in qual)


def test_resolver_returns_qualified_address(mesh_env, tmp_path):
    mine = _mkdirs(tmp_path, "projects/shop2/webshop")
    sib = _mkdirs(tmp_path, "projects/webshop/webshop")
    _sibling(mesh_env, sib)
    presence.heartbeat(cwd=mine)

    addr, source = presence.resolve_own_address()

    assert source == "session"
    assert addr == f"{os.getuid()}:webshop--shop2"


def test_qualifier_helper_readable_and_fallback():
    assert presence.path_qualifier("/p/shop2/x", ["/p/webshop/x"]) == "shop2"
    assert presence.path_qualifier("/alpha/x/proj", ["/beta/x/proj"]) == "alpha"
    # no distinguishing component at all (identical path) → None
    assert presence.path_qualifier("/p/a/x", ["/p/a/x"]) is None
