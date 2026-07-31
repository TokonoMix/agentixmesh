"""A1-01 — CodexAdapter for mesh-onboard-agent.

Codex uses the SAME stdout-as-context hook contract as Claude Code, so the adapter mirrors
ClaudeAdapter and must NOT mutate the universal trust payload (manifest core + skill body +
paste-line). These tests use the default (no --apply) path — never a live Codex config.
"""
import json
import os

from pm_mesh import harness_onboard

_UID = os.getuid()


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_codex_registered():
    assert "codex" in harness_onboard.HARNESSES
    assert harness_onboard.HARNESSES["codex"].name == "codex"


def test_codex_probe_writes_manifest_and_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = harness_onboard.main(["codex", "probe"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("PASTE-TO-AGENT: ") == 1
    adapter = harness_onboard.HARNESSES["codex"]
    assert os.path.exists(adapter.manifest_dest("probe"))
    assert os.path.exists(adapter.skill_dest("probe"))
    # emitted under the Codex config home, not ~/.claude
    assert os.path.join(".codex", "skills", "agentixmesh") in adapter.manifest_dest("probe")
    m = json.loads(_read(adapter.manifest_dest("probe")))
    assert m["address"] == f"{_UID}:probe"
    assert os.path.exists(m["binary_path"])
    assert m["binary_sha256"]


def test_codex_trust_payload_byte_identical_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert harness_onboard.main(["claude", "probe"]) == 0
    assert harness_onboard.main(["codex", "probe"]) == 0
    c = harness_onboard.HARNESSES["claude"]
    x = harness_onboard.HARNESSES["codex"]
    cm = json.loads(_read(c.manifest_dest("probe")))
    xm = json.loads(_read(x.manifest_dest("probe")))

    def _core(man):
        return {k: v for k, v in man.items()
                if k not in ("binary_path", "binary_sha256", "generated_at")}
    assert _core(cm) == _core(xm)
    assert cm["address"] == xm["address"] == f"{_UID}:probe"
    assert cm["source_commit"] == xm["source_commit"]

    def _norm(text, man_path):
        return text.replace(man_path, "<MANIFEST>")
    cs = _norm(_read(c.skill_dest("probe")), c.manifest_dest("probe"))
    xs = _norm(_read(x.skill_dest("probe")), x.manifest_dest("probe"))
    assert cs == xs, "agent-skill BODY must be byte-identical across harnesses"


def test_codex_wire_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    adapter = harness_onboard.HARNESSES["codex"]
    root = harness_onboard.default_root()
    first = adapter.wire("codex", root)
    second = adapter.wire("codex", root)
    assert first == second
    assert first["address"] == f"{_UID}:codex"
    assert os.path.exists(first["binary_path"])
