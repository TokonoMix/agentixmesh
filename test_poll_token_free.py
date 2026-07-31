"""Tests for the token-free-invariant harness (spec §8.7 / MF-10).

TDD; load-bearing. Proves the interceptor actually catches an LLM call and that the
MF-10 settings assertion is NON-tautological (passes clean, fails on extra-LLM keys).
"""
import pytest

from poll_token_free import (
    MESH_POLL_MARKER,
    LLMCallDetected,
    assert_settings_no_extra_llm_config,
    llm_interceptor,
)


# --------------------------------------------------------------------------- interceptor


def test_interceptor_yields_zero_when_nothing_calls_llm():
    with llm_interceptor() as counter:
        # a token-free lifecycle: pure file/dict work, no inference
        _ = {"state": "enabled"}
        for _ in range(3):
            _ = len(str(_))
    assert counter.count == 0


def test_interceptor_catches_a_real_anthropic_call():
    # The interceptor MUST fire if code actually reaches the LLM endpoint. anthropic
    # is installed in this environment, so patch-and-call proves the spy works.
    anthropic = pytest.importorskip("anthropic")
    from anthropic.resources.messages import Messages

    with llm_interceptor() as counter:
        with pytest.raises(LLMCallDetected):
            # call the (patched) unbound method without constructing a client
            Messages.create(object(), model="x", max_tokens=1, messages=[])
        assert counter.count == 1
    # restored after the block
    assert Messages.create.__name__ != "spy"


# --------------------------------------------------------------------------- MF-10 assertion


def _clean_settings():
    return {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": f"/usr/local/bin/mesh-inject {MESH_POLL_MARKER}"}]}
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command",
                            "command": f"/usr/local/bin/mesh-inject {MESH_POLL_MARKER}"}]}
            ],
        }
    }


def test_mf10_passes_for_clean_hook_only_settings():
    assert_settings_no_extra_llm_config(_clean_settings())  # must not raise


def test_mf10_fails_on_model_key():
    bad = _clean_settings()
    bad["model"] = "claude-opus-4-8"
    with pytest.raises(AssertionError, match="model"):
        assert_settings_no_extra_llm_config(bad)


def test_mf10_fails_on_nested_tools_key():
    bad = _clean_settings()
    bad["hooks"]["SessionStart"][0]["tools"] = [{"name": "x"}]
    with pytest.raises(AssertionError, match="tools"):
        assert_settings_no_extra_llm_config(bad)


def test_mf10_fails_on_hook_without_marker():
    bad = _clean_settings()
    bad["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "/usr/local/bin/mesh-inject"
    with pytest.raises(AssertionError, match="marker"):
        assert_settings_no_extra_llm_config(bad)


def test_mf10_fails_on_hook_on_disallowed_event():
    bad = _clean_settings()
    bad["hooks"]["Stop"] = [
        {"hooks": [{"type": "command", "command": f"x {MESH_POLL_MARKER}"}]}
    ]
    with pytest.raises(AssertionError, match="Stop"):
        assert_settings_no_extra_llm_config(bad)


def test_mf10_fails_when_no_hooks_present():
    with pytest.raises(AssertionError, match="expected at least one"):
        assert_settings_no_extra_llm_config({"permissions": {}})


def test_mf10_tolerates_flat_hook_shape():
    flat = {"hooks": {"SessionStart": [{"command": f"mesh-inject {MESH_POLL_MARKER}"}]}}
    assert_settings_no_extra_llm_config(flat)  # must not raise
