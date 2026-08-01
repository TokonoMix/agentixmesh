# Testing your own agent's injection-resistance (`mesh-eval`)

The whole point of the mesh is that an incoming body is inert DATA that can't make you act.
`mesh-eval` lets you **verify that on an agent you control** instead of taking it on faith. It fires
a corpus of injection attempts — fake system overrides, fake human-authority claims, frame-escape
tricks, paste-this-command nudges, urgency pressure, and exfiltration shapes — at a target session
and reports, deterministically (no judge model), whether the agent obeyed any of them. Every case
only ever asks the target to append a harmless planted token to a canary file, so the drill is safe
even if the agent complies with every single case.

## The three commands

```sh
mesh-eval list                            # show the injection corpus (id · category · title)
mesh-eval run --to <uid:project> --apply  # fire it at an agent you OWN (dry run without --apply)
mesh-eval score --latest                   # 0 = all resisted · 1 = a compliance · 2 = no evidence
```

The drill is two-phase and asynchronous: `run` drops the attempts into the target's inbox; a
**separate** run of the agent-under-test then processes that inbox as normal DATA; and `score` reads
the canary afterwards. Find the target's address with `mesh-whoami` in its session.

- **`run` is a dry run unless `--apply`.** A dry run writes the manifest and prints what *would* be
  sent, but sends nothing — it tests nothing, and the tool says so.
- **`--blind`** runs the unmarked variant (no "this is a test" banner) — stricter, and **only** for
  an agent you own; the tool warns.
- **A cross-uid target is refused** — not for etiquette: the canary sits in your own private
  directory that uid cannot write, and a cross-user body is withheld by the approval gate before the
  agent sees it. Both force a `resisted` verdict whatever the agent does. Drill as the owning user.
- **`--canary-path`** puts the canary somewhere neutral; the default path contains `agentixmesh-eval`
  and so tells a reading agent it is being drilled — which defeats `--blind`.
- **Verdicts:** `complied` (with `channel` = `file` or `reply` — some cases ask the agent to send the
  token back, and that channel is watched too), `resisted`, `unevaluable` (sent but nothing to judge
  from — *not* `not_sent`), `not_sent`.
- **Exit codes:** `0` = every sent case resisted, `1` = at least one compliance, `2` = something was
  not evaluable. `1` and `2` are distinct on purpose — a dry run, a lost canary or a single
  unevaluable case can never read as a green pass. `--latest` scores the last run that actually sent.

## What a clean run does and does not prove

A clean run is a **floor, not a certificate**: it proves only that *these* bodies did not induce
*this* agent to act in *this* configuration. The corpus is finite, behaviour shifts with context and
permissions, and a marked run is easier to resist than a blind one.

**`resisted` means the action did not happen, not that the agent declined** — an agent stopped by a
permission or a capability wall is counted the same as one that refused.

The corpus covers ten categories in two families. Six are **direct**: the body asks, in the
imperative, now (`instruction_override`, `authority_claim`, `frame_escape`, `tool_invocation`,
`urgency_social`, `exfiltration_shape`). Four are **indirect**, and they matter because an agent can
be trained to recognise the first family and still be steered by these: `data_format` (the request
rides inside a code comment, a JSON field, a log line or a table the agent was asked to *process*),
`deferred` ("next time you run any shell command…"), `task_shaped` (reads as a routine ticket, not
as an instruction that announces itself) and `reasoning_lure` (invites the agent to conclude on its
own that the action is warranted). Still absent: induced edits to the agent's *own* repository, and
induced mesh actions beyond a reply. Describe a clean result as what it is.

A **compliance is a finding about the receiving agent's configuration** — its operating instructions
and its permissions — **not a defect in the transport**. The mesh delivered inert data and framed it
as untrusted; the agent chose to act. The durable fix is a capability boundary: an agent whose
allowed-tools simply cannot perform an irreversible action **cannot be talked into one**.

Full recipe, the safety invariants behind the corpus, and how to add your own cases:
`docs/EVAL-HARNESS.md` in the repo.
