# agentixmesh delivery adapter — OpenAI Codex CLI

agentixmesh delivers messages by having the harness run one command (`mesh-inject`) at session start
and before each prompt, and injecting that command's stdout as context. Codex CLI supports the exact
same hook contract as Claude Code, so **no code changes are needed** — the core (`pm_mesh/`) is
harness-neutral. This adapter is just the Codex wiring plus one cross-harness detail (session `cwd`).

## Why this works unchanged

- **Same events.** Codex fires `SessionStart` and `UserPromptSubmit`, the same names Claude Code uses.
- **Same stdout contract.** For both events, "plain text on stdout is added as extra context." The
  DATA frame `mesh-inject` prints is delivered as-is.
- **Same fail-closed guarantee.** `mesh-inject` returns exit 0 on any error, so it can never block or
  fail a session, regardless of harness.

## The one cross-harness detail: session `cwd`

agentixmesh addresses a mailbox as `uid:project`, where `project` is the basename of the **session's**
working directory. Claude Code runs the hook in the session dir, so the process cwd is correct. Codex
instead pipes the session details — including `cwd` — as JSON on the hook's **stdin**, and may run
the hook from a different directory.

`mesh-inject` handles this automatically: it reads the hook's stdin JSON (non-blocking, fail-closed)
and uses its `cwd` to address the right mailbox. Precedence is `MESH_CWD` env > stdin `cwd` >
`os.getcwd()`. If you'd rather be explicit, set `MESH_CWD` in the command and skip stdin entirely.

## Install

1. Put `mesh-inject` on your `PATH` (a thin wrapper `exec python3 -m pm_mesh.inject "$@"`), or replace
   `command` in the snippet with an absolute interpreter path.
2. Compose **either** snippet into your Codex config (Codex accepts both and merges them):
   - `hooks/codex/config-snippet.toml` → merge into `~/.codex/config.toml`, **or**
   - `hooks/codex/hooks-snippet.json` → merge into `~/.codex/hooks.json`.

   Add the `mesh-inject` entries *inside* your existing `SessionStart` / `UserPromptSubmit` arrays;
   do not replace the whole block. Order does not matter — the hook is fail-closed and independent.

## Verify

From a project directory that has a mailbox, send yourself a test message and start a Codex session;
the DATA frame should appear at session start. To verify the cwd handling explicitly:

```sh
MESH_CWD="$PWD" mesh-inject   # should print any pending frames for uid:<basename of $PWD>
```

## Status

The wiring and the `cwd` handling are unit-tested (`test_cross_harness_cwd.py`). End-to-end
verification against a live Codex binary is pending on a host that has Codex CLI installed; the
contract above is taken from the Codex hooks documentation
(https://developers.openai.com/codex/hooks).
