"""``mesh-poll`` — customer-facing on/off/status/repair for token-free mesh delivery.

Owns UX + the token-cost contract; NO wiring mechanics (those live in the
per-harness ``DeliveryController``, spec §2). Dispatches to
``harness_onboard.HARNESSES[<harness>].delivery_controller(mailbox_path, cfg)``.

    mesh-poll on   [--to agent | --to whatsapp:<id> | --to telegram:<id>] [--harness H] [--force]
    mesh-poll off
    mesh-poll status
    mesh-poll repair

The HARD PROMISE: checking for messages is always free (0 LLM API calls). Only a
message DELIVERED INTO an AI agent costs tokens, disclosed up front (spec §3).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import config, fastmode, maildir, presence
from .delivery import MailboxAlreadyClaimedError, UnsupportedDestinationError
from .harness_onboard import HARNESSES

# Exit codes
EX_OK = 0
EX_USAGE = 2
EX_DRIFT = 3

# --- customer-facing wording (spec §3, verbatim) ----------------------------

_AGENT_WARNING = (
    "Heads up: delivery to an AI agent costs tokens when a message arrives — "
    "checking is free, reading is not, and the AI remembers what it reads."
)

_COST_CONTRACT = (
    "Checking for messages is always free — 0 AI-tokens, every time, no exceptions.\n"
    "You can verify it yourself: `mesh-poll status` shows a counter of delivery-checks\n"
    "with 0 LLM calls from polling.\n"
    "What a message costs depends only on where it goes:\n"
    "  🟢 to your phone (WhatsApp/Telegram): free end-to-end — no AI reads it, we just forward it.\n"
    "  🟡 to your AI agent: the AI has to read it, so it costs tokens — but only when a message\n"
    "     actually arrives (mid-session it's added to your current turn, no new call; idle it wakes\n"
    "     one fresh turn). It then stays in your session history and contributes to later turns.\n"
    "The rule: looking is free. Reading is not — and the AI remembers what it reads.\n"
    "(\"Free\" = zero LLM/API tokens. Scheduler/cron/compute/hosting cost is separate and out of scope.)"
)


def _detect_harness() -> str:
    """Auto-detect the running harness. Claude Code is the interactive default and the
    only harness whose delivery can be toggled after onboarding; the others are wired
    once by ``mesh-onboard-agent`` and have no on/off switch."""
    return "claude"


def _parse_to(to: str) -> tuple[str, str | None]:
    """``agent`` → ``("agent", None)``; ``whatsapp:<id>`` → ``("whatsapp", "<id>")``."""
    if to == "agent":
        return "agent", None
    if ":" in to:
        kind, _, addr = to.partition(":")
        if kind in ("whatsapp", "telegram") and addr:
            return kind, addr
    raise ValueError(f"invalid --to {to!r}; use agent | whatsapp:<id> | telegram:<id>")


def _make_controller(harness: str, mailbox_path, cfg: dict):
    """Seam: build the harness controller (monkeypatched in tests)."""
    adapter = HARNESSES.get(harness)
    if adapter is None:
        raise KeyError(harness)
    return adapter.delivery_controller(mailbox_path, cfg)


def _print_status(st, out) -> None:
    checks = st.delivery_checks
    src = "via cron" if "cron" in (st.mechanism or "") else "via user-activity-hook"
    print(f"state: {st.state}" + (f" → {st.destination}" if st.destination else ""), file=out)
    print(
        f"delivery-checks: {checks} · LLM calls from polling: 0"
        + (f" — saved you {checks} potential calls" if checks else ""),
        file=out,
    )
    print(f"  ({src})", file=out)
    if st.waiting:
        print(f"waiting: {st.waiting} msg (~{st.waiting_token_size} tokens) → {st.destination}", file=out)
    if st.cost_category:
        print(f"cost when a message arrives: {st.cost_category}", file=out)


def _cmd_status(ctl, out) -> int:
    st = ctl.delivery_status()
    _print_status(st, out)
    return EX_DRIFT if str(st.state).startswith("drifted") else EX_OK


def _cmd_off(ctl, out) -> int:
    before = ctl.delivery_status()
    if before.state == "disabled" and before.lock_holder is None:
        print("already disabled (no-op)", file=out)
        return EX_OK
    ctl.disable_delivery()
    print("mesh-poll disabled", file=out)
    return EX_OK


def _cmd_repair(ctl, out) -> int:
    st = ctl.delivery_status()
    if not str(st.state).startswith("drifted"):
        print(f"nothing to repair (state: {st.state})", file=out)
        return EX_OK
    # drifted_missing_wiring → a stale lock: re-enabling recovers it. Other drift →
    # clear by disabling (removes wiring + releases lock).
    if st.state == "drifted_missing_wiring" and st.destination:
        try:
            ctl.enable_delivery(st.destination)
            print(f"repaired: re-wired {st.destination}", file=out)
            return EX_OK
        except (MailboxAlreadyClaimedError, UnsupportedDestinationError) as exc:
            print(f"repair failed: {exc}", file=out)
            return EX_DRIFT
    ctl.disable_delivery()
    after = ctl.delivery_status()
    if str(after.state).startswith("drifted"):
        print(f"repair could not resolve drift ({after.state})", file=out)
        return EX_DRIFT
    print("repaired: cleared drift", file=out)
    return EX_OK


def _cmd_on(ctl, dest_type, force, out) -> int:
    if dest_type not in ctl.supported_destinations:
        print(
            f"unsupported destination {dest_type!r} for this harness "
            f"(supported: {sorted(ctl.supported_destinations)})",
            file=sys.stderr,
        )
        return EX_USAGE
    st = ctl.delivery_status()
    # re-enable, SAME dest → strict no-op (no mutation)
    if st.state == "enabled" and st.destination == dest_type and not force:
        print("already enabled (no-op)", file=out)
        _print_status(st, out)
        return EX_OK
    # destination-switch → ordered: disable old, then enable new
    if st.state == "enabled" and st.destination != dest_type:
        old = st.destination
        ctl.disable_delivery()
        new = ctl.enable_delivery(dest_type)
        print(f"switched {old}→{dest_type}", file=out)
    else:
        try:
            new = ctl.enable_delivery(dest_type)
        except MailboxAlreadyClaimedError as exc:
            holder = exc.holder or {}
            print(
                f"mailbox already claimed by harness={holder.get('harness')!r} "
                f"destination={holder.get('destination')!r} "
                f"wiring_id={holder.get('wiring_id')!r} — not enabled",
                file=sys.stderr,
            )
            return EX_USAGE
    if dest_type == "agent":
        print(_AGENT_WARNING, file=out)  # mandatory, unmissable
    print(_COST_CONTRACT, file=out)
    _print_status(new, out)
    return EX_OK


def _fastmode_cli(argv) -> int:
    """`mesh-poll fastmode get|set|off` — prompt-free per-address fast-mode (snel-modus) state.

    Runs under the allowlisted ``mesh-poll:*`` entry, so a fast-mode tick persists its step WITHOUT a
    permission prompt (a hung prompt used to kill the ScheduleWakeup loop — 2026-07-21 incident). State is
    per-address (``config.current_address()``), NOT the shared ``~/.claude/mesh-fastmode.state``."""
    p = argparse.ArgumentParser(prog="mesh-poll fastmode",
                                description="prompt-free per-address fast-mode (snel-modus) state")
    p.add_argument("action", choices=["get", "status", "set", "off"])
    p.add_argument("--mode", choices=["on", "off"], default=None)
    p.add_argument("--step", type=int, default=None)
    p.add_argument("--note", default="")
    args = p.parse_args(argv)
    address = presence.resolve_own_address()[0]
    now = time.time()
    if args.action in ("get", "status"):
        print(json.dumps(fastmode.load(address, now=now), ensure_ascii=False, indent=2))
        return EX_OK
    if args.action == "off":
        rec = fastmode.save(address, mode="off", step=0, now=now, note=args.note)
    else:  # set
        mode = args.mode or "on"
        step = args.step if args.step is not None else 1
        rec = fastmode.save(address, mode=mode, step=step, now=now, note=args.note)
        if mode == "on":
            # Counterweight to the wakeup scheduler's "nothing more to do this turn" boilerplate,
            # delivered at the exact moment of arming (2026-07-30 incident: an announced incoming
            # question made the agent idle-wait between ticks, abandoning its in-flight work).
            print(
                "advisory: fast-mode armed — this is bookkeeping, NOT your task. Continue any "
                "in-flight or deferred work in this same turn; waiting for a mesh message (even an "
                "announced one) is never a task. Carry unfinished work in the wakeup prompt.",
                file=sys.stderr,
            )
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return EX_OK


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "fastmode":
        return _fastmode_cli(argv[1:])
    p = argparse.ArgumentParser(prog="mesh-poll", description="token-free mesh delivery on/off/status")
    p.add_argument("command", choices=["on", "off", "status", "repair"])
    p.add_argument("--to", default="agent", help="agent | whatsapp:<id> | telegram:<id>")
    p.add_argument("--harness", default=None, help="harness name (default: auto-detect)")
    p.add_argument("--force", action="store_true", help="override an existing claim")
    args = p.parse_args(argv)

    harness = args.harness or _detect_harness()
    if harness not in HARNESSES:
        print(f"unknown harness {harness!r}; known: {sorted(HARNESSES)}", file=sys.stderr)
        return EX_USAGE

    address = presence.resolve_own_address()[0]
    mailbox_path = maildir.maildrop(address)

    try:
        dest_type, channel_addr = _parse_to(args.to)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EX_USAGE

    cfg: dict = {}
    if channel_addr:
        cfg["channel_address"] = channel_addr
    try:
        ctl = _make_controller(harness, mailbox_path, cfg)
    except KeyError:
        print(f"unknown harness {harness!r}", file=sys.stderr)
        return EX_USAGE

    out = sys.stdout
    if args.command == "status":
        return _cmd_status(ctl, out)
    if args.command == "off":
        return _cmd_off(ctl, out)
    if args.command == "repair":
        return _cmd_repair(ctl, out)
    return _cmd_on(ctl, dest_type, args.force, out)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
