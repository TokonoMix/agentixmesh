"""``mesh revoke <uid>`` + offboarding/cleanup (phase 2, f2-12, closes F6).

Cleans a user **out** of the mesh: removes them from all group configs, cleans up their presence
heartbeats, and removes outstanding ``held/`` messages from/to that uid (with an audit entry). The
OS-level step (removing them from group ``mesh``) is a **human/sudo action** — documented in
``CROSS-USER-SETUP.md`` — because without group ``mesh`` dropping won't work anyway (the drop
perms rely on that group). **Idempotent**: a second run finds nothing left and does nothing (no crash).

Authorization: only the **head leader** or a **manager** (role from the group config, kernel uid).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

from . import audit, config, consent, groups, maildir


class RevokeError(Exception):
    """Revoke could not be carried out (not authorized)."""


def _authorized(cfg, revoker_uid: int) -> bool:
    """Only the head leader or a manager (role from the config) may revoke."""
    return groups.is_head_leader(cfg, revoker_uid) or revoker_uid in groups.manager_uids(cfg)


def _remove_from_groups(cfg, target_uid: int):
    """Return a new config with ``target_uid`` removed from every group; return ``(cfg, [names])``.

    A group that becomes invalid as a result (<2 members, or the manager was the removed uid) is
    removed entirely so the config stays valid.
    """
    new = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    gdict = new.get("groups")
    if not isinstance(gdict, dict):
        return new, []
    touched = []
    for name in list(gdict.keys()):
        group = gdict[name]
        members = group.get("members", []) if isinstance(group, dict) else []
        kept = [m for m in members if groups._entry_uid(m) != target_uid]
        if len(kept) != len(members):
            touched.append(name)
        mgr_uid = groups._entry_uid(group.get("manager")) if isinstance(group, dict) else None
        kept_uids = {groups._entry_uid(m) for m in kept}
        if len(kept) < 2 or mgr_uid == target_uid or mgr_uid not in kept_uids:
            del gdict[name]  # group becomes invalid -> remove entirely
        else:
            group["members"] = kept
    return new, touched


def _clean_presence(base: str, target_uid: int) -> int:
    """Remove all heartbeat files where ``user == target_uid``; return the count."""
    pdir = os.path.join(base, "presence")
    removed = 0
    try:
        names = os.listdir(pdir)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(pdir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                hb = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(hb, dict) and hb.get("user") == target_uid:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed


def _clean_held(base: str, target_uid: int) -> int:
    """Remove outstanding ``held/`` messages **from** (kernel owner) or **to** (address uid) the target.

    An audit entry for every removed message. Return the count removed."""
    removed = 0
    try:
        entries = os.listdir(base)
    except OSError:
        return 0
    for entry in entries:
        drop = os.path.join(base, entry)
        held_dir = os.path.join(drop, "held")
        if not os.path.isdir(held_dir):
            continue
        try:
            to_target = config.parse_address(entry)[0] == target_uid
        except ValueError:
            to_target = False
        for name in os.listdir(held_dir):
            if name.startswith(".") or name.endswith(maildir.SHOWN_SUFFIX):
                continue
            path = os.path.join(held_dir, name)
            if not os.path.isfile(path):
                continue
            remove_it = to_target
            if not remove_it:
                try:
                    remove_it = os.lstat(path).st_uid == target_uid  # from target (owner)
                except OSError:
                    continue
            if remove_it:
                audit.append(
                    "revoke_held_removed", root=base, address=entry, file=name, target_uid=target_uid
                )
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
    return removed


def revoke(target_uid: int, root=None, revoker_uid=None) -> dict:
    """Clean ``target_uid`` out of the mesh. Return a cleanup summary. Idempotent."""
    if revoker_uid is None:
        revoker_uid = os.geteuid()
    base = root if root is not None else config.mesh_root()
    cfg = groups.load_groups(base)

    if not _authorized(cfg, revoker_uid):
        raise RevokeError(
            f"uid {revoker_uid} is not a head leader/manager — may not revoke"
        )

    new_cfg, touched = _remove_from_groups(cfg, target_uid)
    if touched:
        groups.save_config(new_cfg, root=base, editor_uid=revoker_uid)

    summary = {
        "target_uid": target_uid,
        "groups_removed_from": touched,
        "presence_removed": _clean_presence(base, target_uid),
        "held_removed": _clean_held(base, target_uid),
        # Revocation link (f2-14): offboarding = leader-read consent void.
        "consent_revoked": consent.revoke_consent(target_uid, root=base),
    }
    audit.append("revoke", root=base, revoker_uid=revoker_uid, **summary)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-revoke",
        description="Clean a uid out of the mesh (groups, presence, outstanding held/).",
    )
    p.add_argument("uid", type=int, help="the uid to revoke")
    return p


def main(argv=None) -> int:
    """Console entry; exit 0 = done, !=0 = clean error. Don't forget the OS step (see runbook)."""
    args = _build_parser().parse_args(argv)
    try:
        summary = revoke(args.uid)
    except RevokeError as exc:
        print(f"mesh-revoke: {exc}", file=sys.stderr)
        return 1
    print(
        f"revoked uid {args.uid}: groups={summary['groups_removed_from']} "
        f"presence={summary['presence_removed']} held={summary['held_removed']}"
    )
    print(
        f"NOTE (OS step, sudo): also remove the user from group 'mesh' — `gpasswd -d <user> mesh` — "
        f"otherwise they keep OS-level drop access. See pm_mesh/CROSS-USER-SETUP.md."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
