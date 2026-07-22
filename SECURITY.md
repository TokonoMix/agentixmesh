# Security Policy

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities privately by emailing:
`systeembeheer@interip.nl`

Include a description of the issue, steps to reproduce, and any relevant context. You will receive an acknowledgement within a few business days.

## Threat Model

agentixmesh is a **single-machine, same-user** message channel. The sender's uid is kernel-verified via `fstat` on an open file descriptor — the uid field in a message cannot be forged by another process running as the same user. An incoming mesh frame is treated as inert DATA, not as an instruction.

## Prompt-Injection Caveat

The DATA-framing **mitigates** prompt injection — it does **not eliminate** it. The receiving LLM remains the ultimate trust boundary. A message body grants no authority: the receiving session must not execute code, reveal secrets, or take irreversible actions based solely on the content of a mesh message body.

## Adversarial validation

The trust boundary is exercised by an adversarial red-team suite (`test_redteam_*.py`) that
actively attacks the invariants and asserts each attack fails. It covers:

- **sender/identity forgery** — a message whose self-declared `from` field claims a different
  uid than the real file owner is resolved on the kernel-verified owner (`fstat`-on-fd), never on
  the claimed field;
- **prompt-injection sanitation** — forged frame tags, smuggled turn markers (fullwidth,
  zero-width, bidi, `U+2028`/`U+2029` line separators) and imitated metadata lines stay inert and
  line-prefixed inside the DATA frame;
- **gate body-withholding** — a gated message reveals only inert metadata; the body never reaches
  the context window before approval;
- **replay / dedup** — a message is delivered at most once, keyed on the verified receiving
  address, not on any attacker-controllable field;
- **filesystem attacks** — symlink/hardlink swaps, non-regular files and unsafe modes are
  rejected fail-closed;
- **same-uid `auto` defense-in-depth** — the only path to autonomous handling
  (`sender_uid == my_uid`) now requires the caller to explicitly assert kernel-verification
  (`sender_verified=True`). Without that assertion the resolver is **fail-safe**: it degrades to a
  human gate rather than acting autonomously, so a caller that ever passes an unverified uid can
  never silently reach `auto`. A foreign uid is engine-clamped and can never reach `auto`
  regardless of policy.

All of these pass in the current release.

## Out of Scope (this public release)

**In scope** (please do report): everything shipped in this repository. That includes same-user
delivery *and* the cross-user layer — `consent.py`, `groups.py`, `release.py`, the leader-gate, and
the setup described in `pm_mesh/CROSS-USER-SETUP.md`. An earlier version of this file said
cross-user was "not exposed here"; that was wrong — the code is in the tree, so it is in scope.

**Out of scope** for this repository:

- **Higher-authority layers** (operator/superadmin roles above the trust levels documented here) —
  not shipped in this repository. There is nothing here to report against.
- **Cross-machine / networked transport.** agentixmesh is single-machine by design; there is no
  network listener, no port, and no remote transport in this code. A report that assumes one is
  describing a system other than this one.
- **A trusted participant with root-equivalent power on the host.** The identity guarantee is that
  the *kernel* attests the sender's uid. Someone who can already act as root (or as another user)
  can defeat any file-based scheme; that is a stated assumption of the model, not a vulnerability
  in it. See "Prompt-Injection Caveat" for the other stated limit.

## Supported Versions

Only the current release on the `main` branch is supported.
