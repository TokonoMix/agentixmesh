"""Tests for mesh-trust CLI (writes the receiver's own 0600 policy)."""

from __future__ import annotations

import json
import os
import stat

from pm_mesh import trust_cli, trust


def _policy(tmp_path, monkeypatch):
    p = tmp_path / "policy.json"
    monkeypatch.setenv("MESH_POLICY", str(p))
    return p


def test_grant_notify_only_writes_0600_and_resolves(tmp_path, monkeypatch, capsys):
    p = _policy(tmp_path, monkeypatch)
    rc = trust_cli.main(["grant", "1001", "notify-only"])
    assert rc == 0
    data = json.loads(p.read_text())
    assert data["by_uid"]["1001"] == "notify-only"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # a foreign uid at notify-only survives the engine clamp (only auto is floored)
    assert trust.resolve(data, 1001, "pm-mesh", 1002) == "notify-only"


def test_grant_auto_to_foreign_uid_is_floored_by_engine(tmp_path, monkeypatch, capsys):
    p = _policy(tmp_path, monkeypatch)
    trust_cli.main(["grant", "1001", "auto"])
    data = json.loads(p.read_text())
    # the CLI stores what was asked, but resolve() floors cross-user auto → human-gate
    assert trust.resolve(data, 1001, "pm-mesh", 1002) == "human-gate"
    err = capsys.readouterr().err
    assert "floored" in err or "notify-only" in err


def test_uid_project_override_is_restrict_only(tmp_path, monkeypatch):
    p = _policy(tmp_path, monkeypatch)
    trust_cli.main(["grant", "1001", "notify-only"])
    trust_cli.main(["grant", "1001", "human-gate", "--project", "pm-mesh"])  # stricter → honored
    data = json.loads(p.read_text())
    assert trust.resolve(data, 1001, "pm-mesh", 1002) == "human-gate"
    # a looser project override would be ignored by the engine
    trust_cli.main(["grant", "1001", "human-gate"])
    trust_cli.main(["grant", "1001", "notify-only", "--project", "other"])  # looser → ignored
    data = json.loads(p.read_text())
    assert trust.resolve(data, 1001, "other", 1002) == "human-gate"


def test_revoke_returns_to_default(tmp_path, monkeypatch):
    p = _policy(tmp_path, monkeypatch)
    trust_cli.main(["grant", "1001", "notify-only"])
    trust_cli.main(["revoke", "1001"])
    data = json.loads(p.read_text())
    assert "1001" not in data["by_uid"]
    assert trust.resolve(data, 1001, "pm-mesh", 1002) == "human-gate"  # back to cross-user default
