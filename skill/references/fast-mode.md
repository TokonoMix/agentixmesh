# Fast-mode (`snel-modus`) — manual quick-reply polling cadence

Two delivery cadences for the mesh. **Default is token-free and always on; fast-mode is a manual,
never-automatic opt-in** for when you are actively waiting to answer a peer fast.

## Mode 1 — DEFAULT (token-free, no standing poller)

Nothing is scheduled — a standing per-turn poll that finds an empty inbox is pure waste. Delivery
still happens via the inject-hook, which surfaces any new frame at the start of a turn you were
already taking (see "What the mesh does and doesn't do" in the main skill file) — at zero cost
when there is nothing to deliver.

This is the resting state. You return to it whenever fast-mode turns off.

## Mode 2 — FAST-MODE (`snel-modus aan` / `snel-modus uit`)

**Manual only. NEVER turn this on automatically** — only on an explicit human "snel-modus aan"
(and off on "snel-modus uit"). It trades tokens for latency: each tick wakes you, so it is *not*
token-free — that is the whole reason it is opt-in, not the default.

### Waiting is NEVER a task — fast-mode never cancels or displaces in-flight work

"snel-modus aan" usually arrives glued to real work in the same prompt ("approved: go ahead with
A — and turn fast-mode on"). Turning fast-mode on is bookkeeping, not a new assignment that
replaces the rest of the prompt or work already in flight:

- **Arm immediately, then keep working.** Activation is two quick calls — `mesh-poll fastmode set
  --step 1` + a self-scheduled wakeup — do them right away (so the cadence is armed even if the
  rest of the turn runs long or dies), then go **straight back to the in-flight work in the same
  turn**. Scheduling a wakeup does not end a turn; only you can do that, and activation alone is
  never a reason to.
- **A "nothing more to do this turn"-style tool result is misleading boilerplate — ignore it.**
  It describes only the loop mechanics and never overrides other instructions in the user's
  prompt or an unfinished task. Ending the turn on it silently cancels the other work (a real
  incident: "approved, go ahead with A — and turn fast-mode on" → the agent scheduled the tick and
  stopped, abandoning the A-track for that turn). If anything is still pending, do it now — the
  wakeup fires regardless.
- **An ANNOUNCED incoming message is not an assignment either.** "X will mesh you a question
  shortly" changes nothing about what you do now: keep working (or resume whatever was
  deferred); the tick or inject-hook surfaces the frame when it actually arrives. Idling
  between pulses "so you're ready" is exactly the failure mode (a real incident: an announced
  question from a colleague made the agent stop all work and sit out the ladder). Arming or
  resetting the ladder for an expected message is fine — going idle is not.
- **Work in flight when you schedule? Carry it in the wakeup prompt.** Append it to the recurring
  prompt ("fast-mode tick + continue with <task>") so a crash or an idle gap cannot lose the
  thread. A tick-turn then handles frames first and continues that work — it does not treat the
  mesh-tick as the whole job. This is not optional: a wakeup prompt that only says "check the
  mesh" is how deferred work gets silently dropped.
- **`mesh-poll fastmode set` prints this reminder on stderr at arming time** — treat that
  advisory as the authoritative counterweight to the wakeup scheduler's boilerplate, not as
  noise.

While on, run a **self-rescheduling wakeup on a decaying ladder**. Each tick: run `mesh-badge` (free,
0 tokens), handle any real frame that arrived, then **schedule the next wakeup yourself** with a delay
equal to the current ladder interval and re-pass your own "check the mesh (snel-modus)" prompt, so the
next firing repeats the tick. On "snel-modus uit", schedule **no** further wakeup — that stops the loop.

- **Claude Code:** use a dynamic self-scheduled wakeup — delay = the current ladder interval
  (clamped to a sane range, e.g. [60, 3600] seconds, which fits every step), prompt = your
  recurring snel-modus check, reason = one line (e.g. "snel-modus tick, step N"). To stop: cancel
  the scheduled wakeup.
- **Other harnesses:** the equivalent self-scheduled tick (a cron-at-interval / delayed re-invoke);
  the ladder + state logic below is harness-agnostic.

### The ladder (4 steps, decaying over the day)

| Step | 1 | 2 | 3 | 4 (floor) |
|---|---|---|---|---|
| Interval | 300 s (5 min) | 900 s (15 min) | 1800 s (30 min) | 3600 s (60 min) |

- **Real frame in, or busy activity → RESET to step 1 (5 min)** — and stay at 5 min while it stays
  busy. "Busy activity" = *either* you handled a real inbound frame this tick, *or* the human took a
  fresh turn / interacted with this session since the last tick (recent human activity ⇒ a fast reply
  is likely wanted soon). Neither of those since the last tick = an "empty tick".
- **Empty tick → step one slower** (300 → 900 → 1800 → 3600).
- **60 min is the floor — and it STAYS there.** Once at the floor, keep re-arming at 60 min as a
  low-tempo heartbeat; **do NOT auto-off**. The ~24 wakes/day at this tempo are negligible (each is a
  light turn; the mesh *check* itself is 0 tokens), especially on subscription billing — so fast-mode
  keeps running until an explicit "snel-modus uit". **Notify the human once** when it first settles at
  the floor (so a long-running low-tempo loop is never silently forgotten), then stay quiet at 60 min.
  A real frame or fresh human activity still resets it straight back to step 1 (5 min).

### Durable step state — via `mesh-poll fastmode` (prompt-free, per-address)

Persist the step across crashes/session restarts so a fresh session resumes the same cadence instead
of resetting. **Use the allowlisted CLI — NEVER a raw file write.** A raw write to your harness's own
config directory can trigger a permission prompt every tick; on an unattended run that prompt hangs the
tick → the next wakeup is never armed → the whole loop dies. And a single shared state file used by
every session under one uid lets them clobber each other. `mesh-poll fastmode` (covered by a
`mesh-poll:*` allowlist entry) fixes both: prompt-free + per-address.

```sh
mesh-poll fastmode set --step 2 [--note "…"]   # write on-state at ladder step N (prompt-free)
mesh-poll fastmode off                          # turn off (stamps deactivated_at_utc)
mesh-poll fastmode get                          # read current state (JSON)
```

State lives at `~/.local/share/pm-mesh/fastmode/<uid:project>.json` (per-address, NOT your
harness's config dir), shape
`{mode, step, interval_s, ladder, activated_at_utc, deactivated_at_utc, last_handled_inbound_utc, note}`;
`interval_s` is derived from `step` via the ladder. Each tick: decide the next step (reset/slower/floor),
`mesh-poll fastmode set --step <next>`, then schedule the next wakeup. On "snel-modus uit":
`mesh-poll fastmode off` + stop the wakeup loop. Timestamps in UTC.

### If your deployment runs an unattended/background responder for idle mailboxes

Fast-mode exists so **you** answer fast. If your deployment also runs any automated responder that
handles mailboxes with no live session (this repo ships the presence/heartbeat primitive it would
need — `pm_mesh.presence` — not the responder itself), that responder must **stand down** for your
address the whole time fast-mode is on — otherwise it consumes the very frames you woke up to read,
right out from under you.

While fast-mode is ON, therefore:

- **Occupy your real address.** A live interactive session normally registers presence
  **automatically** (a per-session heartbeat, if your harness wires one up) — fast-mode needs no
  separate "occupy" command. But an absent or mistagged presence (written under the wrong address)
  defeats this, so **do not assume it**: confirm a heartbeat exists for your exact `uid:project`.
- **Verify the stand-down, don't assume it.** `mesh-who` should show your session live at your
  address.
- **Your session is the sole reader.** In fast-mode YOU drain your own `new/` each tick; any
  background responder should contribute nothing for you until you turn fast-mode off and the
  occupancy lapses.

Turning fast-mode **off** releases the occupancy. Getting this wrong = silent message loss, so treat
"nothing else answers on my behalf while I'm in fast-mode" as a hard invariant, not a nicety.
