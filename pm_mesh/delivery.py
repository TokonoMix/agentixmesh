"""The ``DeliveryController`` interface + delivery errors (spec §8.2 / §2).

The controller is a DISTINCT interface, NOT methods grafted onto the onboarding
adapter — the *onboard-once* lifecycle (``harness_onboard``) stays separate from
the *toggle-many* lifecycle (``mesh-poll on/off/status``). Each harness adapter is
the FACTORY for its own controller via ``adapter.delivery_controller(mailbox_path,
harness_config)`` (concrete controllers land in MP-05 / MP-07).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .poll_types import DeliveryState, DestinationType


class UnsupportedDestinationError(ValueError):
    """Raised when a controller is asked to deliver to a destination it does not
    support (e.g. the Claude harness asked for a channel — it only has ``agent``)."""


class MailboxAlreadyClaimedError(RuntimeError):
    """Raised when a second delivery path tries to claim a mailbox already held by
    another (harness, destination, mechanism). Names the current holder so the CLI
    can report who won. One address = one delivery path (spec §4.2 / MF-3)."""

    def __init__(self, message: str, holder: dict | None = None):
        super().__init__(message)
        self.holder = holder or {}


@runtime_checkable
class DeliveryController(Protocol):
    """Returned by ``adapter.delivery_controller(...)``. Owns the token-free wiring
    for ONE mailbox on ONE harness (spec §2)."""

    supported_destinations: frozenset[DestinationType]

    def enable_delivery(self, dest: DestinationType) -> DeliveryState:
        """Wire the token-free delivery mechanism for ``dest``. Idempotent."""
        ...

    def disable_delivery(self) -> DeliveryState:
        """Unwire. Idempotent + reconciling (best-effort — succeeds if already off)."""
        ...

    def delivery_status(self) -> DeliveryState:
        """RECONCILED state — compares the lock against the REAL wiring, not a raw
        file read (spec §4.2)."""
        ...
