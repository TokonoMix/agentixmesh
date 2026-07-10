# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.1.2]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.1.2
[1.1.1]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.1.1
[1.1.0]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.1.0
[1.0.0]: https://github.com/TokonoMix/agentixmesh/releases/tag/v1.0.0
