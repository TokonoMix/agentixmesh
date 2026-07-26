# Status badge (`mesh-badge`)

Read this when wiring a statusline, tmux status bar, or a gateway pre-check that
wants a cheap read-only glance at a mailbox. Not needed to send or read messages.

`mesh-badge` gives any harness — a statusline command, a tmux status bar, a gateway agent
doing a pre-check before letting a session take its next turn — a cheap, read-only glance at a
mailbox, without importing any of the mesh's internals.

- **Default output:** one short text line, e.g. `📬 2 · ⏸ 1` — **empty output means nothing to
  report.** `--no-emoji` gives a plain-ASCII equivalent.
- **`--json`:** `{"new": <int>, "held": <int>, "senders": [<uid str>, ...], "address": "<uid:project>"}`.
  `senders` lists only **kernel-verified** sender uids (the same `fstat`-on-open-fd identity
  check as everywhere else in the mesh) — never a project label, subject, or body content.
- **Strictly read-only:** counts and lists files; never claims, moves, or seen-stamps anything.
- **Fail-closed:** any error while gathering yields empty output and exit 0 — it can never break
  a status bar. Pass `--debug` to see the real error while diagnosing.

```sh
mesh-badge                 # "" when nothing to report, else e.g. "📬 2 · ⏸ 1"
mesh-badge --json          # {"new": 2, "held": 1, "senders": ["1200"], "address": "1100:agentixmesh"}
```
