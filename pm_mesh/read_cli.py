"""AR-05: ``mesh read <id>`` — re-render a consumed message from ``cur/`` on demand.

The AUTO re-notify reminder (``inject._renotify_recent_auto``) is metadata-only by design (a cost/noise
choice — the same-uid AUTO body was already shown once at delivery). ``mesh read`` lets a human pull the
full body back on demand, addressing the message by its short id prefix.

Trust invariants (design §3.6) — NOT weakened here:

* **``cur/`` ONLY.** Never scans ``held/`` (that is ``mesh approve``'s domain). A withheld body must
  never be reachable through ``mesh read``.
* **Re-resolve trust on the kernel-verified owner_uid** and render the **full body ONLY for AUTO**. For
  a **notify-only** message re-render the SAME capped preview ``frame.render_notify`` first showed —
  NEVER the withheld full body (else it bypasses the notify cap = a leak). Anything gated/held/BLOCK
  (which should never sit in ``cur/`` anyway) renders nothing.
* **Unique-prefix enforcement.** 8 hex = 32 bits, do not assume unique: 0 matches → clean error, >1
  matches → ambiguous error. Never guess.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import config, frame, identity, maildir, message, presence, trust


def _sender_project(from_) -> str:
    """Extract the (UNTRUSTED) project label from ``from``; ``""`` if unparseable (mirrors inject)."""
    try:
        _, project = config.parse_address(from_)
        return project
    except (ValueError, TypeError, AttributeError):
        return ""


def scan_cur(address: str, root=None):
    """Yield ``(path, msg, owner_uid)`` for each verifiable ``cur/`` message. Strictly non-consuming.

    Kernel-verified read via ``identity.read_verified`` (same as inject/approve); a corrupt or
    unverifiable entry is skipped. Never touches ``held/``.
    """
    drop = maildir.maildrop(address, root=root)
    cur_dir = os.path.join(drop, "cur")
    try:
        names = sorted(os.listdir(cur_dir))
    except OSError:
        return
    for name in names:
        if name.startswith(".") or name.endswith(maildir.SHOWN_SUFFIX):
            continue
        path = os.path.join(cur_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            data, owner_uid = identity.read_verified(path)
            msg = message.from_json(data.decode("utf-8"))
        except Exception:
            continue  # unverifiable/corrupt cur/ entry → skip (fail-closed)
        yield path, msg, owner_uid


def find_by_prefix(address: str, prefix: str, root=None):
    """Resolve ``prefix`` to a UNIQUE ``cur/`` message.

    Returns ``("ok", (msg, owner_uid))`` on a unique match, ``("none", None)`` on 0 matches, or
    ``("ambiguous", n)`` on >1 matches (n = match count). Enforces unique-prefix — never guesses.
    """
    matches = [(msg, owner_uid) for _, msg, owner_uid in scan_cur(address, root=root)
               if msg.id.startswith(prefix)]
    if not matches:
        return "none", None
    if len(matches) > 1:
        return "ambiguous", len(matches)
    return "ok", matches[0]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh read",
        description="Re-render a consumed mesh message from cur/ by its id prefix "
                    "(full body only for same-uid AUTO; capped preview for notify-only).",
    )
    p.add_argument("id", nargs="?",
                   help="message-id prefix (the <msgid8> from an 'unread mesh' reminder)")
    return p



def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    address = presence.resolve_own_address()[0]
    if args.id is None:
        sys.stderr.write("mesh read: give a message-id prefix.\n")
        return 2
    status, payload = find_by_prefix(address, args.id)
    if status == "none":
        sys.stderr.write(f"mesh read: no message in cur/ matching '{args.id}'.\n")
        return 1
    if status == "ambiguous":
        sys.stderr.write(
            f"mesh read: ambiguous prefix '{args.id}' matches {payload} messages — "
            "use more characters.\n"
        )
        return 1

    msg, owner_uid = payload
    # Re-resolve on the KERNEL-verified owner_uid (never the self-declared `from`); sender_verified
    # because owner_uid came from identity.read_verified. uid:project is restrict-only.
    policy = trust.load_policy_or_default(trust.policy_path())
    my_uid = os.geteuid()
    level = trust.resolve(policy, owner_uid, _sender_project(msg.from_), my_uid, sender_verified=True)

    if level == trust.AUTO:
        # AUTO: the full body was already shown at delivery — re-rendering it is not a leak.
        sys.stdout.write(frame.render(msg, owner_uid) + "\n")
        return 0
    if level == trust.NOTIFY_ONLY:
        # Notify-only: only a capped preview was ever shown — re-render THAT, never the full body.
        sys.stdout.write(frame.render_notify(msg, owner_uid) + "\n")
        return 0
    # gate/held/BLOCK should never sit in cur/, but fail-closed: never render a withheld body.
    sys.stderr.write("mesh read: message is gated/withheld — not shown (use 'mesh approve').\n")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
