"""Tests for ClaudeController (spec §8.4 / MF-4/5/10). TDD."""
import json
import os
import stat

import pytest

from pm_mesh import poll_counter, settings_merge
from pm_mesh.delivery import UnsupportedDestinationError
from pm_mesh.harness_onboard import ClaudeAdapter
from pm_mesh.poll_controllers import CLAUDE_HOOK_MARKER, CLAUDE_HOOK_SOURCE, ClaudeController
from poll_token_free import assert_settings_no_extra_llm_config, llm_interceptor


@pytest.fixture
def ctl(tmp_path):
    return ClaudeController(
        tmp_path / "mbox",
        {"settings_path": str(tmp_path / "settings.json"), "state_root": tmp_path / "state"},
    )


def _load(ctl):
    with open(ctl.settings_path) as fh:
        return json.load(fh)


def test_factory_returns_claude_controller(tmp_path):
    c = ClaudeAdapter().delivery_controller(
        tmp_path / "mbox", {"settings_path": str(tmp_path / "s.json"), "state_root": tmp_path / "st"}
    )
    assert isinstance(c, ClaudeController)
    assert c.supported_destinations == frozenset({"agent"})


def test_enable_merges_marker_hook_only(ctl):
    st = ctl.enable_delivery("agent")
    assert st.state == "enabled"
    assert st.cost_category == "ADDS_TO_EXISTING_TURN"
    settings = _load(ctl)
    # MF-10: only marker-bearing SessionStart/UserPromptSubmit hooks, no extra-LLM keys
    assert_settings_no_extra_llm_config(settings)
    for event in ("SessionStart", "UserPromptSubmit"):
        # The entry is the VALID nested Claude-Code form — read the command out of hooks[].command,
        # not a (schema-invalid) bare top-level command key.
        cmds = [
            c
            for h in settings["hooks"][event]
            if settings_merge._entry_matches(h, source=CLAUDE_HOOK_SOURCE, marker=CLAUDE_HOOK_MARKER)
            for c in settings_merge._entry_commands(h)
        ]
        assert cmds and all(CLAUDE_HOOK_MARKER in c for c in cmds)
        # Every written entry is schema-valid (the live-breakage regression: a bare entry with no
        # hooks[] array makes Claude Code reject the whole settings.json).
        for h in settings["hooks"][event]:
            if settings_merge._entry_matches(h, source=CLAUDE_HOOK_SOURCE, marker=CLAUDE_HOOK_MARKER):
                assert isinstance(h.get("hooks"), list) and h["hooks"]


def test_enable_channel_raises_unsupported(ctl):
    with pytest.raises(UnsupportedDestinationError):
        ctl.enable_delivery("whatsapp")


def test_token_free_across_lifecycle(ctl):
    # enable -> counter bump (delivery check) -> status -> disable: ZERO LLM calls.
    with llm_interceptor() as counter:
        ctl.enable_delivery("agent")
        ctl.bump_delivery_check()
        ctl.bump_delivery_check()
        ctl.delivery_status()
        ctl.disable_delivery()
    assert counter.count == 0


def test_disable_removes_only_poll_hook(ctl):
    # pre-seed an UNRELATED enroll hook (different source)
    settings_merge.merge_hook(ctl.settings_path, command="/usr/local/bin/mesh-inject", version="1")
    ctl.enable_delivery("agent")
    ctl.disable_delivery()
    settings = _load(ctl)
    # the enroll hook survives; the poll hook is gone
    for event in ("SessionStart", "UserPromptSubmit"):
        sources = [h.get("source") for h in settings["hooks"].get(event, [])]
        assert CLAUDE_HOOK_SOURCE not in sources
        assert settings_merge.HOOK_SOURCE in sources


def test_wiring_probe_and_disable_survive_cc_normalization(ctl):
    """Claude Code rewrites settings.json and STRIPS the top-level source/version keys; the marker
    embedded in the command survives. Drift-detection must still find our hook (not falsely report
    "not wired") and disable must still remove it (not orphan an un-removable entry)."""
    ctl.enable_delivery("agent")
    settings = _load(ctl)
    for arr in settings["hooks"].values():
        for h in arr:
            h.pop("source", None)
            h.pop("version", None)
    with open(ctl.settings_path, "w") as fh:
        json.dump(settings, fh)
    # (a) drift-probe still identifies the poll hook via its command marker (source is gone).
    assert ctl._wiring_probe() is not None
    # (b) disable removes it cleanly — no orphaned marker-bearing entry left behind.
    ctl.disable_delivery()
    after = _load(ctl)
    for event in ("SessionStart", "UserPromptSubmit"):
        for h in after["hooks"].get(event, []):
            assert all(CLAUDE_HOOK_MARKER not in c for c in settings_merge._entry_commands(h))


def test_disable_idempotent(ctl):
    ctl.disable_delivery()  # never enabled → no-op
    ctl.enable_delivery("agent")
    ctl.disable_delivery()
    ctl.disable_delivery()  # again → no-op
    assert ctl.delivery_status().state == "disabled"


def test_counter_file_mf4_path_and_mode(ctl):
    ctl.bump_delivery_check()
    path = poll_counter._counter_path(ctl.mailbox_path, state_root=ctl._state_root)
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.parent.name  # {mailbox_hash}
    assert path.name == "delivery_checks"


def test_counter_atomic_increment_and_restart(ctl):
    ctl.enable_delivery("agent")
    assert ctl.bump_delivery_check() == 1
    assert ctl.bump_delivery_check() == 2
    # a fresh controller (simulated restart) reads the persisted value + reflects it in status
    fresh = ClaudeController(ctl.mailbox_path, {"settings_path": ctl.settings_path, "state_root": ctl._state_root})
    assert poll_counter.read(fresh.mailbox_path, state_root=fresh._state_root) == 2
    assert fresh.delivery_status().delivery_checks == 2


def test_counter_write_failure_fails_open(ctl, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(poll_counter.os, "open", boom)
    # must NOT raise — fail-OPEN
    val = ctl.bump_delivery_check()
    assert val == 1  # best-known value returned, no exception


def test_status_reconciles_drift_missing_wiring(ctl):
    ctl.enable_delivery("agent")
    # simulate wiring vanishing (settings hook removed) while the lock remains
    settings_merge.remove_hook(ctl.settings_path, source=CLAUDE_HOOK_SOURCE)
    st = ctl.delivery_status()
    assert st.state == "drifted_missing_wiring"


def test_status_never_enabled_is_zero_state(ctl):
    st = ctl.delivery_status()
    assert st.state == "disabled"
    assert st.state_note == "never_enabled"
    assert st.delivery_checks == 0
    assert st.cost_category is None
