"""Auto-acquire the ``mesh`` group via ``sg`` for processes that predate the group-add.

Problem this solves
-------------------
After go-live the inject-hook runs ``MESH_ROOT=/srv/mesh mesh-inject``. A session whose
process started BEFORE ``claude`` was added to group ``mesh`` (gid 985) does not carry the
supplementary group, so it gets ``PermissionError`` traversing ``/srv/mesh`` -> the hook
fails closed -> the session silently reads nothing. A fresh login fixes it, but that is
operationally annoying. This module lets the entrypoint re-acquire the group in-process.

Why this is safe (no trust weakening)
-------------------------------------
``sg mesh`` only adds a supplementary GROUP the user already legitimately holds
(``/etc/group`` lists ``claude`` in ``mesh``). It does NOT change the uid: identity stays
kernel-verified per message via ``fstat``; the human-gate, the UNTRUSTED project label and
the cross-user hard-clamp are untouched. It only materialises access to a shared directory
the user is already authorised for.

Why argv goes through the environment
-------------------------------------
``sg group -c "cmd"`` runs ``cmd`` but DROPS extra positional args (verified empirically).
It does, however, preserve the environment verbatim (no shell interpretation). So argv is
smuggled as base64 of NUL-joined UTF-8 bytes in ``_MESH_ARGV`` and restored in the re-exec'd
copy -> injection-safe for arbitrary message bodies (spaces, ``;``, ``$(...)``, newlines).

Only triggers on the shared root (``cross_user_enabled()``); same-user/local stays
byte-identical to fase 1 (no re-exec, no ``sg`` dependency).
"""
from __future__ import annotations

import base64
import grp
import os
import sys

MESH_GROUP = "mesh"

_REEXEC_FLAG = "_MESH_SG_REEXEC"
_ARGV_ENV = "_MESH_ARGV"


def encode_argv(argv):
    """Serialise an argv list to an opaque base64 string (survives any bytes/metachars)."""
    joined = b"\x00".join(a.encode("utf-8") for a in argv)
    return base64.b64encode(joined).decode("ascii")


def decode_argv(enc):
    """Inverse of :func:`encode_argv`. Empty string -> empty list (not ``['']``)."""
    if not enc:
        return []
    raw = base64.b64decode(enc.encode("ascii"))
    return [part.decode("utf-8") for part in raw.split(b"\x00")]


def _mesh_gid():
    """gid of group ``mesh``, or ``None`` if the host has no such group."""
    try:
        return grp.getgrnam(MESH_GROUP).gr_gid
    except KeyError:
        return None


def _plan_reexec(*, reexec_flag, argv_env, shared, mesh_gid, current_groups, argv, module):
    """Pure decision. Returns one of:

    * ``{"restore_argv": [...]}``  -> we are the re-exec'd copy; restore argv and proceed.
    * ``{"exec": [...], "env_updates": {...}}`` -> re-exec under ``sg`` to gain the group.
    * ``None`` -> proceed unchanged (already in group / local root / no mesh group / etc.).
    """
    # A re-exec'd copy always restores + proceeds first, so it can NEVER loop even if the
    # group still isn't present (then it proceeds and fails loud, which is correct).
    if reexec_flag == "1":
        return {"restore_argv": decode_argv(argv_env)}
    # Same-user / local root needs no group -> never touch fase-1 behaviour.
    if not shared:
        return None
    # Host without a mesh group -> nothing to acquire.
    if mesh_gid is None:
        return None
    # Already carry the group -> proceed directly.
    if mesh_gid in current_groups:
        return None
    # Shared root, group missing, group exists on host -> re-exec under sg to acquire it.
    return {
        "exec": ["sg", MESH_GROUP, "-c", f"exec python3 -m {module}"],
        "env_updates": {_REEXEC_FLAG: "1", _ARGV_ENV: encode_argv(argv)},
    }


def reexec_under_mesh_group_if_needed(module):
    """Glue: gather live state, ask :func:`_plan_reexec`, and act.

    Call this at the TOP of an entrypoint's ``__main__`` block, BEFORE ``main()`` reads argv.
    On the re-exec path it does not return (``execvpe`` replaces the process); on the restore
    path it rewrites ``sys.argv`` and returns; otherwise it returns immediately.
    """
    from . import config

    plan = _plan_reexec(
        reexec_flag=os.environ.get(_REEXEC_FLAG, ""),
        argv_env=os.environ.get(_ARGV_ENV, ""),
        shared=config.cross_user_enabled(),
        mesh_gid=_mesh_gid(),
        current_groups=os.getgroups(),
        argv=list(sys.argv[1:]),
        module=module,
    )
    if plan is None:
        return
    if "restore_argv" in plan:
        sys.argv = [sys.argv[0]] + plan["restore_argv"]
        for key in (_REEXEC_FLAG, _ARGV_ENV):
            os.environ.pop(key, None)
        return
    env = dict(os.environ)
    env.update(plan["env_updates"])
    try:
        os.execvpe(plan["exec"][0], plan["exec"], env)
    except OSError:
        # sg missing / not runnable -> proceed unchanged; the later maildrop access will
        # fail loud with PermissionError rather than silently, which is the honest signal.
        return
