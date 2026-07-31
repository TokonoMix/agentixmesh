"""``mesh-poll`` — the ``fastmode`` state-persistence subcommand.

    mesh-poll fastmode get|set|off [--mode on|off] [--step N] [--note "..."]

This module currently ships only the ``fastmode`` subcommand: prompt-free, per-address
persistence for the fast-mode (snel-modus) quick-reply cadence documented in
``skill/references/fast-mode.md``. It intentionally does not implement a general
on/off/status message-delivery toggle — that is a larger, harness-specific feature not
(yet) part of this release.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config, fastmode, presence

# Exit codes
EX_OK = 0


def _fastmode_cli(argv) -> int:
    """``mesh-poll fastmode get|set|off`` — prompt-free per-address fast-mode (snel-modus) state.

    Runs under the allowlisted ``mesh-poll:*`` entry, so a fast-mode tick persists its step
    WITHOUT a permission prompt (a hung prompt can otherwise kill a self-rescheduling wakeup
    loop). State is per-address (``config.current_address()``), NOT a single shared file."""
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
            # Counterweight to the wakeup scheduler's "nothing more to do this turn"
            # boilerplate, delivered at the exact moment of arming (real incident: an
            # announced incoming question made the agent idle-wait between ticks,
            # abandoning its in-flight work).
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
    p = argparse.ArgumentParser(prog="mesh-poll", description="mesh-poll fastmode state CLI")
    p.add_argument("command", choices=["fastmode"])
    p.parse_args(argv)  # empty/unknown command -> argparse usage error (SystemExit(2))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
