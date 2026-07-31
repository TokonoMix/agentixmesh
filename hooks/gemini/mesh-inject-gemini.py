#!/usr/bin/env python3
"""agentixmesh delivery wrapper — Google Gemini CLI.

Gemini CLI has a command-hook system (``SessionStart`` + ``BeforeAgent``), but UNLIKE Claude Code /
Codex it does NOT inject a hook's raw stdout as context — it expects the hook to print a JSON object
whose ``hookSpecificOutput.additionalContext`` string is injected (docs: geminicli.com/docs/hooks).
So the raw ``mesh-inject`` DATA frame must be WRAPPED in that envelope. This tiny wrapper does exactly
that and nothing else: it runs the canonical delivery command, captures its stdout (the already-
sanitized frame), and emits the Gemini envelope.

Usage (from the Gemini hook config): ``mesh-inject-gemini <SessionStart|BeforeAgent>``. Address the
right mailbox by setting ``MESH_CWD`` (and ``MESH_ROOT`` for cross-user) in the hook command's env —
the onboarder bakes those in, so this wrapper does not depend on Gemini's stdin ``cwd``.

**Fail-closed**: any error → print nothing and exit 0. A hook must never block or fail a session.
"""
import json
import os
import shlex
import subprocess
import sys


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = argv[0] if argv else "SessionStart"
    # Inner delivery command; overridable for tests (default = the canonical mesh-inject entrypoint).
    cmd = os.environ.get("MESH_INJECT_CMD", "python3 -m pm_mesh.inject")
    try:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=10)
        frame = proc.stdout
        if not frame.strip():
            return 0  # nothing pending → emit nothing (Gemini injects no context)
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": frame,
            }
        }))
        return 0
    except Exception:
        return 0  # fail-closed: never break a Gemini session


if __name__ == "__main__":
    raise SystemExit(main())
