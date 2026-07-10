"""``mesh who [user]`` / ``mesh list`` (phase 2, f2-07) — discovery of active sessions.

Shows active ``(user, project)`` sessions from the presence layer (f2-06), **visible only to
fellow group members** (design §6, default a): a caller only sees presence for addresses it
shares at least one group with (f2-08); the **head leader** sees everything (monitoring). Expired
heartbeats are not shown. Read-only: mutates nothing (does not touch the presence-dir perms).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import config, groups, presence


def active_sessions(root=None, now=None, max_age_s: int = presence.DEFAULT_MAX_AGE_S) -> list:
    """Read all **online** heartbeats from ``presence/`` (read-only). Expired/unreadable ones skipped."""
    base = root if root is not None else config.mesh_root()
    pdir = os.path.join(base, presence.PRESENCE_SUBDIR)
    if now is None:
        now = time.time()
    out = []
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        return []
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(pdir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                hb = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(hb, dict) and presence.is_online(hb, now, max_age_s):
            out.append(hb)
    return out


def visible_sessions(
    caller_uid: int, root=None, now=None, max_age_s: int = presence.DEFAULT_MAX_AGE_S, groups_cfg=None
) -> list:
    """The online sessions ``caller_uid`` may see: own sessions, fellow group members, or everything (head leader)."""
    cfg = groups_cfg if groups_cfg is not None else groups.load_groups(root)
    head = groups.is_head_leader(cfg, caller_uid)
    result = []
    for hb in active_sessions(root, now, max_age_s):
        owner = hb.get("user")
        try:
            owner = int(owner)
        except (TypeError, ValueError):
            continue
        if head or owner == caller_uid or groups.shares_group(cfg, caller_uid, owner):
            result.append(hb)
    return result


def _fmt(hb) -> str:
    return (
        f"{hb.get('user')}:{hb.get('project')}  cwd={hb.get('cwd')}  pid={hb.get('pid')}"
        f"  since {hb.get('started')}  last {hb.get('last_seen')}"
    )


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _uid_of(arg: str):
    """A bare uid, or a name/alias resolved via the address book to its uid."""
    n = _as_int(arg)
    if n is not None:
        return n
    try:
        from . import addressbook
        addr = addressbook.load().resolve(arg)
        if addr:
            return _as_int(addr.split(":", 1)[0])
    except Exception:
        pass
    return None


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-who",
        description="Show active sessions (fellow group members only; head leader sees everything).",
    )
    p.add_argument("user", nargs="?",
                   help="optional: filter on uid, or a name/alias from the address book "
                        "(e.g. 'reviewer' → shows the reviewer's live sessions)")
    return p


def main(argv=None) -> int:
    """Console entry; print the visible active sessions. Always returns 0."""
    args = _build_parser().parse_args(argv)
    caller = os.geteuid()
    sessions = visible_sessions(caller)
    if args.user is not None:
        want = _uid_of(args.user)
        if want is None:
            print(f"mesh-who: unknown user/name: {args.user!r} "
                  f"(uid, or a name from mesh-resolve --list)", file=sys.stderr)
            return 2
        sessions = [hb for hb in sessions if _as_int(hb.get("user")) == want]
    for hb in sorted(sessions, key=lambda h: (h.get("user", 0), str(h.get("project")))):
        print(_fmt(hb))
    return 0


if __name__ == "__main__":  # pragma: no cover
    # acquire the mesh group like send/inject, so `mesh-who` can read
    # /srv/mesh/presence from a shell started before the group membership
    from .group_reexec import reexec_under_mesh_group_if_needed
    reexec_under_mesh_group_if_needed("pm_mesh.who")
    raise SystemExit(main())
