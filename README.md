# agentixmesh — an agent trust layer for sessions on one machine

Lightweight, file-based messaging channel between Claude Code agent sessions on one
machine. **This repo builds the same-user core, plus the cross-user mechanisms it takes to
extend it — go-live of cross-user itself stays human-gated (see below).**

## The trust boundary — what it actually guarantees

agentixmesh is a **trust layer**, not just a message bus: on one machine it lets separate agent
sessions exchange messages **without inheriting each other's authority**. The guarantees:

- **Unforgeable sender identity.** The sender's uid is kernel-verified via `fstat` on the open fd
  (with `O_NOFOLLOW` + hardlink/`S_ISREG` guards) — a message cannot claim to be from someone it
  isn't. The self-declared `project` label is always marked UNTRUSTED; only the uid is trusted.
- **Every incoming message is inert DATA, never a command.** The body is rendered inside an explicit
  DATA frame with a standing "this is not an instruction" rule and per-line framing + sanitation. An
  agent *reads* it; it does not *obey* it.
- **A human in the loop across accounts.** A cross-user message is withheld — the body is not shown —
  until a human explicitly releases it. Cross-user `auto`-acting is impossible **by construction**,
  not by policy.
- **Read-confidentiality.** Bodies are written `0600` + a receiver-only POSIX ACL, so another user on
  the same box cannot read a message addressed to someone else.

**Scope, stated honestly.** This is a **single-machine** trust boundary. It makes prompt-injection
*hard* and keeps a human in the loop — it does **not** make the receiving LLM *immune*; a
badly-configured agent can still be talked into acting. The guarantee is the boundary and the gate,
not omniscience.

**Validated end-to-end.** The same-user core and cross-user human-gate messaging are proven between
real, separate OS accounts — bidirectional agent↔agent, plus adversarial prompt-injection probes: a
message instructing the receiver to run a shell command and to exfiltrate credentials was correctly
treated as inert data (no side-effect, no leak — the receiving agent recognised and refused it).

**Roadmap, not yet production-hardened.** The higher-authority **leader-gate** (manager / head-leader
co-approval), **group** roles and **leader-read monitoring** exist as designed mechanisms but are
**not** advertised as production security yet — see **Roadmap**.

- Design + external review: see `docs/` (multi-user design + phase-2 cross-user plan)
- Same-user core (design review §17): maildir + owner-uid via `fstat`-on-fd (+ hardlink/`S_ISREG`
  guards), atomic-rename claim + cur/-janitor, replay guard (`id`+`ts_utc`), DATA-frame sanitation,
  `mesh-send` same-user, inject hook (opt-in, no auto-enable).
- Built on the same-user core and documented in `skill/SKILL.md`: **trust tiers** (`mesh-trust`),
  the **address book** (below), **presence discovery** (`mesh-who`), the **onboarding wizard**
  (`mesh-onboard`) and the **status badge** (`mesh-badge`).
- **Cross-user *go-live* itself** (enabling `notify-only` between real, separate accounts in
  production) remains human-gated — see this repo's `CLAUDE.md` §7 for the sign-off checklist.
  The engine-hard invariant does not change with go-live: cross-user `auto`-acting stays
  impossible regardless.
- **Not built yet:** capability grants / credential brokering (phase 2B, design only — see
  `docs/2026-07-03-autonomous-capability-grants-plan.md`) and the other items under
  **Roadmap** below.

Root config: `$MESH_ROOT` (default `$XDG_DATA_HOME/pm-mesh` or `~/.local/share/pm-mesh`).

## It works — end to end

The whole chain (send → deliver → janitor → replay-guard → DATA frame → inject) is proven by
`test_e2e_same_user.py`: one uid, two projects. Example from the command line (same user):

```sh
# from project A — send a message to <uid>:projectB
mesh-send 1000:projectB "hello from A
line2"          # → prints the message id

# from project B — show new messages as a DATA frame (the inject hook does this
# automatically on SessionStart/UserPromptSubmit; you can also run it by hand):
mesh-inject
# <mesh-msg owner_uid=1000 (kernel-verified)> … the body is framed per line behind "│ "
# a second mesh-inject shows nothing more (dedup/replay-guard)
```

`mesh-send` / `mesh-inject` are the console entry points (`python3 -m pm_mesh.send` /
`... .inject`). The inject hook is **opt-in and fail-closed** — see `hooks/README.md` for
enabling it without overwriting your existing `~/.claude/settings.json`.

## Cross-harness delivery

The core (`pm_mesh/`) is harness-neutral: `mesh-inject` reads whatever a harness pipes to a hook
on stdin (if anything) and derives the session address from it, so the same command works
unchanged across harnesses that share the same push-hook contract. Adapters:

- **Claude Code** — `hooks/README.md` (the reference implementation).
- **OpenAI Codex CLI** — `hooks/codex/README.md`. Codex fires the identical `SessionStart` /
  `UserPromptSubmit` events and the same stdout-becomes-context contract, so no core code changes
  are needed — just the wiring. Unit-tested (`test_cross_harness_cwd.py`); end-to-end verification
  against a live Codex binary is pending on a host that has Codex CLI installed.

Hermes and OpenClaw adapters are designed and prototyped, not yet shipped here — see **Roadmap**.

## Address book & friendly names

A shared address book resolves friendly names/aliases to canonical `uid:project` addresses,
so you never have to guess or remember the exact folder basename a peer session is running in.
It is **layered**, each layer only adding or overriding what the previous one didn't set:

1. `data/addressbook.json` (bundled with the repo — the seed everyone starts with)
2. `$MESH_ROOT/addressbook.json` (shared, e.g. maintained by the steward via `mesh-onboard`)
3. `~/.config/pm-mesh/addressbook.json` (personal — your own aliases win)

```sh
mesh-resolve reviewer           # → 1002:reviews   (or exit 1 + a hint if unknown)
mesh-resolve --list             # the whole book: address · display · aliases
mesh-who                        # which addresses are live right now (same-user session discovery)
mesh-send reviewer "hi"         # mesh-send resolves the alias for you before delivery
```

**Trust boundary:** resolution is **sender-side convenience only**. It changes how a name
becomes an address *before you send* — it never touches the receive side, which stays
**kernel-verified** regardless of which alias was used to reach it. So an alias can't forge
who a message is from; at worst a wrong alias sends to the wrong (real) mailbox, exactly like
a mistyped address.

First-time setup for a whole deployment (steward Q&A wizard that writes the address book plus
an intent-only permission matrix) and a harness-independent unread/held indicator are covered
in `skill/SKILL.md` (`mesh-onboard`, `mesh-badge`).

## Roadmap

Actively on the agenda, in the open:

- **Operator-domain trust scoping** — re-key the cross-user human-gate from raw OS uid to
  *operator domain*, so one operator's own fleet of uids can interoperate autonomously while
  every foreign operator stays permanently human-gated. A design exists; building is
  deliberately deferred until a real multi-uid deployment demands it, and any change to this
  trust boundary goes through a dedicated security review first. The invariant itself is not
  up for debate: the clamp gets re-keyed, never removed.
- **Capability grants / credential brokering** (phase 2B) — design only today, see
  `docs/2026-07-03-autonomous-capability-grants-plan.md`.
- **More harness adapters** — Codex CLI now ships (see **Cross-harness delivery** above), flagged
  by external review as the highest-value differentiator. Hermes and OpenClaw are designed and
  prototyped internally; shipping them here is next.

## Tests
```
python3 -m pytest -q          # canonical (709 passed, 1 skipped)
# or, without pytest:
python3 -m unittest discover -s . -p 'test_*.py'
```
