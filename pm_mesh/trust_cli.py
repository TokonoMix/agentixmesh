"""``mesh-trust`` — set/inspect the RECEIVER's own trust policy for a sender uid.

The receiving user decides how much of a cross-user sender's traffic flows
automatically. The levels (loosest → strictest):

  auto         full body, agent may ACT — SAME-USER ONLY (the engine hard-floors
               cross-user 'auto' to human-gate; you cannot grant it cross-user)
  notify-only  a short inert PREVIEW flows with NO hold and NO approve — the
               receiving agent can READ it and reply with words, but never acts
               on it. This is the "informational" tier for unattended peers.
  human-gate   body withheld; only metadata shown; held until `mesh approve`
  leader-gate  stricter gate
  block        nothing shown; silently held for audit

This CLI only WRITES the receiver's own policy file (mode 0600, outside the
shared mesh root, so a sender can never reach it). It cannot create an unsafe
state: the trust engine independently floors cross-user 'auto' and ignores any
policy file with group/other bits. Elevation keys on uid; a uid:project entry
may only make a uid level MORE restrictive (F1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import trust


def _load_raw(path: str) -> dict:
    try:
        return trust.load_policy(path)
    except Exception as exc:
        print(f"mesh-trust: existing policy unreadable/unsafe: {exc}", file=sys.stderr)
        raise SystemExit(1)


def _atomic_write_0600(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mesh-trust", description="Set/inspect your own trust policy per sender uid.")
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("grant", help="give a sender uid a level")
    g.add_argument("uid", type=int)
    g.add_argument("level", choices=list(trust.LEVELS))
    g.add_argument("--project", help="restrict-only override for uid:project (may only be stricter)")

    r = sub.add_parser("revoke", help="remove the level for a sender uid (back to default human-gate)")
    r.add_argument("uid", type=int)

    sub.add_parser("show", help="show the current policy")

    args = p.parse_args(argv)
    path = trust.policy_path()

    if args.cmd == "show" or args.cmd is None:
        pol = _load_raw(path)
        print(f"# policy: {path}")
        print(json.dumps(pol or {"by_uid": {}, "by_uid_project": {}}, indent=2, sort_keys=True))
        return 0

    pol = _load_raw(path)
    pol.setdefault("by_uid", {})
    pol.setdefault("by_uid_project", {})

    if args.cmd == "grant":
        my = os.geteuid()
        if args.uid != my and args.level == trust.AUTO:
            print("mesh-trust: NOTE — 'auto' for another uid is floored by the engine "
                  "to human-gate (cross-user may never act autonomously). "
                  "Use 'notify-only' for informational auto-reading.", file=sys.stderr)
        if args.project:
            pol["by_uid_project"][f"{args.uid}:{args.project}"] = args.level
        else:
            pol["by_uid"][str(args.uid)] = args.level
        _atomic_write_0600(path, pol)
        # Diagnostic: model a real (fstat-verified) sender of this uid so the shown "effective" level
        # matches actual delivery (a real sender is always kernel-verified). Only affects the same-uid
        # case; for a cross-user uid sender_verified is irrelevant (the shortcut needs ==).
        eff = trust.resolve(pol, args.uid, args.project or "", os.geteuid(), sender_verified=True)
        tgt = f"{args.uid}:{args.project}" if args.project else str(args.uid)
        print(f"set: {tgt} -> {args.level}  (effective after engine clamp: {eff})")
        return 0

    if args.cmd == "revoke":
        pol["by_uid"].pop(str(args.uid), None)
        pol["by_uid_project"] = {k: v for k, v in pol["by_uid_project"].items()
                                 if not k.startswith(f"{args.uid}:")}
        _atomic_write_0600(path, pol)
        print(f"removed: uid {args.uid} -> back to default (human-gate cross-user)")
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
