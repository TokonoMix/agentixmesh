"""``mesh-addressbook-add`` — write one entry into the shared address book.

Thin CLI over :func:`addressbook.upsert_entry` (merge-never-overwrite). Meant to be
called as an enroll/onboarding sub-step so a new address (``uid:project``) lands in
the book automatically instead of a manual steward edit.

The book defaults to ``$MESH_ROOT/addressbook.json`` (the shared team layer). Exit 0 on
success, 2 on a bad address / unwritable-or-corrupt book — never partial-writes.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import addressbook, config


def _default_book() -> str:
    try:
        root = config.mesh_root()
    except Exception:
        root = os.environ.get("MESH_ROOT", "")
    return os.path.join(root, "addressbook.json") if root else ""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-addressbook-add",
        description="Add or extend one uid:project entry in the address book "
                    "(merge-never-overwrite).")
    p.add_argument("address", help="canonical uid:project address, e.g. 1100:backend")
    p.add_argument("--display", default="", help="human display name")
    p.add_argument("--dir", default="", help="on-disk project directory")
    p.add_argument("--alias", action="append", default=[], dest="aliases",
                   help="friendly alias (repeatable)")
    p.add_argument("--note", default="", help="human annotation, stored as $note (ignored by the resolver)")
    p.add_argument("--book", default=None, help="book path (default: $MESH_ROOT/addressbook.json)")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    book = args.book or _default_book()
    if not book:
        print("mesh-addressbook-add: no book path — set --book or $MESH_ROOT", file=sys.stderr)
        return 2
    try:
        res = addressbook.upsert_entry(
            args.address, display=args.display, dir=args.dir,
            aliases=args.aliases, note=args.note, book_path=book)
    except ValueError as exc:
        print(f"mesh-addressbook-add: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"mesh-addressbook-add: cannot write {book}: {exc}", file=sys.stderr)
        return 2
    if res["created"]:
        print(f"created {args.address} in {book}")
    elif res["updated_fields"]:
        print(f"extended {args.address} ({', '.join(res['updated_fields'])}) in {book}")
    else:
        print(f"no change: {args.address} already complete in {book}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
