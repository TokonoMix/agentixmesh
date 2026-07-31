"""Concrete ``DeliveryController`` implementations for mesh-poll.

``ClaudeController`` lives here: Claude Code delivery is the inject hook, so turning
delivery on/off is a merge into ``settings.json`` guarded by a claim lock. The harness
adapters in ``harness_onboard`` are the factories that construct a controller
(``adapter.delivery_controller(mailbox_path, harness_config)``); a harness without one
fails loudly rather than silently doing nothing.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import poll_counter, settings_merge
from .delivery import DeliveryState, UnsupportedDestinationError
from .poll_lock import (
    DRIFT_MISSING_WIRING,
    ENABLED,
    PollLock,
    mailbox_hash,
)
from .poll_types import zero_state

# The poll hook is DISTINCT from the enroll/onboard hook: a separate source + a
# marker (carried as a trailing shell comment so it is inert at runtime but present
# in the settings string for MF-10). Disabling polling removes ONLY this source.
CLAUDE_HOOK_SOURCE = "mesh-poll-claude"
CLAUDE_HOOK_MARKER = "#mesh-poll-claude-v1"
CLAUDE_HOOK_COMMAND = f"/usr/local/bin/mesh-inject {CLAUDE_HOOK_MARKER}"
CLAUDE_MECHANISM = "claude-inject-hook"


def _pkg_version() -> str:
    try:
        from . import harness_onboard
        return harness_onboard._package_version()
    except Exception:
        return "0"


class ClaudeController:
    """Claude Code delivery = the SessionStart/UserPromptSubmit inject hook, merged
    via the SAME ``settings_merge`` path ``mesh-enroll`` uses. Only ``agent`` dest;
    ``cost_category = ADDS_TO_EXISTING_TURN``. The message lands in session history
    regardless of any external channel, so this path legitimately consumes-on-render
    (the deliberate asymmetry vs the channel relay — spec §4.3)."""

    supported_destinations = frozenset({"agent"})

    def __init__(self, mailbox_path, harness_config: dict | None = None):
        self.mailbox_path = Path(mailbox_path)
        cfg = harness_config or {}
        self.settings_path = cfg.get("settings_path") or os.path.join(
            os.path.expanduser("~"), ".claude", "settings.json"
        )
        self._state_root = cfg.get("state_root")  # tests inject a tmp root
        self.lock = PollLock(self.mailbox_path, state_root=self._state_root)

    # -- wiring probe (the REAL settings.json state) ------------------------

    def _wiring_probe(self):
        obj, ok = settings_merge._load(self.settings_path)
        if not ok or not isinstance(obj, dict):
            return None
        hooks = obj.get("hooks", {})
        for event in ("SessionStart", "UserPromptSubmit"):
            for h in hooks.get(event, []) if isinstance(hooks, dict) else []:
                # Match by source OR the inert command marker — the marker survives Claude Code's
                # settings.json normalization (which strips the top-level source key), so drift
                # detection keeps finding our own hook instead of silently reporting "not wired".
                if settings_merge._entry_matches(h, source=CLAUDE_HOOK_SOURCE, marker=CLAUDE_HOOK_MARKER):
                    return {
                        "harness": "claude",
                        "destination": "agent",
                        "mechanism": CLAUDE_MECHANISM,
                        "wiring_id": self.lock.wiring_id("claude", "agent"),
                    }
        return None

    # -- lifecycle ----------------------------------------------------------

    def enable_delivery(self, dest) -> DeliveryState:
        if dest != "agent":
            raise UnsupportedDestinationError(
                f"claude harness supports only 'agent', not {dest!r}"
            )
        state = self.lock.reconcile(self._wiring_probe)
        if state == DRIFT_MISSING_WIRING:
            # A stale lock (claimer died before/after wiring was torn down) → recover.
            self.lock.recover_stale(
                "claude", "agent", CLAUDE_MECHANISM, wiring_probe=self._wiring_probe
            )
        else:
            # Strict claim (the race arbiter) — raises MailboxAlreadyClaimedError to
            # a loser; a genuine same-dest re-enable is a no-op handled by the CLI.
            self.lock.claim("claude", "agent", CLAUDE_MECHANISM)
        # Wire IMMEDIATELY after the claim (minimise the claim→wire window — the
        # residual Race-B window MP-03's liveness check guards).
        rc = settings_merge.merge_hook(
            self.settings_path,
            command=CLAUDE_HOOK_COMMAND,
            version=_pkg_version(),
            source=CLAUDE_HOOK_SOURCE,
        )
        if rc != settings_merge.EX_OK:
            raise RuntimeError(
                f"settings_merge.merge_hook failed (rc={rc}) on {self.settings_path}"
            )
        return self.delivery_status(action="enabled")

    def disable_delivery(self) -> DeliveryState:
        # Best-effort + idempotent: remove ONLY the poll hook (by its source), then
        # release the lock; succeed even if already gone.
        settings_merge.remove_hook(
            self.settings_path, source=CLAUDE_HOOK_SOURCE, marker=CLAUDE_HOOK_MARKER
        )
        try:
            self.lock.release()
        except PermissionError:
            pass  # foreign lock — leave it; reconcile/status will surface drift
        return zero_state("claude")._replace(action="disabled")

    def delivery_status(self, action: str | None = None) -> DeliveryState:
        state = self.lock.reconcile(self._wiring_probe)
        if state == "disabled" and self.lock.read() is None:
            return zero_state("claude")._replace(action=action)
        checks = poll_counter.read(self.mailbox_path, state_root=self._state_root)
        cost = "ADDS_TO_EXISTING_TURN" if state == ENABLED else None
        note = None if state == ENABLED else state
        return DeliveryState(
            state=state,
            harness="claude",
            destination="agent",
            mechanism=CLAUDE_MECHANISM,
            cost_category=cost,
            cost_note="delivery checks via user-activity-hook",
            delivery_checks=checks,
            waiting=0,
            waiting_token_size=0,
            lock_holder=self.lock.read(),
            state_note=note,
            action=action,
        )

    def bump_delivery_check(self) -> int:
        """Called by the inject hook when it reads the mailbox — increments the
        MF-4 counter WITHOUT any LLM call. Fails OPEN."""
        return poll_counter.bump(self.mailbox_path, state_root=self._state_root)
