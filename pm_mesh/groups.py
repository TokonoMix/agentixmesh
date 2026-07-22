"""Groups & roles (phase 2, f2-08) — group config, manager per group, one head leader. Design §9.

Underpins visibility (f2-07) and the leader-gate (f2-09).

* A **group** = >=2 member addresses (``uid:project``) + one **manager** address (itself a member). E.g.
  ``roof-coordination = {1100:backend, 1200:reviews}``, manager ``1200:reviews``.
* **Head leader** (``head_leader`` uid, one fixed; ``$MESH_HEAD_LEADER`` override) sees everything
  (monitoring) and may edit anywhere.
* **Edit rights = manager + head leader, enforced via file ownership/perms** (not via a
  self-declared role): the config is written ``0644`` (owner-write, group/other read), and
  ``load_config`` refuses a config that is group/other-**writable** or whose **owner is not**
  a manager/head leader that the config itself declares. A plain member can't modify the config this way.

Member extraction accepts both ``"uid:project"`` addresses and bare uids; membership for
visibility keys on the **uid** (project is freely chosen and must not determine identity).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile

from . import config

#: Filename of the group config in the mesh root.
GROUPS_FILE = "groups.json"


class GroupError(Exception):
    """The group config is invalid (too few members, no/non-member manager) or unsafe (perm/owner)."""


def groups_path(root=None) -> str:
    base = root if root is not None else config.mesh_root()
    return os.path.join(base, GROUPS_FILE)


# ---------------------------------------------------------------------------
# pure query helpers (member extraction tolerant: address or bare uid → uid)
# ---------------------------------------------------------------------------
def _entry_uid(entry):
    """Extract the uid from a member/manager entry (``"uid:project"`` or bare uid); ``None`` if malformed."""
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    if isinstance(entry, str):
        try:
            return config.parse_address(entry)[0]
        except ValueError:
            try:
                return int(entry)
            except ValueError:
                return None
    return None


def _all_groups(cfg) -> dict:
    g = cfg.get("groups") if isinstance(cfg, dict) else None
    return g if isinstance(g, dict) else {}


def _member_entries(group):
    members = group.get("members") if isinstance(group, dict) else None
    return members if isinstance(members, list) else []


def _member_uids(group) -> set:
    out = set()
    for m in _member_entries(group):
        uid = _entry_uid(m)
        if uid is not None:
            out.add(uid)
    return out


def members(cfg, group_name: str) -> list:
    """The member entries of a group, as stored (addresses or uids)."""
    return list(_member_entries(_all_groups(cfg).get(group_name)))


def manager(cfg, group_name: str):
    """The manager entry of a group, as stored (address or uid), or ``None``."""
    group = _all_groups(cfg).get(group_name)
    return group.get("manager") if isinstance(group, dict) else None


def manager_of(cfg, group_name: str):
    """The manager **uid** of a group, or ``None``."""
    return _entry_uid(manager(cfg, group_name))


def manager_uids(cfg) -> set:
    """All manager uids across all groups."""
    out = set()
    for name in _all_groups(cfg):
        uid = manager_of(cfg, name)
        if uid is not None:
            out.add(uid)
    return out


def shares_group(cfg, uid_a: int, uid_b: int) -> bool:
    """``True`` if ``uid_a`` and ``uid_b`` share at least one common group."""
    for group in _all_groups(cfg).values():
        m = _member_uids(group)
        if uid_a in m and uid_b in m:
            return True
    return False


def head_leader(cfg):
    """The head-leader uid: ``$MESH_HEAD_LEADER`` (override) or ``cfg['head_leader']``; ``None`` if none."""
    env = os.environ.get("MESH_HEAD_LEADER")
    if env:
        try:
            return int(env)
        except ValueError:
            return None
    if isinstance(cfg, dict):
        return _entry_uid(cfg.get("head_leader"))
    return None


def is_head_leader(cfg, uid: int) -> bool:
    hl = head_leader(cfg)
    return hl is not None and hl == uid


#: Alias — the ticket calls the helper ``is_leader``.
def is_leader(cfg, uid: int) -> bool:
    return is_head_leader(cfg, uid)


def groups_of(cfg, address_or_uid) -> list:
    """Names of the groups the address/uid is a member of (sorted)."""
    uid = _entry_uid(address_or_uid)
    if uid is None:
        return []
    return sorted(name for name, g in _all_groups(cfg).items() if uid in _member_uids(g))


def member_uids(cfg) -> set:
    out = set()
    for group in _all_groups(cfg).values():
        out |= _member_uids(group)
    return out


# ---------------------------------------------------------------------------
# validation + edit authorization
# ---------------------------------------------------------------------------
def validate_group(group) -> None:
    """Raise ``GroupError`` if a group is invalid: <2 members, no manager, or manager not a member."""
    member_list = _member_entries(group)
    uids = _member_uids(group)
    if len(member_list) < 2 or len(uids) < 2:
        raise GroupError("group has <2 (valid) members")
    mgr = group.get("manager") if isinstance(group, dict) else None
    mgr_uid = _entry_uid(mgr)
    if mgr_uid is None:
        raise GroupError("group has no (valid) manager")
    if mgr_uid not in uids:
        raise GroupError("manager must itself be a member of the group")


def validate_config(cfg) -> None:
    """Raise ``GroupError`` if any group in the config is invalid."""
    for name, group in _all_groups(cfg).items():
        try:
            validate_group(group)
        except GroupError as exc:
            raise GroupError(f"group {name!r}: {exc}") from exc


def authorize_edit(cfg, editor_uid: int) -> bool:
    """May ``editor_uid`` modify the group config? Only the head leader or a manager (role from config)."""
    return is_head_leader(cfg, editor_uid) or editor_uid in manager_uids(cfg)


def load_config(root=None) -> dict:
    """Read + validate the group config (fail-closed). ``{}`` if it doesn't exist.

    Refuses (``GroupError``): symlink, non-regular file, **group/other-writable**, an owner who is
    not a manager/head leader that the config itself declares, or a structurally invalid group.
    """
    path = groups_path(root)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise GroupError(f"cannot safely open group config {path!r}: {exc}") from exc

    fh = None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise GroupError(f"group config {path!r} is not a regular file")
        if stat.S_IMODE(st.st_mode) & 0o022:
            raise GroupError(f"group config {path!r} is group/other-writable (refused)")
        fh = os.fdopen(fd, encoding="utf-8")
        data = json.load(fh)
    except ValueError as exc:
        raise GroupError(f"group config {path!r} is not valid JSON: {exc}") from exc
    finally:
        if fh is not None:
            fh.close()
        else:
            os.close(fd)

    if not isinstance(data, dict):
        raise GroupError(f"group config {path!r} is not an object")
    validate_config(data)
    # Owner must be a role that the config itself declares (manager or head leader).
    owner = st.st_uid
    if not (is_head_leader(data, owner) or owner in manager_uids(data)):
        raise GroupError(
            f"group config {path!r} does not belong to a manager/head leader (owner uid={owner})"
        )
    return data


def load_groups(root=None) -> dict:
    """Fail-safe reader for read-only use (f2-07 visibility): any error → ``{}`` (no crash)."""
    try:
        return load_config(root)
    except GroupError:
        return {}


def save_config(cfg, root=None, editor_uid=None) -> str:
    """Write the group config — only if ``editor_uid`` is manager/head leader. Atomic, ``0644``.

    Validates the config first (no invalid group gets written) and the edit rights (role from
    the config being written). The ``0644`` perms + ownership are the filesystem enforcement: a plain
    member cannot overwrite the file afterwards.
    """
    if editor_uid is None:
        editor_uid = os.geteuid()
    validate_config(cfg)
    if not authorize_edit(cfg, editor_uid):
        raise GroupError(
            f"uid {editor_uid} is not manager/head leader — may not modify the group config"
        )
    path = groups_path(root)
    directory = os.path.dirname(path)
    data = json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".groups-", dir=directory)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
