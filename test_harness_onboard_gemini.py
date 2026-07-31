"""A1-03 — GeminiAdapter + the mesh-inject-gemini envelope wrapper.

Grounded 2026-07-14 (geminicli.com/docs/hooks): Gemini has a command-hook (SessionStart + BeforeAgent)
but injects JSON `hookSpecificOutput.additionalContext`, not raw stdout. So delivery re-wraps the
mesh-inject frame in that envelope. The adapter mirrors OpenClaw (sync a wrapper from the repo source)
and must NOT mutate the universal trust payload. The wrapper's transform is unit-tested via a stubbed
inner command (MESH_INJECT_CMD), so no real mailbox is needed.
"""
import hashlib
import json
import os
import subprocess
import sys

from pm_mesh import harness_onboard

_UID = os.getuid()
_ROOT = harness_onboard.default_root()
_WRAPPER = os.path.join(_ROOT, "hooks", "gemini", "mesh-inject-gemini.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --- adapter -----------------------------------------------------------------------------------

def test_gemini_registered():
    assert "gemini" in harness_onboard.HARNESSES
    assert harness_onboard.HARNESSES["gemini"].name == "gemini"


def test_gemini_probe_writes_manifest_and_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_GEMINI_BIN_DIR", str(tmp_path / "bin"))
    rc = harness_onboard.main(["gemini", "probe"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("PASTE-TO-AGENT: ") == 1
    adapter = harness_onboard.HARNESSES["gemini"]
    assert os.path.join(".gemini", "skills", "agentixmesh") in adapter.manifest_dest("probe")
    m = json.loads(_read(adapter.manifest_dest("probe")))
    assert m["address"] == f"{_UID}:probe"
    assert str(tmp_path / "bin") in m["binary_path"]
    assert m["binary_sha256"] == _sha256(_WRAPPER)


def test_gemini_trust_payload_byte_identical_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_GEMINI_BIN_DIR", str(tmp_path / "bin"))
    assert harness_onboard.main(["claude", "probe"]) == 0
    assert harness_onboard.main(["gemini", "probe"]) == 0
    c = harness_onboard.HARNESSES["claude"]
    x = harness_onboard.HARNESSES["gemini"]
    cm = json.loads(_read(c.manifest_dest("probe")))
    xm = json.loads(_read(x.manifest_dest("probe")))

    def _core(man):
        return {k: v for k, v in man.items()
                if k not in ("binary_path", "binary_sha256", "generated_at")}
    assert _core(cm) == _core(xm)

    def _norm(text, man_path):
        return text.replace(man_path, "<MANIFEST>")
    cs = _norm(_read(c.skill_dest("probe")), c.manifest_dest("probe"))
    xs = _norm(_read(x.skill_dest("probe")), x.manifest_dest("probe"))
    assert cs == xs, "agent-skill BODY must be byte-identical across harnesses"


def test_gemini_wire_idempotent_and_syncs_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MESH_GEMINI_BIN_DIR", str(tmp_path / "bin"))
    adapter = harness_onboard.HARNESSES["gemini"]
    first = adapter.wire("gemini", _ROOT)
    second = adapter.wire("gemini", _ROOT)
    assert first == second
    assert first["address"] == f"{_UID}:gemini"
    assert _sha256(first["binary_path"]) == _sha256(_WRAPPER)


# --- the envelope wrapper (stubbed inner command, no real mailbox) ------------------------------

def _run_wrapper(event, inner_cmd, env_extra=None):
    env = dict(os.environ)
    env["MESH_INJECT_CMD"] = inner_cmd
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, _WRAPPER, event], capture_output=True, text=True, env=env)


def test_wrapper_wraps_frame_in_gemini_envelope():
    p = _run_wrapper("SessionStart", "printf 'FRAME-LINE-1\\nFRAME-LINE-2'")
    assert p.returncode == 0
    obj = json.loads(p.stdout)
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert obj["hookSpecificOutput"]["additionalContext"] == "FRAME-LINE-1\nFRAME-LINE-2"


def test_wrapper_event_name_passthrough():
    p = _run_wrapper("BeforeAgent", "printf 'x'")
    assert json.loads(p.stdout)["hookSpecificOutput"]["hookEventName"] == "BeforeAgent"


def test_wrapper_empty_mailbox_emits_nothing():
    p = _run_wrapper("SessionStart", "true")   # no stdout
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def test_wrapper_failclosed_on_bad_inner_command():
    p = _run_wrapper("SessionStart", "definitely-not-a-real-command-xyz")
    assert p.returncode == 0        # never break the session
    assert p.stdout.strip() == ""
