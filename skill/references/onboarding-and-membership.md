# Onboarding, roles, permissions & membership

One-off setup procedure. Read this when installing the mesh, onboarding an
account, or adding/removing a member — not on every turn.

## Roles

Everything below is described by **role**, never by a person's name — a mesh deployment can
have any number of participants and the docs must work unchanged for all of them.

- **steward** — the human ultimately responsible for a mesh deployment, designated once at
  first install. Runs the onboarding wizard's `steward` flow (below); owns the shared address
  book and an *intent-only* cross-account permission matrix — but never another account's own
  receive policy (see "The invariant that makes this safe" under Onboarding).
- **participant account** — a colleague with their own OS uid, running their own Claude Code
  sessions. Owns their own trust policy (`mesh-trust`) and, on the cross-user extension, their
  own consent artifacts; nobody else can set these on their behalf.
- **project agent** — a single Claude Code session inside one project directory — the actual
  unit that sends and receives mesh messages, addressed `uid:project`. Every participant
  account runs any number of these.
- **provider agent** *(future — phase 2B, design only, not built)* — a project agent with one
  extra, human-policed capability: issuing scoped, short-lived credentials to other agents
  under a human-signed grant policy. See [capability-grants.md](capability-grants.md).

Examples throughout use neutral uids `1100`/`1200` for "a participant account" —
substitute your own.

## Permission vocabulary — the closed set behind onboarding

The onboarding wizard (`mesh-onboard`, below) and the capability-grants design speak a single,
**closed** permission vocabulary — never free text:

| level | meaning | enforced today? |
|---|---|---|
| `info` | read + reply with words | **yes** — this is the trust tiers above: `notify-only` cross-user, already `auto` within one uid |
| `do` | take an action on another's behalf | **no** — recorded as *intent only*; stays human-gated until phase 2B is built and reviewed |
| `write` | create/modify something for another | **no** — same, intent only |
| `change` | alter another's config/policy | **no** — same, intent only |
| `custom` | free-text audit note | **never a decision input** — documentation only, ignored by every enforcement path |

Only `info` has teeth today. `do`/`write`/`change` describe *intent* for a future phase
(capability grants — design only, not built; see [capability-grants.md](capability-grants.md))
and are stored with a fixed "intent-only above info" note — confirming one never changes what
actually happens. `custom` is a note a human can read later; it must **never** be read by any
decision path (the #1 finding of the cross-vendor review of that plan: an evaluator that reads
free text from the requester is itself the injection target).

## Onboarding (`mesh-onboard`)

`mesh-onboard` is a Q&A wizard that turns the roles above into the files the mesh already
reads — it introduces **no new rights**; it only writes what `mesh-trust`, the address book,
and the (future) capability-grant layer already understand.

**`mesh-onboard steward`** — run once, by the steward, at first install:
- walks through accounts, their projects, and proposed permission pairs (closed vocabulary
  `info | do | write | change`, plus a `custom` audit note);
- writes/merges entries into the shared `$MESH_ROOT/addressbook.json` (a **merge**, never an
  overwrite — later layers still win, per "Address book" above);
- writes an **intent-only** permission matrix to `$MESH_ROOT/permissions.json`;
- for every cross-account `info` pair, **prints** the exact `mesh-trust grant <uid> notify-only`
  command the *receiving* participant must run themselves — the wizard never runs it and never
  touches another account's own trust policy.

**`mesh-onboard participant`** — run by each participant account, in their own session:
- reads the matrix, shows only the pairs proposed *to their own uid*;
- asks per-sender confirmation;
- only for the enforceable `info` level, and only on an explicit yes, writes to the
  participant's **own** trust policy (`mode 0600`, same file `mesh-trust` uses — the wizard
  never touches anyone else's). `do`/`write`/`change` are recorded as intent only — confirming
  one never causes a write, because no enforcement path exists for them yet.

**The invariant that makes this safe:** a compromised or over-eager steward session can
*propose* a permission across an account boundary, but can never *grant* it — only the
receiving account, running its own onboarding (or `mesh-trust`) in its own session, can change
its own receive policy. Within one account, the steward's matrix is directly authoritative;
across accounts it is a proposal, never an elevation.

```sh
mesh-onboard steward                  # interactive Q&A; or --answers <file> for automation
mesh-onboard participant              # reads the matrix, asks per-sender confirmation
```

**Adding a single address-book entry without the full wizard:** `mesh-addressbook-add` is the
thin, scriptable primitive `mesh-onboard steward` builds on — useful as an enroll/onboarding
sub-step so a new `uid:project` lands in the shared book automatically. Same merge-never-overwrite
semantics: a new address is appended, an existing one only gets its *empty* fields filled and its
aliases unioned — a non-empty field a steward already wrote is never replaced.

```sh
mesh-addressbook-add 1200:staging --display "Staging clone" --dir /home/bob/dev/staging \
  --alias staging-reviewer --note "auto-added by enroll"
```

## Adding a member (including yourself)

Enroll is **human-initiated only** — a mesh message body never triggers it ("a body authorizes nothing").
An agent invoking it non-interactively must pass `--yes`.

Cold-start / self-enroll: an admin enrolls their **own** OS user first, then others.

    mesh-enroll <os-user>            # admin (usually root) adds an EXISTING user to the mesh
    mesh-enroll <os-user> --verify   # read-only: is it activated yet?
    mesh-enroll <os-user> --out-of-band-message   # copy-pasteable notice to hand the user

The enrolled user must start a **new *login* session** — a fresh Claude session inside the same login
is NOT enough (group membership takes effect at login). On WSL2, run `wsl.exe --shutdown` (WARNING:
terminates the entire distro) and reopen. The welcome then appears automatically.

**If enroll prints a deferral (non-zero exit + a stderr remedy):**
- `10` host substrate missing → an admin must run `pm_mesh/CROSS-USER-SETUP.md` first.
- `11` membership deferred (no root) → run `sudo usermod -aG mesh <user>`.
- `12` per-user wiring deferred → make the checkout world-readable, or run enroll as the user.
- `13` settings-merge deferred → the user's `~/.claude/settings.json` is malformed; add the hook manually.

## Removing a member

Offboarding is a 3-step checklist:

    mesh revoke <uid>                 # drop consent + presence artifacts
    mesh-enroll --revoke <user>       # remove the hook, skill (symlink or copy), markers
    gpasswd -d <user> mesh            # remove OS group membership (admin OS step; NOT done by --revoke)

## Platform matrix

Supported now: Linux, WSL2. macOS: experimental/gated. Native Windows: roadmap (fail-closed).
