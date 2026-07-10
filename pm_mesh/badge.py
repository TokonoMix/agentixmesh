"""``mesh-badge`` — harness-independent status-bar indicator (unread/awaiting-approval).

Meant to be callable by ANY agent harness or status bar, not just Claude Code. Reuses the counts
from ``status.gather`` (the same read-only ``new/``/``held/`` introspection) and adds only
``senders``: the set of **kernel-verified** sender uids (``identity``, ``fstat``-on-open-fd — see
``identity.open_verified``), NEVER a project label/subject/body content.

Fail-closed as a status-bar citizen (this must never break a status bar): any error during
``gather`` or formatting yields empty output + exit 0, no traceback — unless ``--debug`` is set,
which lets the error propagate for diagnosis.

Strictly read-only: only counting/listing/open-for-fstat, never claiming/moving/seen-stamping.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import config, identity, status

#: Emoji labels for the default status-bar line.
_EMOJI_NEW = "\U0001f4ec"  # 📬
_EMOJI_HELD = "⏸"  # ⏸


def _sender_uids(drop: str, subdirs) -> set:
    """Kernel-verified sender uids from ``<drop>/<subdirs>`` (read-only, best-effort).

    Reuses ``status._list_files`` for the same filtering (skip dotfiles/``.shown`` sidecars) as
    ``status.gather``. A file that isn't kernel-verifiable (symlink/hardlink/corrupt — see
    ``identity.IdentityError``) is silently skipped: that's a counting detail, not a fatal error.
    """
    uids = set()
    for sub in subdirs:
        path = os.path.join(drop, sub)
        for name in status._list_files(path):
            try:
                fd, uid = identity.open_verified(os.path.join(path, name))
            except identity.IdentityError:
                continue
            os.close(fd)
            uids.add(uid)
    return uids


def gather(root=None) -> dict:
    """Collect the badge info as a dict: ``new``, ``held``, ``senders`` (str uids), ``address``.

    The ``new``/``held`` counts come straight from ``status.gather`` (maximal reuse of the
    existing read-only mailbox view); ``senders`` is an addition on top of it.
    """
    info = status.gather(root=root)
    base = root if root is not None else config.mesh_root()
    drop = os.path.join(base, info["address"])
    uids = _sender_uids(drop, ("new", "held"))
    return {
        "new": info["new"],
        "held": info["held"],
        "senders": [str(u) for u in sorted(uids)],
        "address": info["address"],
    }


def format_text(info: dict, emoji: bool = True) -> str:
    """One short status-bar line; ``""`` if there's nothing to report (new==0 and held==0)."""
    parts = []
    if info["new"]:
        parts.append(f"{_EMOJI_NEW} {info['new']}" if emoji else f"new:{info['new']}")
    if info["held"]:
        parts.append(f"{_EMOJI_HELD} {info['held']}" if emoji else f"held:{info['held']}")
    if not parts:
        return ""
    return " · ".join(parts) if emoji else " ".join(parts)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-badge",
        description="Harness-independent status-bar badge: unread + awaiting-approval.",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON instead of a text line")
    p.add_argument("--no-emoji", action="store_true", help="pure ASCII output (no emoji font needed)")
    p.add_argument(
        "--debug", action="store_true",
        help="actually show errors (traceback) instead of the normal fail-closed empty output — diagnosis only",
    )
    return p


def main(argv=None) -> int:
    """Console entry. Fail-closed: any error -> empty output + exit 0, unless ``--debug``."""
    args = _build_parser().parse_args(argv)
    try:
        info = gather()
        if args.json:
            print(json.dumps(info))
        else:
            text = format_text(info, emoji=not args.no_emoji)
            if text:
                print(text)
    except Exception:
        if args.debug:
            raise
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    from .group_reexec import reexec_under_mesh_group_if_needed
    reexec_under_mesh_group_if_needed("pm_mesh.badge")
    raise SystemExit(main(sys.argv[1:]))
