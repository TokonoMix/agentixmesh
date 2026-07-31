# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-07-31

Operability: a read-only self-diagnosis command, and a real default-deny publishing guard.

### Added
- **`mesh doctor`** — strictly read-only diagnosis for the question every adopter eventually
  asks: *"I never got the message — what's wrong with my setup?"* Walks the fixed checklist by
  hand-inspection instead of you doing it: is the inject-hook wired, does the skill symlink
  resolve, what are `MESH_ROOT`/`MESH_ACL`, are the mailbox permissions sane, and is any
  cross-user dropbox mis-owned (the failure that permanently blocks a receiver). It mutates
  nothing, guards every check individually, and always exits 0. Checks it genuinely cannot
  perform — a directory it may not traverse — report `--`, never `OK`: unknown is not ok.

### Changed
- **`PUBLIC-MANIFEST.txt` no longer globs the engine.** A `pm_mesh/*.py` line used to stand
  where 38 explicit module paths now are. That glob made the manifest inert for exactly the
  case it exists to stop — a new module dropped beside the existing ones was auto-published,
  so the default-deny only ever guarded new top-level directories. Adding a module to the
  public build is now a line someone writes on purpose. Tests keep their glob deliberately
  (not product surface, and covered by the org-literal denylist).

## [1.4.0] — 2026-07-31

Identity sync: the own address is session-bound, and same-basename collisions get readable
qualified labels. No change to the trust invariants (human-gate, body-withholding,
kernel-verified identity) — the `from` label remains sender-claimed DATA within one uid;
these changes stop *accidents*, not deliberate same-uid impersonation.

### Added
- **Session-bound own address.** `presence.resolve_own_address()` is the one resolver for
  "who am I": `MESH_CWD` env (explicit identity) > the session presence-heartbeat
  (drift-immune) > `basename(cwd)` (legacy fallback). Used by `mesh-send`, `mesh-whoami`,
  `mesh status`, `mesh-poll fastmode` and `mesh approve` — a drifted shell `cd` no longer
  changes who you are. The dead-from advisory's drift tier is superseded: drift is now
  *corrected* at send (stamped with the session address, transparency note on stderr).
- **Foreign-live-owner send guard.** A cwd-resolved from-address that a live *other* session
  is registered on refuses the send (exit 3) with the remedy named — a uid-shared process
  (gateway, cron) can no longer accidentally speak as another live session. Explicit
  `MESH_CWD` and session-resolved sends are never blocked; killswitch `MESH_FROM_GUARD=off`.
- **Path-qualified addressing (worktree collision).** Two live sessions whose dirs share a
  basename used to share one mailbox. Under a live collision each session now registers a
  readable qualified label — `<base>--<distinguishing-path-segment>` (short path-hash only as
  last resort) — sticky for the session lifetime. A qualified session reads both its boxes
  (atomic base relay-in), so precise replies and base traffic surface in one pipeline.
- **Refuse-on-ambiguity + path addressing in `mesh-send`.** Sending to a base label claimed
  by ≥2 live sessions is refused (exit 4) listing each variant with its path (`--base`
  delivers to the shared box deliberately), and `mesh-send <uid>:/abs/path` addresses the
  session *in* that directory — humans think in folders; the mesh translates.
- **Fast-mode keep-working advisory.** `mesh-poll fastmode set` (arming) prints a stderr
  reminder that waiting is never a task — the counterweight to a wakeup scheduler's
  "nothing more to do this turn" boilerplate. Skill: the side-task rule is broadened
  ("an announced incoming message is not an assignment"; always carry in-flight work in the
  wakeup prompt), and long bodies go via a one-line stdin redirect (a heredoc makes the
  command multi-line → permission prompt, fatal in unattended runs).

## [1.3.0] — 2026-07-27

Reliability sync. No change to the trust invariants (human-gate, body-withholding,
kernel-verified identity).

### Added
- **Dead-from advisory in `mesh-send` (cwd-drift detection).** The from-label is
  basename(shell cwd) at send time; a `cd` that persisted in the tool shell stamps sends
  with an address no session is reading, and the frame's reply-with line steers the reply
  into that dead mailbox — silent loss on both sides. `mesh-send` now warns on stderr:
  precise tier compares the cwd-derived label against the live session's presence heartbeat
  (fires even when a stray reply already auto-created the dead mailbox); fallback tier
  (no heartbeat — cron/terminal) warns when the from-address has no mailbox at all.
  Advisory only: exit code, stdout (the message id) and delivery are unchanged, and any
  internal error in the check is swallowed.

### Fixed
- **Presence heartbeats now record the long-lived agent-session pid** (parent-chain walk,
  harness-agnostic) instead of the short-lived hook subprocess pid — previously a live
  session could look dead to liveness checks within a second of the hook returning. Also
  tags the heartbeat under the session's real address when the hook runs from a different
  cwd, and hardens stale-heartbeat pruning (dead pid → immediate, TTL fallback for the rest).

## [1.2.0] — 2026-07-26

DX-focused sync. No change to the trust invariants (human-gate, body-withholding,
kernel-verified identity).

### Added
- Friendly addressing (address book + `mesh-resolve`/`mesh-who`/`mesh-whoami`), per-recipient
  language routing, a documented quick-reply polling mode (`snel-modus`/fast-mode).

### Changed
- Slimmer skill doc (load-on-demand references).

## [1.1.2] — 2026-07-08

Cross-user hardening & hygiene. No change to the trust invariants (human-gate,
body-withholding, kernel-verified identity) — recommended for any cross-user deployment.

### Fixed
- **Cross-user enrollment on a self-service root.** `mesh-enroll` deferred with exit 10
  ("substrate not ready") on a real deployment because the substrate check asserted the shared
  root was `0o2750` (administered) while the code self-creates each receiver's mailbox on-demand —
  which requires a group-writable root. Aligned `CROSS_USER_ROOT_MODE` to `0o3730` (self-service:
  group-write so receivers create their own mailbox, no group-read so the mesh is not enumerable,
  setgid + sticky to block cross-delete) and updated the runbook + tests to match. Squatting stays
  fail-closed — delivery re-verifies each drop is owned by the address's uid, so the worst case is
  denial-of-service by a trusted member, never interception.

### Changed
- **Leak-gate hardened.** The no-org-literals check now also bans internal host/filesystem paths
  and scans the whole public surface (shipped code, all tests, and docs), not just the runtime
  package — closing the gap that let internal paths slip into docs and test fixtures. Internal
  paths were removed from the public docs, shipped code defaults, and test fixtures; the DCP test
  suite is now self-contained (no dependency on an internal checkout).

## [1.1.1] — 2026-07-07

Skill-install fix. No change to mesh behaviour, the CLI, or any protocol invariant —
upgrade recommended for anyone who installed 1.1.0.

### Fixed
- **Skill installation:** the `pm-mesh` SKILL.md frontmatter `description` exceeded the
  1024-character skill limit **and** contained `: ` (colon-space) sequences that made
  strict YAML parsers reject the frontmatter (`mapping values are not allowed here`),
  breaking installation for downstream users. The description is condensed to 974
  characters and the colon-space sequences are removed; all trigger keywords are
  preserved.

## [1.1.0] — 2026-07-03

Adds friendly-name addressing, onboarding, receiver-set trust tiers, cross-harness
status, a structured-message transport, and the cross-user delivery layer — all on top
of the unchanged core invariant: an incoming message is inert **DATA**, never a command.

### Added
- **Address book & friendly names** — a layered book (bundled seed `<`
  `$MESH_ROOT/addressbook.json` shared `<` `~/.config/pm-mesh/addressbook.json` personal)
  maps aliases and display names to canonical `uid:project` addresses. New `mesh-resolve`
  and `mesh-who` helpers. The book is sender-side convenience only; receive-side identity
  stays kernel-verified, so an alias can never forge who a message is from.
- **Onboarding wizard (`mesh-onboard`)** — a role-based Q&A (steward / participant /
  project agent) that writes the shared address book, a read-only intent matrix, and
  prints the exact `mesh-trust` command each receiver runs themselves. Closed permission
  vocabulary `info | do | write | change` (+ `custom`, an audit-only note). Only `info`
  is enforceable today; anything above it is recorded as intent and stays human-gated.
- **Trust tiers (`mesh-trust`)** — the receiver sets, per sender uid, how much traffic
  arrives automatically (`auto` same-user only, `notify-only`, `human-gate`,
  `leader-gate`, `block`). Cross-user `auto` is engine-hard impossible.
- **Member onboarding (`mesh-enroll`)** — enroll / verify / revoke / out-of-band flow
  for adding a participant account.
- **Sender-claimed subject line** — `mesh-send --subject` shows one extra line in the
  held / notify views, explicitly marked *sender-claimed, untrusted*, sanitized,
  str-coerced and capped; never a routing or decision input.
- **Status badge (`mesh-badge`)** — a harness-independent, read-only, fail-closed
  status command (counts only, plus kernel-verified sender uids; text or `--json`) that
  any status line, prompt, or gateway pre-check can consume.
- **Structured transport (DCP)** — carry a Development Coordination Protocol envelope as
  a mesh body (`dcp-mesh-send` / `dcp-mesh-recv`); the envelope is validated but remains
  an inert claim, never a command.
- **Cross-user delivery layer** — shared-root delivery with group-readable drops, an
  unattended responder script, role/group hierarchy, and consent-gated leader-read
  (fail-closed, receiver-owned).

### Fixed
- Security review of the subject line: a non-string subject in a hand-crafted dropbox
  file could crash the delivery turn or bypass the length cap. Coerced and quarantined.
- Cross-user split-brain and group-gid re-exec issues in delivery.
- Release-spool handling so an approved held message shows its body once, then dedupes.

### Hardening
- Broadened fail-closed quarantine so one malformed message never aborts a delivery turn.

## [1.0.0] — 2026-06-29

First public release: the **same-user, single-machine** core.

### Added
- File-based message transport: per-address maildir (`new/ cur/ held/ seen/`) under a
  configurable `$MESH_ROOT`, with `0700` owner-only directories. No daemon, no ports,
  no privilege.
- Addressing as `uid:project` (project derived from the working-directory basename).
- Kernel-verified sender identity via `fstat` on the open file descriptor
  (`O_NOFOLLOW`, hardlink / `S_ISREG` guards) — never from a self-declared field.
- `mesh-send` and `mesh-inject` console entry points.
- Opt-in, fail-closed inject hook for `SessionStart` / `UserPromptSubmit` that renders
  incoming messages as an anti-injection **DATA frame** (per-line framing, input
  sanitation, replay guard, advisory per-thread turn cap, non-forgeable reply hint).
- `mesh status` read-only mailbox view.
- Comprehensive test suite (standard-library `unittest`, runnable under `pytest`).

### Scope
- This release is **same-user only**. Cross-user and cross-machine operation is a
  separate, security-gated layer in private beta — see `docs/SCALING.md`.

[1.3.0]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.3.0
[1.2.0]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.2.0
[1.1.2]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.1.2
[1.1.1]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.1.1
[1.1.0]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.1.0
[1.0.0]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.0.0
