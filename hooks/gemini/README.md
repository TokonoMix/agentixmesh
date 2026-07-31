# agentixmesh delivery adapter — Google Gemini CLI

Gemini CLI has a command-hook system (`SessionStart`, `BeforeAgent`), but **unlike Claude Code and
Codex it does not inject a hook's raw stdout as context**. Its hooks must print a JSON object whose
`hookSpecificOutput.additionalContext` string is injected (docs: geminicli.com/docs/hooks/reference).
So agentixmesh delivers to Gemini through a thin wrapper that runs `mesh-inject` and re-wraps the DATA
frame in that envelope.

## Mechanism

- **Wrapper:** `hooks/gemini/mesh-inject-gemini.py` runs the canonical delivery command
  (`python3 -m pm_mesh.inject`), captures its stdout (the already-sanitized DATA frame), and emits
  `{"hookSpecificOutput": {"hookEventName": "<event>", "additionalContext": "<frame>"}}`. Empty
  mailbox → prints nothing (Gemini injects no context). Fail-closed: any error → exit 0, never blocks.
- **Events:** wire it on `SessionStart` (startup/resume/clear) and `BeforeAgent` (before each agent
  turn — the Gemini analogue of Claude's `UserPromptSubmit`). The hook `command` passes the event name
  as argv so the envelope's `hookEventName` matches.
- **Addressing:** agentixmesh addresses a mailbox as `uid:project`. Set `MESH_CWD` (and `MESH_ROOT`
  for cross-user) in the hook command's env — `mesh-onboard-agent gemini <label>` bakes
  `MESH_CWD=~/mesh/<label>` in, so delivery does not depend on Gemini's stdin `cwd`.

## Install (or just run `mesh-onboard-agent gemini <label>`)

1. Put `mesh-inject-gemini` on PATH (or use an absolute path), and ensure `python3 -m pm_mesh.inject`
   resolves (the `pm-mesh` `.pth`).
2. Add two hooks to your Gemini config (`~/.gemini/settings.json`, `hooks` block), each `type:
   "command"`:
   - `SessionStart` → `command: "MESH_CWD=<label-dir> mesh-inject-gemini SessionStart"`
   - `BeforeAgent`  → `command: "MESH_CWD=<label-dir> mesh-inject-gemini BeforeAgent"`
   Compose into your existing arrays; do not overwrite. The hook is fail-closed and independent.

## Trust surface

Identical to the other adapters: the frame is already sanitized (ANSI/zero-width/control stripped,
per-line framed) by `mesh-inject`; the wrapper only JSON-wraps it. It remains **inert DATA** — a
frame authorizes nothing. Keep the receiving agent's capabilities constrained as always.
