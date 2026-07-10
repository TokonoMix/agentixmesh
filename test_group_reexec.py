"""Tests for pm_mesh.group_reexec — auto-acquire the ``mesh`` group via ``sg`` when a
process that predates the group-add needs to reach the shared ``/srv/mesh`` root.

Why this exists: the go-live inject-hook runs ``MESH_ROOT=/srv/mesh mesh-inject`` WITHOUT
the ``mesh`` gid in already-running sessions -> PermissionError -> fail-closed silence.
This helper re-execs the entrypoint under ``sg mesh`` (member-only, no password, no uid
change -> identity stays kernel-verified) so those sessions work without a fresh login.

``sg`` drops positional args (verified empirically) but PRESERVES the environment, so argv
is smuggled through an env var as base64 of NUL-joined UTF-8 bytes -> shell-injection-safe.
"""
import pytest
from pm_mesh import group_reexec as gr


# --- argv codec: must survive spaces, shell metachars, newlines, unicode -----------------

@pytest.mark.parametrize("argv", [
    [],
    ["1001:pm-mesh", "hello"],
    ["1001:x", "a b c"],
    ["1001:x", "x;whoami", "$(id)", "`reboot`"],
    ["1001:x", "line1\nline2\twith\ttabs"],
    ["1001:x", "emoji 🚀 en accenten café"],
    ["--thread", "abc", "1001:x", ""],
])
def test_encode_decode_roundtrip(argv):
    enc = gr.encode_argv(argv)
    assert isinstance(enc, str)
    assert gr.decode_argv(enc) == argv


def test_encode_is_opaque_no_shell_metachars_leak():
    # base64 alphabet only -> nothing the shell could interpret
    enc = gr.encode_argv(["x;whoami", "$(id)"])
    assert all(c.isalnum() or c in "+/=" for c in enc)


def test_decode_empty_is_empty_list():
    assert gr.decode_argv("") == []


# --- plan: the pure decision function (no side effects) -----------------------------------

MODULE = "pm_mesh.send"
MESH_GID = 985


def plan(**kw):
    base = dict(
        reexec_flag="", argv_env="", shared=True, mesh_gid=MESH_GID,
        current_groups=[1001, 100], argv=["1001:x", "hi"], module=MODULE,
    )
    base.update(kw)
    return gr._plan_reexec(**base)


def test_reexec_copy_restores_argv_and_proceeds():
    enc = gr.encode_argv(["1001:x", "a b", "x;id"])
    p = plan(reexec_flag="1", argv_env=enc)
    assert p == {"restore_argv": ["1001:x", "a b", "x;id"]}


def test_already_in_group_proceeds_directly():
    assert plan(current_groups=[1001, MESH_GID, 100]) is None


def test_local_root_never_reexecs_even_without_group():
    # same-user / local root needs no group -> must stay byte-identical (no re-exec)
    assert plan(shared=False, current_groups=[1001]) is None


def test_no_mesh_group_on_host_proceeds():
    assert plan(mesh_gid=None, current_groups=[1001]) is None


def test_shared_root_missing_group_plans_sg_reexec():
    p = plan(shared=True, mesh_gid=MESH_GID, current_groups=[1001, 100],
             argv=["1001:x", "a b"], module=MODULE)
    assert p is not None and "exec" in p
    assert p["exec"] == ["sg", "mesh", "-c", f"exec python3 -m {MODULE}"]
    # the guard flag + argv are carried through the environment (sg preserves env)
    assert p["env_updates"]["_MESH_SG_REEXEC"] == "1"
    assert gr.decode_argv(p["env_updates"]["_MESH_ARGV"]) == ["1001:x", "a b"]


def test_reexec_flag_takes_precedence_over_missing_group():
    # a re-exec'd copy that STILL lacks the group must NOT loop -> restore & proceed
    enc = gr.encode_argv(["1001:x"])
    p = plan(reexec_flag="1", argv_env=enc, current_groups=[1001], mesh_gid=MESH_GID)
    assert p == {"restore_argv": ["1001:x"]}


def test_module_name_is_reflected_in_sg_command():
    p = plan(module="pm_mesh.inject", current_groups=[1001])
    assert p["exec"][-1] == "exec python3 -m pm_mesh.inject"
