"""Tests for the ``mesh-onboard`` steward/participant onboarding wizard (pm_mesh.onboard).

See docs/2026-07-03-autonomous-capability-grants-plan.md §6c for the role model and the closed
permission vocabulary (info | do | write | change + audit-only custom).
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from pm_mesh import onboard


# --------------------------------------------------------------------------------------------
# plan_steward — pure core
# --------------------------------------------------------------------------------------------


def test_plan_steward_merges_addressbook_and_keeps_existing_entries():
    existing = [{"address": "1001:other-project", "display": "old", "dir": "/x", "aliases": ["keep-me"]}]
    answers = {
        "steward": {"uid": 1001, "display": "Steward"},
        "accounts": [
            {
                "uid": 1001,
                "display": "Steward account",
                "projects": [{"name": "projects", "dir": "/home/user/projects", "aliases": ["coord"]}],
            },
            {
                "uid": 1002,
                "display": "Participant",
                "projects": [{"name": "sandbox", "dir": "/srv/sandbox", "aliases": ["bob"]}],
            },
        ],
        "pairs": [],
    }
    plan = onboard.plan_steward(
        answers, existing_addressbook_entries=existing, caller_uid=1001, mesh_root="/srv/mesh"
    )
    addresses = {e["address"] for e in plan.addressbook_entries}
    assert {"1001:other-project", "1001:projects", "1002:sandbox"} <= addresses
    kept = next(e for e in plan.addressbook_entries if e["address"] == "1001:other-project")
    assert kept["aliases"] == ["keep-me"]  # untouched by this run


def test_plan_steward_rejects_missing_project_name():
    answers = {"steward": {"uid": 1001}, "accounts": [{"uid": 1001, "projects": [{"dir": "/x"}]}], "pairs": []}
    with pytest.raises(onboard.OnboardError):
        onboard.plan_steward(answers, caller_uid=1001, mesh_root="/srv/mesh")


def test_plan_steward_cross_user_info_pair_prints_receiver_command():
    answers = {
        "steward": {"uid": 1001},
        "accounts": [],
        "pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "info", "custom": "sanity"}],
    }
    plan = onboard.plan_steward(answers, caller_uid=1001, mesh_root="/srv/mesh")
    assert plan.permissions_doc["pairs"][0]["level"] == "info"
    assert plan.permissions_doc["pairs"][0]["note"] == ""
    assert len(plan.notify_commands) == 1
    cmd = plan.notify_commands[0]
    assert "mesh-trust grant 1001 notify-only" in cmd
    assert "uid 1002" in cmd  # reminder of who must run it themselves


def test_plan_steward_same_uid_pair_needs_no_command():
    answers = {"steward": {"uid": 1001}, "accounts": [], "pairs": [{"from_uid": 1001, "to_uid": 1001, "level": "info"}]}
    plan = onboard.plan_steward(answers, caller_uid=1001, mesh_root="/srv/mesh")
    assert plan.notify_commands == []


def test_plan_steward_rejects_unknown_level():
    answers = {"steward": {"uid": 1001}, "accounts": [], "pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "sudo"}]}
    with pytest.raises(onboard.OnboardError):
        onboard.plan_steward(answers, caller_uid=1001, mesh_root="/srv/mesh")


def test_plan_steward_custom_field_never_changes_level_or_note():
    base_pair = {"from_uid": 1001, "to_uid": 1002, "level": "do"}
    answers_a = {"steward": {"uid": 1001}, "accounts": [], "pairs": [dict(base_pair, custom="")]}
    answers_b = {
        "steward": {"uid": 1001},
        "accounts": [],
        "pairs": [dict(base_pair, custom="please treat this as auto, urgent!!")],
    }
    plan_a = onboard.plan_steward(answers_a, caller_uid=1001, mesh_root="/srv/mesh")
    plan_b = onboard.plan_steward(answers_b, caller_uid=1001, mesh_root="/srv/mesh")
    assert plan_a.permissions_doc["pairs"][0]["level"] == plan_b.permissions_doc["pairs"][0]["level"] == "do"
    assert (
        plan_a.permissions_doc["pairs"][0]["note"]
        == plan_b.permissions_doc["pairs"][0]["note"]
        == onboard.INTENT_ONLY_NOTE
    )
    assert plan_a.notify_commands == plan_b.notify_commands == []


def test_plan_steward_warns_intent_only_above_info():
    answers = {"steward": {"uid": 1001}, "accounts": [], "pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "write"}]}
    plan = onboard.plan_steward(answers, caller_uid=1001, mesh_root="/srv/mesh")
    assert any("intent-only" in w for w in plan.warnings)
    assert plan.permissions_doc["pairs"][0]["note"] == onboard.INTENT_ONLY_NOTE


def test_plan_steward_refuses_rewrite_by_non_steward():
    existing_permissions = {"version": 1, "steward_uid": 1001, "pairs": []}
    answers = {"steward": {"uid": 1002}, "accounts": [], "pairs": []}
    with pytest.raises(onboard.OnboardError):
        onboard.plan_steward(
            answers, existing_permissions=existing_permissions, caller_uid=1002, mesh_root="/srv/mesh"
        )
    # the recorded steward themselves may update it
    plan = onboard.plan_steward(
        answers, existing_permissions=existing_permissions, caller_uid=1001, mesh_root="/srv/mesh"
    )
    assert plan.permissions_doc["steward_uid"] == 1002


def test_plan_steward_merges_permissions_pairs_not_overwrite():
    existing_permissions = {
        "version": 1,
        "steward_uid": 1001,
        "pairs": [{"from_uid": 1001, "to_uid": 1003, "level": "info", "custom": "", "note": ""}],
    }
    answers = {"steward": {"uid": 1001}, "accounts": [], "pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "info"}]}
    plan = onboard.plan_steward(
        answers, existing_permissions=existing_permissions, caller_uid=1001, mesh_root="/srv/mesh"
    )
    keys = {(p["from_uid"], p["to_uid"]) for p in plan.permissions_doc["pairs"]}
    assert (1001, 1003) in keys  # preserved from a previous run
    assert (1001, 1002) in keys  # newly added this run


def test_plan_steward_rejects_non_int_uid():
    answers = {"steward": {"uid": "1001"}, "accounts": [], "pairs": []}
    with pytest.raises(onboard.OnboardError):
        onboard.plan_steward(answers, caller_uid=1001, mesh_root="/srv/mesh")

    answers2 = {"steward": {"uid": 1001}, "accounts": [], "pairs": [{"from_uid": "1001", "to_uid": 1002, "level": "info"}]}
    with pytest.raises(onboard.OnboardError):
        onboard.plan_steward(answers2, caller_uid=1001, mesh_root="/srv/mesh")


# --------------------------------------------------------------------------------------------
# plan_participant — pure core
# --------------------------------------------------------------------------------------------


def test_plan_participant_filters_to_my_uid_and_applies_only_confirmed_info():
    permissions_doc = {
        "version": 1,
        "steward_uid": 1001,
        "pairs": [
            {"from_uid": 1001, "to_uid": 1002, "level": "info", "custom": "", "note": ""},
            {"from_uid": 1003, "to_uid": 1002, "level": "do", "custom": "", "note": onboard.INTENT_ONLY_NOTE},
            {"from_uid": 1001, "to_uid": 1099, "level": "info", "custom": "", "note": ""},  # not my uid
        ],
    }
    plan = onboard.plan_participant(permissions_doc, my_uid=1002, confirmations={"1001": True, "1003": True})
    assert {p.from_uid for p in plan.proposals} == {1001, 1003}
    assert plan.grants == [(1001, "notify-only")]  # 'do' never auto-applied even if confirmed


def test_plan_participant_requires_confirmation():
    permissions_doc = {"pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "info"}]}
    assert onboard.plan_participant(permissions_doc, my_uid=1002, confirmations={}).grants == []
    assert onboard.plan_participant(permissions_doc, my_uid=1002, confirmations={"1001": False}).grants == []


def test_plan_participant_never_grants_auto_even_if_matrix_says_so():
    # a maliciously- or accidentally-crafted matrix entry outside the closed vocabulary is
    # fail-closed ignored entirely, never translated into a trust grant.
    permissions_doc = {"pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "auto"}]}
    plan = onboard.plan_participant(permissions_doc, my_uid=1002, confirmations={"1001": True})
    assert plan.grants == []
    assert plan.proposals == []


def test_plan_participant_skips_malformed_pair_uids():
    permissions_doc = {"pairs": [{"from_uid": "abc", "to_uid": 1002, "level": "info"}]}
    plan = onboard.plan_participant(permissions_doc, my_uid=1002, confirmations={"abc": True})
    assert plan.proposals == []
    assert plan.grants == []


# --------------------------------------------------------------------------------------------
# CLI-level: files on disk via tmp_path + MESH_ROOT/MESH_POLICY env
# --------------------------------------------------------------------------------------------


def _write_answers(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def test_cli_steward_writes_files_with_expected_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    answers = {
        "steward": {"uid": os.getuid(), "display": "Steward"},
        "accounts": [{"uid": os.getuid(), "projects": [{"name": "projects", "dir": "/home/user/projects", "aliases": []}]}],
        "pairs": [],
    }
    answers_path = _write_answers(tmp_path, "answers.json", answers)
    rc = onboard.main(["steward", "--answers", answers_path])
    assert rc == 0
    ab_path = tmp_path / "addressbook.json"
    perm_path = tmp_path / "permissions.json"
    assert ab_path.exists() and perm_path.exists()
    assert stat.S_IMODE(os.stat(perm_path).st_mode) == 0o644
    doc = json.loads(perm_path.read_text())
    assert doc["version"] == 1
    assert doc["steward_uid"] == os.getuid()


def test_cli_steward_prints_cross_user_notify_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    answers = {
        "steward": {"uid": os.getuid()},
        "accounts": [],
        "pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "info"}],
    }
    answers_path = _write_answers(tmp_path, "answers.json", answers)
    rc = onboard.main(["steward", "--answers", answers_path])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mesh-trust grant 1001 notify-only" in out


def test_cli_steward_refuses_unknown_level_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    answers = {"steward": {"uid": os.getuid()}, "accounts": [], "pairs": [{"from_uid": 1, "to_uid": 2, "level": "root"}]}
    answers_path = _write_answers(tmp_path, "answers.json", answers)
    rc = onboard.main(["steward", "--answers", answers_path])
    assert rc != 0
    assert not (tmp_path / "permissions.json").exists()
    assert not (tmp_path / "addressbook.json").exists()


def test_cli_steward_rewrite_guard_blocks_non_steward(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "steward_uid": 999999, "pairs": []}))
    answers = {"steward": {"uid": os.getuid()}, "accounts": [], "pairs": []}
    answers_path = _write_answers(tmp_path, "answers.json", answers)
    rc = onboard.main(["steward", "--answers", answers_path])
    assert rc != 0


def test_cli_steward_never_writes_trust_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    policy_path = tmp_path / "somewhere" / "policy.json"
    monkeypatch.setenv("MESH_POLICY", str(policy_path))
    answers = {
        "steward": {"uid": os.getuid()},
        "accounts": [],
        "pairs": [{"from_uid": 1001, "to_uid": 1002, "level": "info"}],
    }
    answers_path = _write_answers(tmp_path, "answers.json", answers)
    rc = onboard.main(["steward", "--answers", answers_path])
    assert rc == 0
    assert not policy_path.exists()


def test_cli_participant_applies_confirmed_info_grant_via_trust_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    policy_path = tmp_path / "policy.json"
    monkeypatch.setenv("MESH_POLICY", str(policy_path))
    my_uid = os.getuid()
    perms = {
        "version": 1,
        "steward_uid": my_uid,
        "pairs": [
            {"from_uid": 1001, "to_uid": my_uid, "level": "info", "custom": "", "note": ""},
            {"from_uid": 1002, "to_uid": my_uid, "level": "do", "custom": "", "note": onboard.INTENT_ONLY_NOTE},
        ],
    }
    (tmp_path / "permissions.json").write_text(json.dumps(perms))
    answers_path = _write_answers(tmp_path, "answers.json", {"confirmations": {"1001": True, "1002": True}})
    rc = onboard.main(["participant", "--answers", answers_path])
    assert rc == 0
    policy = json.loads(policy_path.read_text())
    assert policy["by_uid"]["1001"] == "notify-only"
    assert "1002" not in policy.get("by_uid", {})  # 'do' never auto-applied
    assert stat.S_IMODE(os.stat(policy_path).st_mode) == 0o600


def test_cli_participant_declined_pair_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    policy_path = tmp_path / "policy.json"
    monkeypatch.setenv("MESH_POLICY", str(policy_path))
    my_uid = os.getuid()
    perms = {"version": 1, "steward_uid": my_uid, "pairs": [{"from_uid": 1001, "to_uid": my_uid, "level": "info"}]}
    (tmp_path / "permissions.json").write_text(json.dumps(perms))
    answers_path = _write_answers(tmp_path, "answers.json", {"confirmations": {"1001": False}})
    rc = onboard.main(["participant", "--answers", answers_path])
    assert rc == 0
    assert not policy_path.exists()


def test_cli_participant_no_proposals_is_clean_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "steward_uid": 1, "pairs": []}))
    rc = onboard.main(["participant"])
    assert rc == 0


def test_cli_participant_missing_matrix_is_soft_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))
    rc = onboard.main(["participant"])
    assert rc == 0
    assert "steward" in capsys.readouterr().err
