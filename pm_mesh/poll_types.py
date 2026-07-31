"""Shared value types for the mesh-poll routine (spec §8.1 / §10 MF-9).

Pure, stdlib-only, no side effects. Every ``DeliveryController`` returns a
``DeliveryState``; the three ``cost_category`` labels and the ``DestinationType``
set are the plain-English token-cost vocabulary the CLI prints.

The token-cost meaning of each ``cost_category`` (spec §3):
- ``FREE``                 — cron no-agent relay → channel; poll AND delivery both
                             cost nothing, no LLM ever reads it.
- ``ADDS_TO_EXISTING_TURN`` — Claude inject-hook; rides a turn already running (no
                             new call; cost = the message's own tokens once at
                             delivery) and then stays in session history, re-read
                             on later turns.
- ``WAKES_NEW_TURN``       — cron wakeAgent-gate; a waiting message wakes one fresh
                             AI turn (one normal call).
"""
from __future__ import annotations

from typing import Literal, NamedTuple

# ---------------------------------------------------------------------------
# Destination + cost vocabularies
# ---------------------------------------------------------------------------

DestinationType = Literal["agent", "whatsapp", "telegram"]
"""Where a delivered message goes. ``agent`` costs tokens; the channels are FREE."""

DESTINATION_TYPES: frozenset[str] = frozenset({"agent", "whatsapp", "telegram"})

CostCategory = Literal["FREE", "ADDS_TO_EXISTING_TURN", "WAKES_NEW_TURN"]
"""The three first-class, plain-English cost labels (spec §3)."""

COST_CATEGORIES: frozenset[str] = frozenset(
    {"FREE", "ADDS_TO_EXISTING_TURN", "WAKES_NEW_TURN"}
)

StateName = Literal[
    "disabled",
    "enabled",
    "drifted_missing_wiring",
    "drifted_missing_lock",
    "drifted_mismatch",
]


# ---------------------------------------------------------------------------
# DeliveryState — the uniform shape returned by every controller (MF-9)
# ---------------------------------------------------------------------------

class DeliveryState(NamedTuple):
    """Uniform status shape across harnesses (spec §10 MF-9, verbatim field set)."""

    state: StateName
    harness: str
    destination: str | None = None
    mechanism: str | None = None
    cost_category: CostCategory | None = None
    cost_note: str | None = None
    delivery_checks: int = 0
    waiting: int = 0
    waiting_token_size: int = 0
    lock_holder: dict | None = None
    state_note: str | None = None
    action: str | None = None


def zero_state(harness: str) -> DeliveryState:
    """The never-enabled zero-state, uniform across adapters (spec §4.1 / MF-1).

    ``{state:"disabled", harness, mechanism:null, destination:null,
    cost_category:null, delivery_checks:0, waiting:0, state_note:"never_enabled"}``.
    """
    return DeliveryState(
        state="disabled",
        harness=harness,
        destination=None,
        mechanism=None,
        cost_category=None,
        delivery_checks=0,
        waiting=0,
        state_note="never_enabled",
    )
