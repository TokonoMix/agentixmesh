"""``mesh-onboard-agent`` — onboard a foreign / skeptical agent onto agentixmesh.

Distinct from :mod:`pm_mesh.onboard` (the steward/participant capability-grants wizard). This tool does
the OPERATOR-WIRING for a named harness adapter and EMITS two agent-facing trust artifacts — it never
touches the agent's persona and never coerces the agent.


Two-atom human UX: (1) ONE operator command ``mesh-onboard-agent <harness> <label>``; (2) ONE
``PASTE-TO-AGENT`` line the operator relays through the agent's own channel. The agent self-verifies the
provenance manifest (hash the executed binary + cross-check ``source_commit`` against the live repo) and
opts in through its own approval channel.

Orchestrator, not re-implementer: each harness adapter REUSES the existing ``hooks/<harness>/`` delivery
recipe (a wrapper script plus a scheduled job, for a harness with no stdout-capture hook) or the shared
``pm_mesh.settings_merge`` wiring (Claude), never a second copy of delivery logic. The universal core owns
the manifest, the agent-skill BODY, and the paste-line — byte-identical across harnesses.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from . import onboard_render, provenance, settings_merge

# Universal, harness-agnostic trust vocabulary.
CAPABILITIES = ["inspect", "receive", "send"]
RESTRICTIONS = ["no_network", "no_sudo", "no_exec"]

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_root() -> str:
    """The agentixmesh source repo root (parent of the pm_mesh package)."""
    return _PKG_PARENT


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def _write(path: str, text: str, mode: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


# --------------------------------------------------------------------------- adapters


class HarnessAdapter:
    """Base adapter. A new harness implements only ``wire``/``register_delivery`` + dest paths (small).
    The core owns everything identical across harnesses."""

    name = ""

    def wire(self, label: str, root: str) -> dict:  # pragma: no cover - overridden
        """PURE, test-safe operator-wiring: ensure the delivery mechanism's files exist (idempotently)
        WITHOUT registering the live cron/hook. Returns ``{binary_path, address, notes}`` where
        ``binary_path`` is the EXACT file the delivery mechanism executes."""
        raise NotImplementedError

    def register_delivery(self, label: str, root: str) -> str:  # pragma: no cover - live
        """The live side-effect (cron/hook registration). Invoked ONLY under ``--apply``. MUST be
        idempotent — detect an existing mesh entry for the label and skip/replace it."""
        raise NotImplementedError

    def manifest_dest(self, label: str) -> str:  # pragma: no cover - overridden
        """Absolute path where the provenance manifest is written — resolved by the adapter itself (the
        base dir is harness-specific: a gateway's own data dir, or ~ for Claude)."""
        raise NotImplementedError

    def skill_dest(self, label: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def delivery_controller(self, mailbox_path, harness_config: dict):
        """Factory for this harness's ``DeliveryController``.

        DISTINCT from onboarding (``wire``/``register_delivery``): the controller
        owns the *toggle-many* on/off/status lifecycle for ``mesh-poll``. The
        Only harnesses whose delivery can be toggled have one (Claude Code: the inject
        hook); the base refuses so an unimplemented harness fails loudly rather than
        silently doing nothing.
        ``mailbox_path`` is a ``pathlib.Path``; ``harness_config`` a plain dict.
        """
        raise NotImplementedError(
            f"delivery_controller not implemented for harness {self.name!r} — this harness is wired for delivery at onboarding time, it has no on/off toggle"
        )


class ClaudeAdapter(HarnessAdapter):
    """Claude Code. Delivery = the SessionStart/UserPromptSubmit hook merged into ~/.claude/settings.json
    via the SAME ``settings_merge.merge_hook`` path ``mesh-enroll`` uses — no second copy."""

    name = "claude"
    HOOK_COMMAND = "/usr/local/bin/mesh-inject"

    def _skill_home_dir(self, label: str) -> str:
        return os.path.join(os.path.expanduser("~"), ".claude", "skills", "agentixmesh", label)

    def manifest_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "provenance.json")

    def skill_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "SKILL.md")

    def wire(self, label: str, root: str) -> dict:
        home = os.path.expanduser("~")
        os.makedirs(os.path.join(home, "mesh", label), exist_ok=True)
        return {
            "binary_path": self.HOOK_COMMAND,  # the exact hook the harness executes
            "address": f"{os.getuid()}:{label}",
            "notes": "hook fires on SessionStart + UserPromptSubmit (reuses settings_merge, as mesh-enroll)",
        }

    def register_delivery(self, label: str, root: str) -> str:
        home = os.path.expanduser("~")
        settings_path = os.path.join(home, ".claude", "settings.json")
        version = _package_version()
        rc = settings_merge.merge_hook(settings_path, command=self.HOOK_COMMAND, version=version)
        if rc != settings_merge.EX_OK:
            raise RuntimeError(f"settings_merge.merge_hook failed (rc={rc}) on {settings_path}")
        return f"merged Claude mesh hook into {settings_path}"

    def delivery_controller(self, mailbox_path, harness_config: dict):
        from .poll_controllers import ClaudeController
        return ClaudeController(mailbox_path, harness_config)


class CodexAdapter(HarnessAdapter):
    """OpenAI Codex CLI. Delivery = the SAME stdout-as-context hook contract as Claude Code
    (Codex fires SessionStart + UserPromptSubmit; ``mesh-inject`` plugs in unchanged and reads the
    hook-stdin ``cwd`` for addressing — ``inject._effective_cwd``). Reuses ``hooks/codex/``. So this
    adapter mirrors ``ClaudeAdapter`` and differs only in WHERE it registers (Codex config)."""

    name = "codex"
    HOOK_COMMAND = "/usr/local/bin/mesh-inject"

    def _skill_home_dir(self, label: str) -> str:
        return os.path.join(os.path.expanduser("~"), ".codex", "skills", "agentixmesh", label)

    def manifest_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "provenance.json")

    def skill_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "SKILL.md")

    def wire(self, label: str, root: str) -> dict:
        home = os.path.expanduser("~")
        os.makedirs(os.path.join(home, "mesh", label), exist_ok=True)
        return {
            "binary_path": self.HOOK_COMMAND,  # the exact hook the harness executes
            "address": f"{os.getuid()}:{label}",
            "notes": "Codex fires SessionStart + UserPromptSubmit; stdout injected as context (same "
                     "contract as Claude); mesh-inject reads the hook-stdin cwd for addressing",
        }

    def register_delivery(self, label: str, root: str) -> str:  # pragma: no cover - live side-effect
        import json
        home = os.path.expanduser("~")
        hooks_path = os.path.join(home, ".codex", "hooks.json")
        os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
        try:
            with open(hooks_path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        hooks = data.setdefault("hooks", {})
        added = []
        for event in ("SessionStart", "UserPromptSubmit"):
            arr = hooks.setdefault(event, [])
            # idempotent: skip if any group already runs our command
            if any(
                h.get("command") == self.HOOK_COMMAND
                for grp in arr if isinstance(grp, dict)
                for h in grp.get("hooks", []) if isinstance(h, dict)
            ):
                continue
            arr.append({"hooks": [{"type": "command", "command": self.HOOK_COMMAND, "timeout": 10}]})
            added.append(event)
        tmp = hooks_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, hooks_path)
        return (f"merged Codex mesh hook into {hooks_path}"
                + (f" (added {', '.join(added)})" if added else " (already present)"))


class OpenClawAdapter(HarnessAdapter):
    """OpenClaw. UNLIKE Claude/Codex it has NO stdout-capture hook: the shipped external-injection
    path is a PUSH bridge (``hooks/openclaw/mesh-inject-openclaw.sh`` → ``openclaw system event --text …
    --mode next-heartbeat``) run on a schedule. So ``binary_path`` is the bridge script and delivery is a
    cron/timer rather than a hook. See hooks/openclaw/README.md."""

    name = "openclaw"

    def _bridge_dir(self) -> str:
        # Default to a NON-privileged dir: the mesh promises no sudo and no privileged path.
        # Overridable (tests, and anyone who does want a system-wide bridge).
        return os.environ.get("MESH_OPENCLAW_BIN_DIR",
                              os.path.join(os.path.expanduser("~"), ".local", "bin"))

    def _bridge_dest(self) -> str:
        return os.path.join(self._bridge_dir(), "mesh-inject-openclaw")

    def _skill_home_dir(self, label: str) -> str:
        return os.path.join(os.path.expanduser("~"), ".openclaw", "skills", "agentixmesh", label)

    def manifest_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "provenance.json")

    def skill_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "SKILL.md")

    def wire(self, label: str, root: str) -> dict:
        home = os.path.expanduser("~")
        os.makedirs(os.path.join(home, "mesh", label), exist_ok=True)
        dest = self._bridge_dest()
        canonical = os.path.join(root, "hooks", "openclaw", "mesh-inject-openclaw.sh")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Sync the installed bridge to the reviewed repo source (copy if absent OR content differs), so the
        # agent runs the reviewed recipe AND the manifest's binary_sha256 matches the repo source.
        if not os.path.exists(dest) or _read_bytes(dest) != _read_bytes(canonical):
            shutil.copyfile(canonical, dest)
            os.chmod(dest, 0o755)
        return {
            "binary_path": dest,
            "address": f"{os.getuid()}:{label}",
            "notes": "OpenClaw has no stdout hook; delivery = the mesh-inject-openclaw bridge pushing "
                     "frames via `openclaw system event --mode next-heartbeat`, run on a schedule. A "
                     "system event is a stronger signal than a passive context line — keep the agent's "
                     "capabilities constrained.",
        }

    SCHEDULE = os.environ.get("MESH_OPENCLAW_SCHEDULE", "* * * * *")  # every 1m; mailbox waits until polled

    def register_delivery(self, label: str, root: str) -> str:  # pragma: no cover - live side-effect
        import subprocess as _sp
        label_dir = os.path.join(os.path.expanduser("~"), "mesh", label)
        marker = f"# mesh-openclaw-{label}"
        mesh_root = os.environ.get("MESH_ROOT", "")
        root_env = f"MESH_ROOT={mesh_root} " if mesh_root else ""
        line = f"{self.SCHEDULE} {root_env}MESH_CWD={label_dir} {self._bridge_dest()}  {marker}"
        try:
            current = _sp.run(["crontab", "-l"], capture_output=True, text=True).stdout
        except OSError:
            current = ""
        kept = [ln for ln in current.splitlines() if marker not in ln]  # idempotent: drop old, re-add
        kept.append(line)
        new_tab = "\n".join(kept) + "\n"
        _sp.run(["crontab", "-"], input=new_tab, text=True, check=True)
        return f"registered OpenClaw bridge cron ('{self.SCHEDULE}', {marker}) running {self._bridge_dest()}"


class GeminiAdapter(HarnessAdapter):
    """Google Gemini CLI. Grounded against the vendor docs (geminicli.com/docs/hooks/reference): Gemini HAS a
    command-hook system (``SessionStart`` + ``BeforeAgent``) but does NOT inject raw stdout — a hook
    must emit JSON ``hookSpecificOutput.additionalContext``. So delivery reuses a thin wrapper
    (``hooks/gemini/mesh-inject-gemini.py``) that runs ``mesh-inject`` and re-wraps the frame in that
    envelope. Adapter shape mirrors ``OpenClawAdapter`` (sync a wrapper script from the repo source;
    binary_path = the wrapper) — the difference is only the delivery mechanism."""

    name = "gemini"

    def _bin_dir(self) -> str:
        return os.environ.get("MESH_GEMINI_BIN_DIR", "/usr/local/bin")

    def _wrapper_dest(self) -> str:
        return os.path.join(self._bin_dir(), "mesh-inject-gemini")

    def _skill_home_dir(self, label: str) -> str:
        return os.path.join(os.path.expanduser("~"), ".gemini", "skills", "agentixmesh", label)

    def manifest_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "provenance.json")

    def skill_dest(self, label: str) -> str:
        return os.path.join(self._skill_home_dir(label), "SKILL.md")

    def wire(self, label: str, root: str) -> dict:
        home = os.path.expanduser("~")
        os.makedirs(os.path.join(home, "mesh", label), exist_ok=True)
        dest = self._wrapper_dest()
        canonical = os.path.join(root, "hooks", "gemini", "mesh-inject-gemini.py")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.exists(dest) or _read_bytes(dest) != _read_bytes(canonical):
            shutil.copyfile(canonical, dest)
            os.chmod(dest, 0o755)
        return {
            "binary_path": dest,
            "address": f"{os.getuid()}:{label}",
            "notes": "Gemini has a command-hook but injects JSON additionalContext, not raw stdout; "
                     "delivery = the mesh-inject-gemini wrapper (SessionStart + BeforeAgent) that "
                     "re-wraps the frame in the Gemini envelope. Set MESH_CWD in the hook env.",
        }

    # The two Gemini hook events that carry a delivery: startup/resume/clear, and before each turn.
    EVENTS = ("SessionStart", "BeforeAgent")

    def register_delivery(self, label: str, root: str) -> str:  # pragma: no cover - live side-effect
        import json
        home = os.path.expanduser("~")
        settings_path = os.path.join(home, ".gemini", "settings.json")
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        label_dir = os.path.join(home, "mesh", label)
        try:
            with open(settings_path) as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        hooks = data.setdefault("hooks", {})
        command = f"MESH_CWD={label_dir} {self._wrapper_dest()}"
        added = []
        for event in self.EVENTS:
            arr = hooks.setdefault(event, [])
            entry_cmd = f"{command} {event}"
            if any(
                h.get("command") == entry_cmd
                for grp in arr if isinstance(grp, dict)
                for h in grp.get("hooks", []) if isinstance(h, dict)
            ):
                continue
            arr.append({"hooks": [{"type": "command", "command": entry_cmd, "timeout": 10000}]})
            added.append(event)
        tmp = settings_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, settings_path)
        return (f"merged Gemini mesh hooks into {settings_path}"
                + (f" (added {', '.join(added)})" if added else " (already present)"))


HARNESSES: dict[str, HarnessAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "openclaw": OpenClawAdapter(),
    "gemini": GeminiAdapter(),
}


def _package_version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:
        return "0"


# --------------------------------------------------------------------------- CLI


def _reply_addr_default() -> str:
    return f"{os.getuid()}:agentixmesh"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mesh-onboard-agent", add_help=True)
    p.add_argument("harness")
    p.add_argument("label")
    p.add_argument("--root", default=default_root())
    p.add_argument("--reply-addr", default=None)
    p.add_argument("--apply", action="store_true",
                   help="perform the live delivery registration (cron/hook); default only emits artifacts")
    p.add_argument("--dry-run", action="store_true", help="write nothing; print what would happen")
    args = p.parse_args(argv)

    adapter = HARNESSES.get(args.harness)
    if adapter is None:
        print(f"unknown harness {args.harness!r}; known: {', '.join(sorted(HARNESSES))}", file=sys.stderr)
        return 2

    root = args.root
    reply_addr = args.reply_addr or _reply_addr_default()
    address = f"{os.getuid()}:{args.label}"
    manifest_path = adapter.manifest_dest(args.label)

    paste_line = onboard_render.render_paste_line(
        address=address, manifest_path=manifest_path, source_root=root, reply_addr=reply_addr,
    )

    if args.dry_run:
        print(f"[dry-run] would wire {args.harness} delivery for {address}")
        print(f"[dry-run] would write manifest -> {manifest_path}")
        print(f"[dry-run] would write skill    -> {adapter.skill_dest(args.label)}")
        if args.apply:
            print(f"[dry-run] would register live delivery for {args.harness}")
        print("PASTE-TO-AGENT: " + paste_line)
        return 0

    # Operator-wiring (idempotent, no live registration yet).
    wired = adapter.wire(args.label, root)
    binary_path = wired["binary_path"]

    manifest = provenance.build_manifest(
        binary_path=binary_path, source_root=root, address=wired["address"],
        capabilities=CAPABILITIES, restrictions=RESTRICTIONS, now_iso=_now_iso(),
    )
    skill_text = onboard_render.render_agent_skill(
        address=wired["address"], manifest_path=manifest_path, source_root=root,
        source_commit=manifest["source_commit"], reply_addr=reply_addr,
    )
    _write(manifest_path, provenance.manifest_json(manifest), 0o644)
    _write(adapter.skill_dest(args.label), skill_text, 0o644)

    # The paste-line points at THIS manifest; re-render with the actual address (== wired address).
    paste_line = onboard_render.render_paste_line(
        address=wired["address"], manifest_path=manifest_path, source_root=root, reply_addr=reply_addr,
    )

    if args.apply:
        # Live delivery registration is fail-closed: if it fails, DO NOT hand the operator a paste-line
        # inviting an agent onto a mesh that will not deliver.
        try:
            note = adapter.register_delivery(args.label, root)
        except Exception as exc:  # noqa: BLE001 - report and fail closed
            print(f"delivery registration FAILED: {exc}", file=sys.stderr)
            print("artifacts were written but delivery is NOT wired; not printing the paste-line.",
                  file=sys.stderr)
            return 1
        print(note)
        if wired.get("notes"):
            print("note: " + wired["notes"])

    print("PASTE-TO-AGENT: " + paste_line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
