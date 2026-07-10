"""``mesh approve <id>`` (phase 2, f2-05) — gate-release (Design B): release a parked (``held/``)
message via the receiver-only release spool.

**Design B (spec §3.1):** after authorization the kernel-verified bytes are staged into
``release/<token_hex(16)>`` (receiver-only 0700); ``held/<f>`` is then removed. We NEVER write to
``new/`` — the spool is the only handoff. The next inject turn drains the spool and shows the full
body (§3.2). Safety invariants:
- BLOCK is refused before any write (§9.11 / consensus-4).
- Verified bytes come 1:1 from ``identity.read_verified`` — the held file is never reopened
  (§9.1 / sec-A: TOCTOU-free).
- ``held`` is removed only after a successful stage (crash-safe: re-approve restarts cleanly).

Unknown/already-released id → a clean ``ApproveError`` (CLI exit != 0), no stacktrace. Every approve
writes a (best-effort) audit entry (``audit.append``; f2-15 centralizes that).
"""

from __future__ import annotations

import argparse
import os
import sys

from . import audit, config, groups, identity, maildir, message, release, trust


class ApproveError(Exception):
    """Approve could not be carried out (unknown id, not authorized, or I/O error)."""


def _sender_project(from_) -> str:
    """The (UNTRUSTED) project label from ``from``; ``""`` if unparseable (restrict-only, so safe)."""
    try:
        _, project = config.parse_address(from_)
        return project
    except (ValueError, TypeError, AttributeError):
        return ""


def _shared_group_names(cfg, uid_a: int, uid_b: int) -> list:
    """Groups in which both ``uid_a`` and ``uid_b`` are members."""
    a = set(groups.groups_of(cfg, uid_a))
    b = set(groups.groups_of(cfg, uid_b))
    return sorted(a & b)


def _manager_can_approve(
    level: str, approver_uid: int, receiver_uid: int, address: str, owner_uid=None, root=None
) -> bool:
    """May ``approver_uid``, as a group manager or head leader, approve this message? (f2-09)

    The **head leader** may always co-approve. A **manager** may approve if he is manager of a
    group that the **sender** (``owner_uid``, kernel-verified) and the **receiver** share. Without
    ``owner_uid`` (or without a shared group) → only the head leader can grant.
    """
    cfg = groups.load_groups(root)
    if groups.is_head_leader(cfg, approver_uid):
        return True
    if owner_uid is None:
        return False
    shared = _shared_group_names(cfg, owner_uid, receiver_uid)
    managers = {groups.manager_of(cfg, g) for g in shared}
    return approver_uid in managers


def _held_level(held_path: str, receiver_uid: int):
    """Determine the trust level of a held message via the KERNEL-verified owner_uid.

    Returns ``(level, owner_uid, verified_bytes)``. The ``verified_bytes`` are exactly the bytes
    returned by ``identity.read_verified`` — they are passed 1:1 to ``release.stage`` without
    reopening the file (§9.1: TOCTOU-free / content-swap-safe).

    Raises ``ApproveError`` if the message cannot be read safely (then the approve authority
    cannot be determined → fail-closed).
    """
    try:
        data, owner_uid = identity.read_verified(held_path)
        msg = message.from_json(data.decode("utf-8"))
    except (identity.IdentityError, ValueError, UnicodeDecodeError, OSError) as exc:
        raise ApproveError(f"could not safely read the held message: {exc}") from exc
    policy = trust.load_policy_or_default(trust.policy_path())
    # owner_uid is kernel-verified (identity.read_verified above), so assert sender_verified=True (DiD).
    level = trust.resolve(policy, owner_uid, _sender_project(msg.from_), receiver_uid,
                          sender_verified=True)
    return level, owner_uid, data  # data = verified_bytes (§9.1)


def _find_held(held_dir: str, msg_id: str):
    """Find the ``held/`` file with ``msg.id == msg_id``; ``None`` if it's no longer there.

    Reads+parses every held file (the receiver owns/reads ``held/``); corrupt/unreadable → skipped.
    """
    try:
        names = sorted(os.listdir(held_dir))
    except OSError:
        return None
    for name in names:
        if name.startswith(".") or name.endswith(maildir.SHOWN_SUFFIX):
            continue
        path = os.path.join(held_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                msg = message.from_json(fh.read())
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if msg.id == msg_id:
            return path
    return None


def approve(msg_id: str, root=None, approver_uid=None, address=None) -> str:
    """Release the ``held/`` message with ``id == msg_id`` via the release spool (Design B).

    Steps (§3.1):
    1. Reject BLOCK before any write (§9.11).
    2. Authz unchanged (receiver / manager / head leader).
    3. Stage the kernel-verified bytes in ``release/`` via ``release.stage``.
    4. Remove ``held/<f>`` after a successful stage.
    5. NEVER writes to ``new/``.

    Returns the release-spool path (or stable indicator). Raises ``ApproveError`` on
    not-authorized, unknown/already-released id, or an unexpected I/O error.
    """
    if approver_uid is None:
        approver_uid = os.geteuid()
    if address is None:
        address = config.current_address()
    receiver_uid, _ = config.parse_address(address)

    base = root if root is not None else config.mesh_root()
    drop = os.path.join(base, address)
    held_dir = os.path.join(drop, "held")

    held_path = _find_held(held_dir, msg_id)
    if held_path is None:
        raise ApproveError(f"unknown or already-released message id: {msg_id!r}")

    # Determine the level + kernel-verified bytes in one atomic read (§9.1, §9.11).
    # Single-shot: we do NOT re-resolve the policy between this check and the stage.
    level, owner_uid, verified_bytes = _held_level(held_path, receiver_uid)

    # §9.11 / consensus-4: BLOCK cannot be released by anyone — refuse before any write.
    if level == trust.BLOCK:
        raise ApproveError("a blocked message cannot be released")

    if level == trust.LEADER_GATE:
        # leader-gate: ONLY the group manager (of a shared group) or the head leader — a
        # regular member, even the receiver themself, cannot release this alone (f2-09).
        cfg = groups.load_groups(base)
        if not _shared_group_names(cfg, owner_uid, receiver_uid) and not groups.is_head_leader(
            cfg, approver_uid
        ):
            raise ApproveError(
                "no shared group for this leader-gate message — cannot be released "
                "(stays in held/); a head leader CAN still co-approve it"
            )
        if not _manager_can_approve(
            "leader-gate", approver_uid, receiver_uid, address, owner_uid=owner_uid, root=base
        ):
            raise ApproveError(
                "leader-gate requires the group manager or the head leader — the receiver alone is not enough"
            )
    else:
        # human-gate / notify-only / other: the receiver themself may approve, or a
        # manager-of-a-shared-group, or the head leader (who may co-approve anything).
        if approver_uid != receiver_uid and not _manager_can_approve(
            level, approver_uid, receiver_uid, address, owner_uid=owner_uid, root=base
        ):
            raise ApproveError(
                f"not authorized: only the receiver (uid {receiver_uid}), a group manager "
                f"or the head leader may approve this message"
            )

    # §3.1 step 3: stage the kernel-verified bytes in release/ (Design B, receiver-only spool).
    # verified_bytes were already read by _held_level — the held file is NOT reopened.
    try:
        entry_path = release.stage(address, owner_uid, verified_bytes, root=base)
    except OSError as exc:
        raise ApproveError(f"could not write release-spool entry: {exc}") from exc

    # §3.1 step 4: remove held after a successful stage (crash window: if the stage already
    # succeeded here but remove fails, the message sits in both dirs; the drain dedupes on msg.id).
    try:
        os.remove(held_path)
    except OSError as exc:
        raise ApproveError(f"could not remove held file after stage: {exc}") from exc

    audit.append(
        "approve",
        root=base,
        msg_id=msg_id,
        address=address,
        approver_uid=approver_uid,
        level=level,
    )
    return entry_path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-approve",
        description="Release a parked (held) message; the next inject shows the body.",
    )
    p.add_argument("id", help="message id (the 'id:' from the ⏸-held frame)")
    return p


def main(argv=None) -> int:
    """Console entry; exit 0 = released, !=0 = clean error (no stacktrace)."""
    args = _build_parser().parse_args(argv)
    try:
        entry_path = approve(args.id)
    except ApproveError as exc:
        print(f"mesh-approve: {exc}", file=sys.stderr)
        return 1
    print(f"released: {os.path.basename(entry_path)} (the next inject shows the body)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
