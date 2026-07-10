"""Tests for pm_mesh.dcp — wire-format wrap/extract + validate.

TDD: tests written before implementation.
"""
import json
import os
import pytest
from pm_mesh.dcp import wrap, extract, validate

_DCP_REPO = os.environ.get("DCP_REPO") or os.path.expanduser("~/development-coordination-protocol")
_EXAMPLE = os.path.join(_DCP_REPO, "examples/v1/task.completed.json")


# ---------------------------------------------------------------------------
# wrap
# ---------------------------------------------------------------------------

def test_wrap_produces_open_tag():
    result = wrap('{"a":1}')
    assert result.startswith('<dcp v="1.0">\n')


def test_wrap_ends_with_close_tag():
    result = wrap('{"a":1}')
    assert result.endswith('\n</dcp>')


def test_wrap_contains_json():
    json_str = '{"hello": "world"}'
    result = wrap(json_str)
    assert json_str in result


def test_wrap_exact_format():
    json_str = '{"x":1}'
    expected = '<dcp v="1.0">\n{"x":1}\n</dcp>'
    assert wrap(json_str) == expected


def test_wrap_empty_string():
    # even empty json-like content round-trips
    result = wrap('')
    assert result == '<dcp v="1.0">\n\n</dcp>'


# ---------------------------------------------------------------------------
# extract — happy path
# ---------------------------------------------------------------------------

def test_extract_round_trip():
    json_str = '{"key": "value", "num": 42}'
    assert extract(wrap(json_str)) == json_str


def test_extract_round_trip_unicode():
    json_str = '{"msg": "héllo wörld 🎉"}'
    assert extract(wrap(json_str)) == json_str


def test_extract_accepts_any_version():
    body = '<dcp v="2.5">\n{"x":1}\n</dcp>'
    assert extract(body) == '{"x":1}'


def test_extract_tolerates_leading_whitespace():
    body = '   \n<dcp v="1.0">\n{"a":1}\n</dcp>'
    assert extract(body) == '{"a":1}'


def test_extract_tolerates_trailing_whitespace():
    body = '<dcp v="1.0">\n{"a":1}\n</dcp>   \n'
    assert extract(body) == '{"a":1}'


def test_extract_tolerates_both_sides_whitespace():
    body = '  \n  <dcp v="1.0">\n{"z":9}\n</dcp>  \n  '
    assert extract(body) == '{"z":9}'


def test_extract_multiline_json():
    json_str = '{\n  "a": 1,\n  "b": 2\n}'
    assert extract(wrap(json_str)) == json_str


def test_extract_byte_faithful():
    # the inner string must be returned byte-identical (no strip, no modification)
    json_str = '  {"padded": true}  '
    result = wrap(json_str)
    assert extract(result) == json_str


# ---------------------------------------------------------------------------
# extract — None cases
# ---------------------------------------------------------------------------

def test_extract_plain_text_returns_none():
    assert extract("hello world") is None


def test_extract_empty_string_returns_none():
    assert extract("") is None


def test_extract_missing_close_tag_returns_none():
    assert extract('<dcp v="1.0">\n{"a":1}') is None


def test_extract_missing_open_tag_returns_none():
    assert extract('{"a":1}\n</dcp>') is None


def test_extract_only_open_tag_returns_none():
    assert extract('<dcp v="1.0">') is None


def test_extract_only_close_tag_returns_none():
    assert extract('</dcp>') is None


def test_extract_substring_mention_no_frame_returns_none():
    # body merely mentions "<dcp" but is not a real frame
    assert extract('see docs at <dcp v="1.0"> for details') is None


def test_extract_doubled_open_tag_returns_none():
    # two top-level <dcp ...> open tags → reject
    body = '<dcp v="1.0">\n{"a":1}\n</dcp>\n<dcp v="1.0">\n{"b":2}\n</dcp>'
    assert extract(body) is None


def test_extract_doubled_open_tag_different_versions_returns_none():
    body = '<dcp v="1.0">\n{"a":1}\n</dcp>\n<dcp v="2.0">\n{"b":2}\n</dcp>'
    assert extract(body) is None


def test_extract_nested_dcp_returns_none():
    # nested <dcp> inside the block
    body = '<dcp v="1.0">\n<dcp v="1.0">\n{"inner":1}\n</dcp>\n</dcp>'
    assert extract(body) is None


def test_extract_open_tag_inside_body_still_rejects():
    # if the inner content contains a real (unescaped) open-tag pattern, reject
    # Note: a JSON-encoded <dcp v=\"...\"> uses backslash-escaped quotes so it
    # does NOT match the <dcp v="..."> pattern — that is safe and round-trips.
    # A real second open tag embedded in a crafted (non-JSON) body IS rejected.
    crafted_body = '<dcp v="1.0">\nsome text\n<dcp v="1.0">\nmore\n</dcp>'
    assert extract(crafted_body) is None

def test_extract_json_containing_dcp_substring_roundtrips():
    # A JSON string whose value contains the <dcp substring with escaped quotes
    # is byte-faithful and should round-trip (the inner text is not a real tag).
    json_str = '{"tag": "<dcp v=\\"1.0\\">"}'
    assert extract(wrap(json_str)) == json_str


def test_extract_well_formed_tag_required():
    # tag must be <dcp v="..."> — missing v attribute → None
    assert extract('<dcp>\n{"a":1}\n</dcp>') is None


def test_extract_v_attribute_must_be_quoted():
    assert extract('<dcp v=1.0>\n{"a":1}\n</dcp>') is None


def test_extract_one_open_two_close_rejected():
    # Close-tag symmetry (internal review + consensus 2026-07-01, req 11a04496):
    # one open tag but a trailing extra </dcp> must NOT be greedily folded into inner.
    body = '<dcp v="1.0">\n{"a":1}\n</dcp>\nGARBAGE\n</dcp>'
    assert extract(body) is None


def test_extract_trailing_close_after_valid_frame_rejected():
    body = '<dcp v="1.0">\n{"a":1}\n</dcp>\n</dcp>'
    assert extract(body) is None


# ---------------------------------------------------------------------------
# validate — DCP reference validator bridge
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.path.isfile(_EXAMPLE),
    reason="DCP repo example not available",
)
def test_validate_valid_example():
    """A well-formed DCP example passes validation."""
    with open(_EXAMPLE, encoding="utf-8") as f:
        dcp_json = f.read()
    valid, errors = validate(dcp_json)
    assert valid is True
    assert errors == []


@pytest.mark.skipif(
    not os.path.isdir(_DCP_REPO),
    reason="DCP repo not available",
)
def test_validate_broken_message_type():
    """A message where message_type != entity_type.verb fails validation."""
    with open(_EXAMPLE, encoding="utf-8") as f:
        msg = json.load(f)
    # Corrupt message_type so it no longer equals entity_type.verb
    msg["message_type"] = "wrong.type"
    valid, errors = validate(json.dumps(msg))
    assert valid is False
    assert len(errors) > 0


@pytest.mark.skipif(
    not os.path.isdir(_DCP_REPO),
    reason="DCP repo not available",
)
def test_validate_missing_dcp_version():
    """A message missing dcp_version fails validation."""
    with open(_EXAMPLE, encoding="utf-8") as f:
        msg = json.load(f)
    msg.pop("dcp_version", None)
    valid, errors = validate(json.dumps(msg))
    assert valid is False
    assert any("dcp_version" in e for e in errors)


def test_validate_bad_repo_path():
    """When the repo path doesn't exist, validate returns fail-closed."""
    valid, errors = validate('{"any":"json"}', dcp_repo="/nonexistent/repo")
    assert valid is False
    assert len(errors) == 1
    assert "validator not found" in errors[0].lower() or "not found" in errors[0].lower()


def test_validate_missing_node(monkeypatch, tmp_path):
    """When node is not on PATH, validate returns fail-closed."""
    # Patch shutil.which to return None for 'node'
    import pm_mesh.dcp as dcp_mod
    monkeypatch.setattr(dcp_mod.shutil, "which", lambda name: None)
    valid, errors = validate('{"any":"json"}')
    assert valid is False
    assert "node" in errors[0].lower()


# --- Green-gate hardening (internal review + consensus req a607bef2) ---

def test_validate_non_str_fails_closed():
    # non-str input → tf.write raises TypeError → must fail-closed, never raise.
    ok, errs = validate(None)
    assert ok is False
    assert errs


def test_validate_header_strip_is_exact_not_broad(tmp_path):
    # A real error line that merely ENDS with the temp basename must be preserved;
    # only the exact "FAIL <basename>" header line is dropped.
    # Hermetic: a stub validator repo + mocked node/subprocess so this unit test never
    # depends on a real DCP reference checkout being present.
    import subprocess as _sp
    from unittest import mock

    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "validate.mjs").write_text("// stub\n", encoding="utf-8")

    def fake_run(argv, **kw):
        tmp = argv[2]
        base = os.path.basename(tmp)
        out = f"FAIL {base}\n  field 'p' must not end with {base}\n"
        return _sp.CompletedProcess(argv, 1, stdout="", stderr=out)

    with mock.patch("pm_mesh.dcp.shutil.which", return_value="/usr/bin/node"), \
         mock.patch("pm_mesh.dcp.subprocess.run", side_effect=fake_run):
        ok, errs = validate('{"x":1}', dcp_repo=str(tmp_path))
    assert ok is False
    assert any("must not end with" in e for e in errs), errs
    assert all(not (e.startswith("FAIL ") and "/" not in e and e.count(" ") == 1) for e in errs)
