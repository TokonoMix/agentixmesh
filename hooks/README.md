# agentixmesh delivery hook — opt-in installation

The `mesh-inject` hook shows new mesh messages as an unambiguous **DATA frame** at the start of
a session and before each prompt. Installation is **opt-in and additive**: you enable it yourself
by **composing** a small fragment into your harness's own hook config — a live config file is
never touched automatically. See also design **§5**.

This directory covers **Claude Code** below. For **OpenAI Codex CLI**, which fires the same
`SessionStart`/`UserPromptSubmit` hook events, see `hooks/codex/README.md` — the core (`pm_mesh/`)
is harness-neutral, so only the wiring differs.

## What it does (one turn, fail-closed)

On `SessionStart` and `UserPromptSubmit`, `mesh-inject` runs once:

1. determines the own address (`<uid>:<project>`) and ensures the maildrop exists;
2. runs the janitor (orphaned `cur/` messages back to `new/`);
3. consumes each new message (atomic claim + **kernel-verified** sender uid via `fstat`),
   shows fresh messages via the anti-injection frame, and marks them as seen + shown.

**Fail-closed guarantee:** any unexpected error stops further printing and returns **exit 0**. The
hook can therefore never block or fail an agent run — in the worst case it just doesn't show a
message for a turn.

## Enabling it

1. Make sure `mesh-inject` is on your `PATH`. For example, a wrapper that invokes the package:

   ```sh
   #!/usr/bin/env sh
   exec python3 -m pm_mesh.inject "$@"
   ```

   Make it executable and put it on your `PATH`, or replace `command` in the snippet with the
   absolute path to your venv python, e.g. `"/path/to/venv/bin/python -m pm_mesh.inject"`.

2. **Merge** `hooks/settings-snippet.json` into your `~/.claude/settings.json`. The fragment is
   additive: add the `mesh-inject` entries **inside** your existing `SessionStart` and
   `UserPromptSubmit` arrays. Don't replace the whole `hooks` block.

## Coexisting with the TZ/locale hook

Already have a TZ/locale hook on `SessionStart` (and/or `UserPromptSubmit`)? Then those arrays
already have entries. Just place the `mesh-inject` entry **next to** it in the same array —
Claude Code runs all entries of an event. Example of a composed `SessionStart`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "<your-existing-TZ-hook>" } ] },
      { "hooks": [ { "type": "command", "command": "mesh-inject" } ] }
    ]
  }
}
```

Order doesn't matter for `mesh-inject`: it is fail-closed and independent of the other hooks.
