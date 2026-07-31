"""A1-02 — OpenClawAdapter for mesh-onboard-agent.

OpenClaw has NO stdout hook: delivery is a push bridge (mesh-inject-openclaw.sh) on a schedule.
The adapter syncs that bridge from the repo source so binary_sha256
matches, and must NOT mutate the universal trust payload. Tests redirect the bridge dest under
tmp_path via MESH_OPENCLAW_BIN_DIR — never writing to /usr/local/bin, never registering a live cron.
"""
import hashlib
import json
import os

from pm_mesh import harness_onboard

_UID = os.getuid()


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def test_openclaw_registered():
    assert "openclaw" in harness_onboard.HARNESSES
    assert harness_onboard.HARNESSES["openclaw"].name == "openclaw"


def test_openclaw_probe_writes_manifest_and_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_OPENCLAW_BIN_DIR", str(tmp_path / "bin"))
    rc = harness_onboard.main(["openclaw", "probe"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("PASTE-TO-AGENT: ") == 1
    adapter = harness_onboard.HARNESSES["openclaw"]
    assert os.path.exists(adapter.manifest_dest("probe"))
    assert os.path.exists(adapter.skill_dest("probe"))
    assert os.path.join(".openclaw", "skills", "agentixmesh") in adapter.manifest_dest("probe")
    m = json.loads(_read(adapter.manifest_dest("probe")))
    assert m["address"] == f"{_UID}:probe"
    # binary_path is the SYNCED bridge, under the redirected bin dir, hash matching the repo source
    assert str(tmp_path / "bin") in m["binary_path"]
    assert os.path.exists(m["binary_path"])
    canonical = os.path.join(harness_onboard.default_root(), "hooks", "openclaw",
                             "mesh-inject-openclaw.sh")
    assert m["binary_sha256"] == _sha256(canonical)


def test_openclaw_trust_payload_byte_identical_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_OPENCLAW_BIN_DIR", str(tmp_path / "bin"))
    assert harness_onboard.main(["claude", "probe"]) == 0
    assert harness_onboard.main(["openclaw", "probe"]) == 0
    c = harness_onboard.HARNESSES["claude"]
    x = harness_onboard.HARNESSES["openclaw"]
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


def test_openclaw_wire_idempotent_and_syncs_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_OPENCLAW_BIN_DIR", str(tmp_path / "bin"))
    adapter = harness_onboard.HARNESSES["openclaw"]
    root = harness_onboard.default_root()
    first = adapter.wire("openclaw", root)
    second = adapter.wire("openclaw", root)
    assert first == second
    assert first["address"] == f"{_UID}:openclaw"
    assert os.path.exists(first["binary_path"])
    canonical = os.path.join(root, "hooks", "openclaw", "mesh-inject-openclaw.sh")
    assert _sha256(first["binary_path"]) == _sha256(canonical)


def test_openclaw_wire_resyncs_when_dest_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_OPENCLAW_BIN_DIR", str(tmp_path / "bin"))
    adapter = harness_onboard.HARNESSES["openclaw"]
    root = harness_onboard.default_root()
    dest = adapter.wire("openclaw", root)["binary_path"]
    with open(dest, "w") as fh:            # corrupt the installed bridge
        fh.write("# stale\n")
    adapter.wire("openclaw", root)          # must re-sync
    canonical = os.path.join(root, "hooks", "openclaw", "mesh-inject-openclaw.sh")
    assert _sha256(dest) == _sha256(canonical)
