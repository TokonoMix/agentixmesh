"""Tests for pm_mesh.poll_types — shared mesh-poll value types (spec §8.1 / MF-9).

TDD: tests written before implementation.
"""
from pm_mesh.poll_types import (
    COST_CATEGORIES,
    DESTINATION_TYPES,
    DeliveryState,
    zero_state,
)

# The exact MF-9 field set, in order.
_MF9_FIELDS = (
    "state",
    "harness",
    "destination",
    "mechanism",
    "cost_category",
    "cost_note",
    "delivery_checks",
    "waiting",
    "waiting_token_size",
    "lock_holder",
    "state_note",
    "action",
)


def test_delivery_state_has_exact_mf9_fields():
    assert DeliveryState._fields == _MF9_FIELDS


def test_delivery_state_constructs_with_required_only():
    st = DeliveryState(state="enabled", harness="claude")
    assert st.state == "enabled"
    assert st.harness == "claude"
    # defaults
    assert st.destination is None
    assert st.mechanism is None
    assert st.cost_category is None
    assert st.delivery_checks == 0
    assert st.waiting == 0
    assert st.waiting_token_size == 0
    assert st.lock_holder is None
    assert st.state_note is None
    assert st.action is None


def test_cost_categories_are_the_three_labels():
    assert COST_CATEGORIES == {"FREE", "ADDS_TO_EXISTING_TURN", "WAKES_NEW_TURN"}


def test_destination_types_are_agent_and_two_channels():
    assert DESTINATION_TYPES == {"agent", "whatsapp", "telegram"}


def test_zero_state_matches_never_enabled_shape():
    st = zero_state("claude")
    assert st.state == "disabled"
    assert st.harness == "claude"
    assert st.destination is None
    assert st.mechanism is None
    assert st.cost_category is None
    assert st.delivery_checks == 0
    assert st.waiting == 0
    assert st.state_note == "never_enabled"


def test_zero_state_carries_harness_name():
    assert zero_state("codex").harness == "codex"
