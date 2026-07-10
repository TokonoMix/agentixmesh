"""``mesh status`` console entry (design §17-dogfood) — **strictly read-only** own-mailbox introspection.

A quick look at the **own** ``uid:project`` address during the dogfood weeks: the address, the
mesh root, and the counts in ``new/``/``cur/``/``held/`` plus the number of ``seen/`` stamps. This
is **NOT** §6-presence/discovery (cross-user, phase 2) — nothing cross-user, and nothing that
mutates: it only counts/lists, never claims/renames/marks, and the janitor doesn't run.
"""

from __future__ import annotations

import os
import sys

from . import config, maildir


def _list_files(path: str):
    """Regular files in ``path`` (no subdirs); a missing dir counts as empty."""
    try:
        names = os.listdir(path)
    except OSError:
        return []
    return [n for n in names if os.path.isfile(os.path.join(path, n))]


def _count_messages(drop: str, sub: str) -> int:
    """Count messages in ``<drop>/<sub>``: regular files, no ``.`` temp and no ``.shown`` sidecars."""
    path = os.path.join(drop, sub)
    return sum(
        1
        for n in _list_files(path)
        if not n.startswith(".") and not n.endswith(maildir.SHOWN_SUFFIX)
    )


def _count_seen(drop: str) -> int:
    """Count ``seen/`` stamps (processed messages); the ``seen/turns/`` counter dir doesn't count."""
    return len(_list_files(os.path.join(drop, "seen")))


def gather(root=None) -> dict:
    """Collect the read-only status of the own address as a dict (no I/O mutation).

    The address comes from ``config.current_address()``; the drop path is assembled directly
    (NOT via ``maildir.maildrop`` — that would create subdirectories). Missing subdirectories
    count as ``0``.
    """
    address = config.current_address()
    base = root if root is not None else config.mesh_root()
    drop = os.path.join(base, address)
    return {
        "address": address,
        "root": base,
        "new": _count_messages(drop, "new"),
        "cur": _count_messages(drop, "cur"),
        "held": _count_messages(drop, "held"),  # Phase 1: always 0, but shown broker-ready
        "seen": _count_seen(drop),
    }


def main(argv=None) -> int:
    """Console entry: print the read-only status of the own address. Always returns ``0``."""
    info = gather()
    print(f"address:   {info['address']}")
    print(f"mesh-root: {info['root']}")
    print(f"new:  {info['new']}")
    print(f"cur:  {info['cur']}")
    print(f"held: {info['held']}")
    print(f"seen: {info['seen']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    from .group_reexec import reexec_under_mesh_group_if_needed
    reexec_under_mesh_group_if_needed("pm_mesh.status")
    raise SystemExit(main(sys.argv[1:]))
