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
import time

from . import (acks, audit, config, frame, identity, maildir, message, messages, platform,
               presence, release, reminder, replay, trust)


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


def _relay_box(src_address: str, dst_address: str, sub: str) -> int:
    """Move every entry of ``src``'s ``sub`` box (``new``/``held``) into ``dst``'s.

    Same-tree ``os.rename`` (atomic; both boxes live under one mesh root), ``.shown``
    companions travel along as plain names, dot-prefixed temp files stay. Best-effort: a
    per-file failure skips that file, never raises."""
    src = os.path.join(maildir.maildrop(src_address), sub)
    dst = os.path.join(maildir.maildrop(dst_address), sub)
    moved = 0
    try:
        names = os.listdir(src)
    except OSError:
        return 0
    for name in names:
        if name.startswith("."):
            continue
        try:
            os.rename(os.path.join(src, name), os.path.join(dst, name))
            moved += 1
        except OSError:
            continue
    return moved


def _renotify_pending_held(address, policy, my_uid, ledger, now) -> None:
    """re-emit the INERT held metadata for still-pending (unapproved) held messages.

    A ``held`` message is shown once when it lands; a receiver who missed that single notice would
    never be reminded. This re-shows the reminder so it stays visible until ``mesh approve`` (or the
    message is dismissed). **Body-withholding invariant is absolute**: exactly like the first notice,
    ONLY ``frame.render_held`` metadata is emitted — never the body, never a preview — so re-notify
    can never become a side-channel around the cross-user gate.

    Strictly non-consuming over ``held/``: each file is read with a KERNEL-verified ``owner_uid``
    (``identity.read_verified``, same as ``consume_new`` / ``approve``), never claimed/renamed/marked;
    only this module's own reminder markers are written. Guards:
    * **BLOCK is skipped** — a blocked message was parked in ``held/`` WITHOUT ever being shown, not
      even metadata; re-notify must preserve that (never surface a blocked sender's metadata/subject).
    * **count cap + time throttle** via ``ledger`` — bounded reminders, never a per-prompt nag.
    * **per-sender per-turn rate cap** (``NOTIFY_RATE_CAP_PER_TURN``) — a flooded ``held/`` can't
      dominate one turn; the rest simply reappears next turn.

    Fail-open per entry AND overall: any error skips that message / returns — never breaks delivery.
    """
    try:
        drop = maildir.maildrop(address)
        held_dir = os.path.join(drop, "held")
        try:
            names = sorted(os.listdir(held_dir))
        except OSError:
            return
        renotify_counts: dict = {}  # owner_uid → re-notifications emitted this turn (per-sender cap)
        for name in names:
            if name.startswith(".") or name.endswith(maildir.SHOWN_SUFFIX):
                continue
            path = os.path.join(held_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                data, owner_uid = identity.read_verified(path)
                msg = message.from_json(data.decode("utf-8"))
            except Exception:
                continue  # corrupt/unverifiable held file → skip (fail-closed), never crash
            try:
                # Recompute the level on the KERNEL-verified owner_uid (never the self-declared from);
                # uid:project is restrict-only, so a spoofed project can only restrict, never elevate.
                level = trust.resolve(policy, owner_uid, _sender_project(msg.from_), my_uid,
                                      sender_verified=True)
                if level == trust.BLOCK:
                    continue  # block = never shown, not even metadata (flagged in review)
                # re-notify only RE-shows what was already shown once —
                # it never INTRODUCES a first showing. A held message the receiver never saw (a BLOCK
                # parked silently, or any never-seeded id) has ledger count 0 → skip it. This keeps
                # BLOCK-silence DURABLE even if the trust policy later degrades to {} (unreadable/deleted
                # → human-gate fallback): the guard does not depend on re-resolving BLOCK every turn.
                if ledger.count(address, msg.id) == 0:
                    continue
                if not ledger.should_remind(address, msg.id, now=now):
                    continue  # count cap reached or still inside the throttle window
                emitted = renotify_counts.get(owner_uid, 0)
                if emitted >= config.NOTIFY_RATE_CAP_PER_TURN:
                    continue  # per-sender per-turn cap; the rest reappears next turn
                renotify_counts[owner_uid] = emitted + 1
                sys.stdout.write(frame.render_held(msg, owner_uid, level) + "\n")
                ledger.record(address, msg.id, now=now)
            except Exception:
                continue  # per-message fail-closed
    except Exception:
        return  # fail-open: re-notify must never break the hook


def _held_backlog_summary(address, ledger, *, root=None, now=None):
    """One aggregate line for the still-pending ``held/`` backlog, or ``None`` if there is none.

    The per-message re-notify above is deliberately BOUNDED (``HELD_REMINDER_MAX``): once those
    notices are spent a held message is never mentioned again and sits unapproved forever — the
    receiver stops being told, and the sender was never told at all. That silent-loss failure mode
    is not hypothetical: in our own deployment it stranded dozens of real messages across several
    mailboxes for weeks before anyone noticed. This line is the un-exhaustible backstop:
    while anything is held, every turn says how much and how old.

    It is safe to make un-exhaustible because it carries **strictly less** than the per-message
    notice it outlives: a count and an age — no body, no subject, no thread, not even a sender. It
    therefore cannot become a side-channel around the withholding gate, and there is nothing for a
    sender to inject into (every character is generated here).

    Counts only messages the receiver has **already been told about** (``ledger.count > 0``), the
    same guard ``_renotify_pending_held`` uses: a BLOCK-parked message was never announced, not even
    as metadata, and a counter must not be what reveals that it exists.

    Fail-open: any error → ``None`` (no line), never an exception into the hook.
    """
    try:
        held_dir = os.path.join(maildir.maildrop(address, root=root), "held")
        try:
            names = sorted(os.listdir(held_dir))
        except OSError:
            return None
        if now is None:
            now = time.time()
        count = 0
        oldest = None
        for name in names:
            if name.startswith(".") or name.endswith(maildir.SHOWN_SUFFIX):
                continue
            path = os.path.join(held_dir, name)
            try:
                if not os.path.isfile(path):
                    continue
                data, _owner_uid = identity.read_verified(path)
                msg = message.from_json(data.decode("utf-8"))
                if ledger.count(address, msg.id) == 0:
                    continue  # never announced (BLOCK) → stays invisible, count included
                mtime = os.stat(path).st_mtime
            except Exception:
                continue  # per-entry fail-closed: an unreadable file is simply not counted
            count += 1
            if oldest is None or mtime < oldest:
                oldest = mtime
        if not count:
            return None
        age = _relative_age(now - oldest) if oldest is not None else "unknown age"
        return (f"⚠ pm-mesh: {count} held message(s) awaiting your approval (oldest {age}). "
                f"Their text stays withheld until you release it — list: mesh status · "
                f"release: mesh-approve <id>")
    except Exception:
        return None


def _relative_age(seconds) -> str:
    """Compact human relative age for the AUTO re-notify line ("just now" / "5m ago" / "2h ago")."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "recently"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _renotify_recent_auto(address, policy, my_uid, ledger, now) -> None:
    """re-surface a COMPACT metadata reminder for a recently-consumed same-uid AUTO message.

    A same-uid AUTO message is shown once at delivery, then lives in ``cur/`` with a ``.shown``
    companion. If the consuming inject turn was not the human's active session, the human silently
    misses it. This re-emits a one-line metadata reminder (``frame.render_unread_auto`` — NEVER the
    body) a bounded number of times, then it auto-expires with the recency window. Mirrors
    ``_renotify_pending_held`` but pointed at ``cur/``; runs on the SAME interactive path.

    Eligibility + guards:

    * **``.shown`` companion EXISTS** — proof the message was already shown once. This is the correct
      "already shown" signal, NOT a ledger ``count>0`` check: the AUTO consume branch never seeds the
      ``ReminderLedger``, so the ledger here counts ONLY re-notifications (blocker #2 — copying held's
      ``if count==0`` guard would make re-notify a permanent silent no-op).
    * **Recency by ``.shown`` mtime** within ``AUTO_RENOTIFY_WINDOW_S`` (time since the human *could*
      have seen it) — NOT ``ts_utc``. Processed **oldest-``.shown``-first** (deterministic).
    * **Same-uid AUTO (+ cross-user notify-only) only.** Only AUTO (same uid, short-circuited) and
      NOTIFY_ONLY (cross-user) ever land in ``cur/`` — a gate/held/BLOCK message was moved to ``held/``
      and is the held-renotify's job. Re-resolving ``level`` on the kernel-verified owner_uid still earns its
      keep: it drops a cross-user sender whose policy TIGHTENED to a gate after the message was shown.
      A notify-only reminder is metadata only (never its withheld body — that was never shown).
    * **Skip if acked** (``acks.is_acked``) — the human confirmed seeing it.
    * **Bounded cadence** via ``ReminderLedger.should_remind`` with the ``AUTO_RENOTIFY_*`` cap AND the
      DISTINCT ~2h interval (NOT the 30-min held default — blocker #3). Per-sender per-turn cap
      (``NOTIFY_RATE_CAP_PER_TURN``) + a global per-turn cap (``AUTO_RENOTIFY_GLOBAL_CAP_PER_TURN``).

    Fail-open per entry AND overall (mirror ``_renotify_pending_held``): any error skips that message /
    returns — re-notify must NEVER break the delivery hook.
    """
    try:
        drop = maildir.maildrop(address)
        cur_dir = os.path.join(drop, "cur")
        try:
            names = os.listdir(cur_dir)
        except OSError:
            return
        # Collect eligible entries (has .shown companion, .shown mtime in window); sort oldest-first.
        eligible = []  # (shown_mtime, path)
        for name in names:
            if name.startswith(".") or name.endswith(maildir.SHOWN_SUFFIX):
                continue
            path = os.path.join(cur_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                shown_mtime = os.stat(path + maildir.SHOWN_SUFFIX).st_mtime
            except OSError:
                continue  # no .shown companion → never shown once → not eligible
            if not config.within_window(shown_mtime, now, config.AUTO_RENOTIFY_WINDOW_S):
                continue  # older than the recency window → auto-expired
            eligible.append((shown_mtime, path))
        eligible.sort(key=lambda e: e[0])  # oldest-.shown-first (deterministic order)

        renotify_counts: dict = {}  # owner_uid → reminders emitted this turn (per-sender cap)
        global_emitted = 0
        for shown_mtime, path in eligible:
            if global_emitted >= config.AUTO_RENOTIFY_GLOBAL_CAP_PER_TURN:
                break  # global per-turn cap; the rest reappears next turn
            try:
                data, owner_uid = identity.read_verified(path)
                msg = message.from_json(data.decode("utf-8"))
            except Exception:
                continue  # corrupt/unverifiable cur/ file → skip (fail-closed), never crash
            try:
                # Recompute the level on the KERNEL-verified owner_uid (never the self-declared from);
                # uid:project is restrict-only, so a spoofed project can only restrict, never elevate.
                level = trust.resolve(policy, owner_uid, _sender_project(msg.from_), my_uid,
                                      sender_verified=True)
                # This filter is NOT redundant, do NOT remove it as "cur/ is only AUTO/notify-only"
                # delivery writes to cur/ THEN moves a
                # gate/held/BLOCK message to held/. A crash between those two steps strands a gated
                # message in cur/; re-resolving on the kernel-verified owner_uid and skipping anything
                # but AUTO/notify-only keeps a withheld body (and a BLOCK sender's metadata) from ever
                # leaking under crash-recovery. It also drops a sender whose policy tightened post-show.
                if level not in (trust.AUTO, trust.NOTIFY_ONLY):
                    continue  # gate/held/BLOCK is the held-renotify's job (and never normally reaches cur/)
                if acks.is_acked(address, msg.id):
                    continue  # human explicitly acked → stop reminding
                # auto-renotify AUTO and held re-notify share ONE
                # ReminderLedger (wired together in main). Namespace the AUTO cadence key so the two
                # semantic uses of the ledger are provably disjoint — a held msg.id and an AUTO msg.id
                # can never cross-suppress each other's count-cap/throttle even under a (uuid4-improbable)
                # id reuse. AUTO-only prefix: held's raw-key seed/record (main + _renotify_pending_held)
                # stays byte-identical, so the held-renotify's behavior is untouched. acks/ keys on the raw id.
                led_key = "auto:" + msg.id
                if not ledger.should_remind(address, led_key, now=now,
                                            max_reminders=config.AUTO_RENOTIFY_MAX,
                                            min_interval_s=config.AUTO_RENOTIFY_MIN_INTERVAL_S):
                    continue  # count cap reached or still inside the ~2h throttle window
                emitted = renotify_counts.get(owner_uid, 0)
                if emitted >= config.NOTIFY_RATE_CAP_PER_TURN:
                    continue  # per-sender per-turn cap; the rest reappears next turn
                rel_age = _relative_age(now - shown_mtime)
                sys.stdout.write(frame.render_unread_auto(msg, owner_uid, rel_age) + "\n")
                ledger.record(address, led_key, now=now)
                renotify_counts[owner_uid] = emitted + 1
                global_emitted += 1
            except Exception:
                continue  # per-message fail-closed
    except Exception:
        return  # fail-open: re-notify must never break the hook

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
        effective_cwd = _effective_cwd(_read_hook_stdin())
        address = config.current_address(effective_cwd)
        _maybe_show_welcome(address)       # welcome BEFORE pending (ux-7); one-time via marker/sentinel
        maildir.maildrop(address)          # make sure new/cur/held exist
        try:
            presence.heartbeat(cwd=effective_cwd)  # tag under the SESSION address, not the hook cwd
        except Exception:
            pass  # fail-open: presence must NEVER break delivery
        # Path-qualified addressing: under a live same-basename collision the heartbeat just
        # registered a QUALIFIED label — this session then owns two boxes. Relay base new/ +
        # held/ into the qualified box (atomic same-tree renames; the relay IS the base-box
        # read, keeping first-reader semantics between siblings) and run the rest of the turn
        # on the qualified address, so precise replies and base traffic surface in one
        # pipeline. Fail-open: any error keeps the plain base address (previous behaviour).
        try:
            _hb = presence.session_heartbeat_record()
            if (
                isinstance(_hb, dict)
                and isinstance(_hb.get("project"), str)
                and isinstance(_hb.get("project_base"), str)
                and _hb["project"] != _hb["project_base"]
                and f"{os.geteuid()}:{_hb['project_base']}" == address
            ):
                qualified = f"{_hb['user']}:{_hb['project']}"
                maildir.maildrop(qualified)        # ensure new/cur/held exist
                maildir.recover_stale(address)     # orphaned base cur/ → new/ BEFORE the relay
                for _sub in ("new", "held"):
                    _relay_box(address, qualified, _sub)
                address = qualified
        except Exception:
            pass
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
        # Re-notify still-pending held messages BEFORE consuming new/ — a new arrival gets its FIRST
        # notice in the loop below, old held/ gets its reminder here, so nothing is shown twice in one
        # turn. Then re-surface recently-consumed same-uid AUTO messages (compact metadata only) from
        # cur/, reusing the SAME ledger and clock. Both are fail-open: they never break delivery.
        ledger = reminder.ReminderLedger()
        now = time.time()
        _renotify_pending_held(address, policy, my_uid, ledger, now)
        _renotify_recent_auto(address, policy, my_uid, ledger, now)
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
                        # Seed the reminder ledger — this first notice counts as reminder #1, so the
                        # re-notify pass throttles instead of nagging on the very next turn.
                        ledger.record(address, msg.id, now=now)
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
