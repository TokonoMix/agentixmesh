"""Hook entrypoint ``mesh-inject`` (design §18) — show new mesh messages as a DATA frame.

One turn: determine the own address, first run the janitor (orphaned cur/ messages back to new/),
then consume each new message (atomic claim + kernel-verified owner_uid), show fresh messages via
the anti-injection frame, and mark them as seen+shown.

**Fail-closed**: any unexpected error stops further printing and returns **exit 0** — the hook must
never block or fail an agent run. (No actual settings.json wiring; that's t11.)
"""

from __future__ import annotations

import json
import os
import select
import sys

from . import audit, config, frame, maildir, messages, platform, presence, release, replay, trust


def _read_hook_stdin() -> str | None:
    """Non-blocking read of the JSON a harness pipes to a hook on stdin (Claude Code and Codex CLI
    both do this). Returns the raw text, or None if stdin is a tty / empty / unavailable. NEVER
    blocks (guards with ``select``) and NEVER raises — a hook must not hang or fail on stdin."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
        data = sys.stdin.read()
        return data or None
    except Exception:
        return None


def _effective_cwd(stdin_text: str | None = None) -> str | None:
    """Resolve the SESSION working directory for address derivation, for harnesses that run the
    delivery hook from a different directory than the session. Precedence: ``MESH_CWD`` env >
    the ``cwd`` field of the hook's stdin JSON > None (caller falls back to ``os.getcwd()``).

    Fail-closed: only an existing directory string is accepted; anything else (missing, wrong type,
    malformed JSON, non-existent path) yields None so delivery degrades to the process cwd."""
    def _valid(path) -> str | None:
        return path if isinstance(path, str) and path and os.path.isdir(path) else None

    env_cwd = _valid(os.environ.get("MESH_CWD"))
    if env_cwd is not None:
        return env_cwd
    if not stdin_text:
        return None
    try:
        obj = json.loads(stdin_text)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return _valid(obj.get("cwd"))


def _platform_ok(plat=None) -> bool:
    """Structural guard seam (arch-7): returns a bool, NEVER raises across the hook boundary."""
    try:
        if plat is not None:
            return bool(plat.posix_ok)
        return platform.posix_structural_ok()
    except Exception:
        return False


def render_welcome(address) -> str:
    """One-time, action-first welcome for a zero-context reader (§4.2). Content is independent of the
    (self-owned) marker's bytes — the marker is a boolean trigger only.
    Routed through ``frame.render_trusted_block`` for sanitization + per-line framing (§4.2 interface
    ledger: via the trusted frame path)."""
    lines = [
        messages.t("welcome_intro"),
        messages.t("welcome_address", address=address),
        messages.t("welcome_try", address=address),
        messages.t("welcome_rule"),
        messages.t("welcome_hook"),
        messages.t("welcome_leader"),
        messages.t("welcome_skill"),
    ]
    return frame.render_trusted_block("mesh-welcome", lines)


def _maybe_show_welcome(address) -> None:
    """Show the welcome once (marker present -> show + write done-sentinel). Fail-open: any error leaves
    the pending marker so it re-shows next session (§11.6/§11.8); never strands a zero-context user."""
    try:
        pending = config.onboarding_marker_path()
        if not os.path.exists(pending):
            return
        sys.stdout.write(render_welcome(address) + "\n")
        done = config.onboarding_done_path()
        os.makedirs(os.path.dirname(done), exist_ok=True)
        fd = os.open(done, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.unlink(pending)  # clear only AFTER done-sentinel is durable
    except Exception:
        return  # leave pending in place -> welcome re-shows next turn (at-least-once)


def _advisory_turn_cap(store, msg, warned, address) -> None:
    """Advisory anti-ping-loop (design §8): count ``msg.thread``'s turns and log when exceeded.

    **Detection aid, NOT enforcement**: above ``MAX_TURNS_PER_THREAD`` turns, one line appears on
    **stderr** (stdout with the DATA frame stays clean); nothing is suppressed or blocked.
    One line per thread per turn. **Fail-open**: any error in the count/log layer is swallowed — the
    counter must never break delivery. The turn counter keys on the VERIFIED receiving ``address``
    (not on the attacker-controlled ``msg.to``), consistent with the dedup store.
    """
    try:
        count = store.record_turn(address, msg.thread, msg.id)
        if count > config.MAX_TURNS_PER_THREAD and msg.thread not in warned:
            warned.add(msg.thread)
            sys.stderr.write(
                f"⚠ pm-mesh: thread {msg.thread} has had {count} turns "
                f"(cap {config.MAX_TURNS_PER_THREAD}) — possible ping-loop\n"
            )
    except Exception:
        return  # fail-open: the advisory must never break delivery


def _notify_suppressed(counts: dict, owner_uid: int) -> bool:
    """Count a notify-only from ``owner_uid`` this turn; ``True`` if the per-sender cap was already hit.

    **Fail-open** (measure D): any error in the count layer → ``False`` (show) so a counting error
    never suppresses delivery.
    """
    try:
        seen = counts.get(owner_uid, 0)
        counts[owner_uid] = seen + 1
        return seen >= config.NOTIFY_RATE_CAP_PER_TURN
    except Exception:
        return False


def _emit_notify_summary(counts: dict) -> None:
    """Print one summary line per sender that went over the notify-only rate cap (fail-open)."""
    try:
        cap = config.NOTIFY_RATE_CAP_PER_TURN
        for owner_uid, total in sorted(counts.items()):
            if total > cap:
                sys.stdout.write(
                    f"⚠ pm-mesh: {total - cap} notify-only message(s) from uid {owner_uid} "
                    f"suppressed this turn (rate-cap {cap}/turn).\n"
                )
    except Exception:
        return  # fail-open: the summary must never break the hook


def _sender_project(from_) -> str:
    """Extract the (UNTRUSTED) project label from ``from``; ``""`` if unparseable.

    Used only for the ``uid:project`` resolution, which is restrict-only — a lied-about project
    can therefore only restrict further, never elevate. The uid that counts is the kernel-verified
    owner_uid, not the uid from ``from``.
    """
    try:
        _, project = config.parse_address(from_)
        return project
    except (ValueError, TypeError, AttributeError):
        return ""


def main(argv=None, plat=None) -> int:
    """Process the own address's inbox in one turn; print fresh DATA frames to stdout.

    Always returns ``0`` (fail-closed). ``argv`` is for the entrypoint convention and is not
    used (yet). ``plat`` is an injection seam for the platform guard (testability/arch-7).
    """
    try:
        if not _platform_ok(plat):
            sys.stderr.write("pm-mesh: non-POSIX/failed guard — no delivery this turn.\n")
            return 0  # degrade to no-delivery, never raise (arch-7); pending messages preserved (§11.6)
        # Cross-harness: derive the address from the SESSION cwd, which a harness may report via
        # MESH_CWD or its hook stdin JSON when it runs the hook outside the session dir (fail-closed
        # → None → os.getcwd(), so Claude Code and manual runs are unchanged).
        address = config.current_address(_effective_cwd(_read_hook_stdin()))
        _maybe_show_welcome(address)       # welcome BEFORE pending (ux-7); one-time via marker/sentinel
        maildir.maildrop(address)          # make sure new/cur/held exist
        try:
            presence.heartbeat()           # refresh the session heartbeat (f2-06)
        except Exception:
            pass  # fail-open: presence must NEVER break delivery
        try:
            presence.prune_stale()         # janitor: clean up heartbeats of dead/old sessions
        except Exception:
            pass  # fail-open: GC must NEVER break delivery
        maildir.recover_stale(address)     # janitor: orphaned cur/ → new/
        store = replay.SeenStore()
        warned: set = set()  # threads for which the advisory has already been emitted this turn

        # §3.2 / §9.5: drain the receiver-only release-spool BEFORE consuming new/.
        # Dedup-only (is_seen, no age-gate) — approval IS the freshness decision (invariant 7).
        # Fail-closed: any per-entry error is swallowed inside release.drain; the loop never raises.
        try:
            for entry, rel_owner_uid, rel_msg in release.drain(address):
                try:
                    if store.is_seen(address, rel_msg.id):
                        release.discard(entry)  # already shown in a previous turn → clean up
                        continue
                    sys.stdout.write(frame.render(rel_msg, rel_owner_uid) + "\n")
                    store.mark_seen(address, rel_msg.id)
                    release.discard(entry)  # discard ONLY after a successful show+mark
                except Exception:
                    # Do NOT discard on a render/mark error: an approved body not yet shown must
                    # not be lost (consensus 2026-07-01, Opus MEDIUM-2). The claimed entry stays
                    # in place → stale-reclaim (>300s) shows it after all. Continue with the next entry.
                    continue
        except Exception:
            pass  # fail-closed: drain loop must never block the hook

        # Receiver's trust policy (fail-closed: unusable → cross-user human-gate default).
        policy = trust.load_policy_or_default(trust.policy_path())
        # "Me" = the effective uid (like the whole security layer: maildrop owner, policy owner). In
        # normal use equal to getuid; geteuid is the correct identity for the owner comparison and
        # consistent with assert_secure_maildrop / trust.load_policy.
        my_uid = os.geteuid()
        notify_counts: dict = {}  # owner_uid → number of notify-only this turn (rate-cap, f2-11)
        for msg, owner_uid, cur_path in maildir.consume_new(
            address, limit=config.MAX_MESSAGES_PER_TURN
        ):
            try:
                # Resolution on the KERNEL-verified owner_uid (interface contract f2-03), NEVER on the
                # self-declared `from`. The project label does come from `from` (untrusted) — that is
                # safe because uid:project is restrict-only (F1): a lied-about project can only
                # restrict further, never elevate.
                # owner_uid is kernel-verified by maildir.consume_new (fstat), so assert
                # sender_verified=True — the only place allowed to unlock the same-uid -> auto path.
                level = trust.resolve(policy, owner_uid, _sender_project(msg.from_), my_uid,
                                      sender_verified=True)

                if level == trust.AUTO:
                    # Phase-1 path: show the full (sanitized) body. Dedup/turn-counter key on the
                    # VERIFIED receiving `address` (the maildir where the message was actually
                    # found), NEVER on the attacker-controlled `msg.to` — that isn't validated on
                    # the receive path, so it's variable enough to bypass dedup (security review
                    # f2-16, #1).
                    if store.is_fresh(msg, address=address):
                        sys.stdout.write(frame.render(msg, owner_uid) + "\n")
                        store.mark_seen(address, msg.id)
                        _advisory_turn_cap(store, msg, warned, address)
                    # Mark as shown — even an already-seen/old message: handled, not put back into
                    # new/ by the janitor (prevent a replay loop).
                    maildir.mark_shown(cur_path)
                elif level == trust.BLOCK:
                    # Block: do NOT show (not even metadata). Silently to held/ for auditability.
                    maildir.hold(cur_path)
                    audit.append("block", sender_uid=owner_uid, to=address,
                                 thread=msg.thread, id=msg.id, level=level)
                elif level == trust.NOTIFY_ONLY:
                    # notify-only (f2-11): metadata + short capped preview (no held). Per-sender
                    # rate cap: suppress above the cap (summary after the loop). Shown once.
                    if store.is_fresh(msg, address=address):
                        if not _notify_suppressed(notify_counts, owner_uid):
                            sys.stdout.write(frame.render_notify(msg, owner_uid) + "\n")
                        store.mark_seen(address, msg.id)
                    maildir.mark_shown(cur_path)
                else:
                    # human-gate / leader-gate / notify-only → WITHHOLD the body: show only inert
                    # metadata and park the message in held/ until `mesh approve` (f2-05).
                    if store.is_fresh(msg, address=address):
                        sys.stdout.write(frame.render_held(msg, owner_uid, level) + "\n")
                    # DELIBERATELY no mark_seen: dedup happens via the move to held/, and if
                    # `mesh approve` later puts the message back into new/, the full body must
                    # still be able to appear (a mark_seen here would block that — is_fresh would
                    # then be False).
                    maildir.hold(cur_path)
                    audit.append("held", sender_uid=owner_uid, to=address,
                                 thread=msg.thread, id=msg.id, level=level)
            except Exception:
                # Per-message fail-closed: skip this message, continue with the rest.
                continue
        # One summary line per sender that went over the notify-only rate cap (f2-11).
        _emit_notify_summary(notify_counts)
        # Any mail left in new/ after the cap? Report it with one STATIC line (no message
        # content → not injectable) so the agent knows there's a backlog.
        try:
            backlog = len(maildir.list_new(address))
            if backlog:
                sys.stdout.write(
                    f"⚠ pm-mesh: {backlog} message(s) still in the inbox; "
                    f"the next turn shows the rest (cap {config.MAX_MESSAGES_PER_TURN}/turn).\n"
                )
        except Exception:
            pass  # the notice must never break the hook
        return 0
    except Exception:
        # Global fail-closed: never block the agent run.
        return 0


if __name__ == "__main__":  # pragma: no cover
    from .group_reexec import reexec_under_mesh_group_if_needed
    reexec_under_mesh_group_if_needed("pm_mesh.inject")
    raise SystemExit(main(sys.argv[1:]))
