"""``mesh-send`` console entry (design §18) — **phase 1: same-user**.

Builds a message from the own address (``config.current_address()``) to a given destination
address and delivers it atomically into their inbox (``maildir.deliver``). No cross-user or
permission logic: that's phase 2.
"""

from __future__ import annotations

import argparse
import sys

import os

from . import audit, config, maildir, message


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-send",
        description="Send a message to <to-address> (same-user mesh).",
    )
    p.add_argument("to", help="destination address in the form <uid>:<project>")
    p.add_argument("body", nargs="?", help="message body; omitted -> read from stdin")
    p.add_argument("--kind", default="request", help=f"kind (default request); one of {', '.join(message.KINDS)}")
    p.add_argument("--thread", default=None, help="thread id (default = new message id)")
    p.add_argument(
        "--subject", default=None,
        help=f"optional subject (sender-claimed, DATA; truncated above {message.SUBJECT_MAX_LEN} characters)",
    )
    return p


def main(argv=None) -> int:
    """Console entry; returns an exit code (0 = success, !=0 = failure)."""
    args = _build_parser().parse_intermixed_args(argv)

    # Resolve a friendly name/alias to a canonical uid:project (sender-side
    # convenience only — the receive-side identity stays kernel-verified, so an
    # alias can never forge who a message is from). A bare address passes through
    # unchanged; an unknown non-address stays as-is and fails validation below
    # with a clear error.
    try:
        from . import addressbook
        resolved = addressbook.load().resolve(args.to)
        if resolved:
            args.to = resolved
    except Exception:
        pass  # address book is best-effort; never block a send on it

    # Validate the destination address before writing anything.
    try:
        config.parse_address(args.to)
    except ValueError as exc:
        print(f"mesh-send: invalid destination address: {exc} "
              f"(tip: mesh-resolve --list shows known names/aliases)", file=sys.stderr)
        return 2

    body = args.body if args.body is not None else sys.stdin.read()

    msg = message.new_message(
        args.to, body, kind=args.kind, thread=args.thread, from_=config.current_address(),
        subject=args.subject,
    )

    # Enforce body limit / kind / form before delivery.
    try:
        message.validate(msg)
    except ValueError as exc:
        print(f"mesh-send: invalid message: {exc}", file=sys.stderr)
        return 2

    try:
        maildir.deliver(msg)
    except OSError as exc:
        print(f"mesh-send: delivery failed: {exc}", file=sys.stderr)
        return 1

    # Central audit (f2-15) — no body text, best-effort.
    audit.append(
        "send",
        sender_uid=os.geteuid(),
        from_=msg.from_,
        to=msg.to,
        thread=msg.thread,
        id=msg.id,
        kind=msg.kind,
    )
    print(msg.id)
    return 0


if __name__ == "__main__":  # pragma: no cover
    from .group_reexec import reexec_under_mesh_group_if_needed
    reexec_under_mesh_group_if_needed("pm_mesh.send")
    raise SystemExit(main())
