# agentixmesh delivery adapter — OpenClaw

**OpenClaw differs from the other adapters.** Claude Code and Codex CLI both have a hook whose
stdout is injected as context — `mesh-inject` plugs straight in. OpenClaw does **not** have such a
stdout-capture hook. Its shipped external-injection path is a **push command**:

```sh
openclaw system event --text "<text>" --mode next-heartbeat   # or --mode now
```

which enqueues text the agent sees on its next heartbeat turn. So the OpenClaw adapter is a **bridge**
that polls the mesh and pushes any pending DATA frame into OpenClaw — not a hook.

> Note: an earlier design assumed a cron `preCheck` gate. That is a feature request
> (openclaw/openclaw#49339), **not shipped**, so this adapter does not use it. The `system event`
> push above is the shipped mechanism (docs.openclaw.ai/gateway/heartbeat).

## Install

1. Ensure `openclaw` and a `mesh-inject`-capable `python3 -m pm_mesh.inject` are on PATH.
2. Install the bridge, e.g. `cp hooks/openclaw/mesh-inject-openclaw.sh /usr/local/bin/mesh-inject-openclaw`
   and `chmod +x`.
3. Run it on a schedule (the mesh is a mailbox; a message waits until the next poll). Example systemd
   timer or cron entry, with `MESH_CWD` set to the agent's project dir:

   ```
   MESH_CWD=/path/to/agent/project MESH_ROOT=/srv/mesh /usr/local/bin/mesh-inject-openclaw
   ```

## Known limitations (be honest about these before relying on it)

- **Different trust surface.** The frame is pushed as a `system event` `--text` argument. It is passed
  as a single quoted argv element (never re-parsed by the shell) and `mesh-inject` already sanitizes
  control/ANSI/zero-width characters, but a `system event` is a stronger signal to the agent than a
  passive context line — keep the receiving agent's capabilities constrained as always.
- **Delivery durability.** `mesh-inject` marks a message seen on render, so a push failure *after* that
  is not retried on the next poll. Fine for a notify/monitor channel; not yet suitable as the sole
  path for must-not-lose messages (needs a push-then-mark variant).
- **Unverified against a live OpenClaw.** OpenClaw is not installed on the build host, so this recipe
  is written from the shipped docs and the mesh side is proven, but the end-to-end push has not been
  run against a live OpenClaw instance. Verify on a real install before production use.
