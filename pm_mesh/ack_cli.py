"""AR-05: ``mesh ack <id>|--all`` — record an ack marker so AUTO re-notify skips the message.

The human is the only actor who can truly confirm presence (design §3.5). ``mesh ack`` writes the AR-02
``acks/`` marker (a dedicated subdir, NOT a ``cur/`` sidecar) so ``inject._renotify_recent_auto`` stops
re-surfacing that message early. ``--all`` acks every currently in-window unread AUTO/notify-only
message (the same scan the re-notify pass uses). Idempotent; an unknown id is a clean no-op.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import acks, config, maildir, presence, trust
from .read_cli import find_by_prefix, scan_cur, _sender_project


def _in_window_unread_auto_ids(address, my_uid, policy, now, root=None):
    """Enumerate ids of currently in-window, unacked, same-uid AUTO / notify-only ``cur/`` messages.

    Mirrors the ``inject._renotify_recent_auto`` eligibility scan (``.shown`` companion in the recency
    window + level in (AUTO, NOTIFY_ONLY) + not already acked) so ``--all`` acks exactly the set that
    re-notify would otherwise keep surfacing.
    """
    drop = maildir.maildrop(address, root=root)
    cur_dir = os.path.join(drop, "cur")
    ids = []
    for path, msg, owner_uid in scan_cur(address, root=root):
        try:
            shown_mtime = os.stat(path + maildir.SHOWN_SUFFIX).st_mtime
        except OSError:
            continue  # no .shown companion → never shown once → not a re-notify candidate
        if not config.within_window(shown_mtime, now, config.AUTO_RENOTIFY_WINDOW_S):
            continue
        level = trust.resolve(policy, owner_uid, _sender_project(msg.from_), my_uid, sender_verified=True)
        if level not in (trust.AUTO, trust.NOTIFY_ONLY):
            continue
        if acks.is_acked(address, msg.id, root=root):
            continue
        ids.append(msg.id)
    return ids


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh ack",
        description="Acknowledge an unread mesh message so it stops re-surfacing "
                    "(by id prefix, or --all for every in-window unread AUTO message).",
    )
    p.add_argument("id", nargs="?", help="message-id prefix to ack (the <msgid8> from a reminder)")
    p.add_argument("--all", action="store_true",
                   help="ack every currently in-window unread AUTO/notify-only message")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    # Distinguish an OMITTED id (argparse → None) from an explicit empty prefix (""), which is a
    # valid — if degenerate — prefix that matches everything and must reach the ambiguous branch.
    if sum([bool(args.all), args.id is not None]) > 1:
        sys.stderr.write("mesh ack: give an id OR --all, not both.\n")
        return 2
    if not args.all and args.id is None:
        sys.stderr.write("mesh ack: give a message-id prefix, or --all.\n")
        return 2

    address = presence.resolve_own_address()[0]


    if args.all:
        my_uid = os.geteuid()
        policy = trust.load_policy_or_default(trust.policy_path())
        ids = _in_window_unread_auto_ids(address, my_uid, policy, time.time())
        n = acks.ack_all(address, ids)
        print(f"mesh ack: acked {n} unread message(s).")
        return 0

    # Single id: resolve the prefix against cur/ (unique-prefix), then ack the FULL id.
    status, payload = find_by_prefix(address, args.id)
    if status == "none":
        print(f"mesh ack: no message in cur/ matching '{args.id}' — nothing to ack.")
        return 0  # clean no-op (idempotent semantics)
    if status == "ambiguous":
        sys.stderr.write(
            f"mesh ack: ambiguous prefix '{args.id}' matches {payload} messages — "
            "use more characters.\n"
        )
        return 1
    msg, _ = payload
    acks.ack(address, msg.id)
    print(f"mesh ack: acked {msg.id[:8]}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
