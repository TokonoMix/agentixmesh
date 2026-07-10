# Per-agent capability profiles (agentixmesh phase 2, f2-13 — F3 / condition 4)

> **The strongest practical hardening against mesh injection sits OUTSIDE the mesh:** set the
> allowed-tools/permissions of each PM session so that an **irreversible action is simply not
> executable**. Then an instruction injected via the mesh cannot trigger it — no matter how
> convincing the text is. Condition 3 (body withholding, f2-04) reduces the odds that such text
> ever reaches the context at all; this boundary is the *safety-net* layer underneath.

## WARNING: APPLYING THIS = HUMAN-GATED (PARKED)

This file + the lint (`pm_mesh/capability_lint.py`) are **built**. **Applying** it to a live
`~/.claude/settings.json` of a PM session has **NOT** been done autonomously — it touches production
config of running agents, which falls under "no writes to project/live config without a human".

**Human-next-action (exact):**
1. Choose the role profile below per PM session.
2. Run the lint **read-only** against the current settings:
   `python3 -m pm_mesh.capability_lint ~/.claude/settings.json mesh-reachable-pm`
3. Manually copy the reported `deny` rules into `permissions.deny` of that settings.json
   (and remove forbidden `allow` rules). Repeat the lint until it's clean.
4. Restart/verify the session. Only then is the capability boundary live.

## Role profiles

### `mesh-reachable-pm` — a PM session that receives mesh messages
Irreversible/outward-reaching actions are **not executable**. `permissions.deny` MUST contain
(prefix-match on the tool-permission string):

| Category | Deny rule | Why |
|---|---|---|
| Privilege escalation | `Bash(sudo:*)` | sudo = everything; never via a mesh injection |
| Destructive wipe | `Bash(rm -rf:*)` | irreversible deletion |
| Service/prod restart | `Bash(systemctl:*)` | a prod restart is visibly irreversible |
| Deploy | `Bash(*deploy*)` | production push |
| External HTTP send | `Bash(curl:*)`, `Bash(wget:*)` | exfiltration / external trigger |
| Group/user administration | `Bash(gpasswd:*)`, `Bash(usermod:*)` | OS-level access change |
| External fetch | `WebFetch` | outward reach / exfil |
| API key creation | `Bash(*api*key*)` | irreversible credential creation |
| External MCP sends | `mcp__<external-gateway>__*`, `mcp__*send*` | message sent outward (push/chat channel, etc.) |

`permissions.allow` must **not** contain any rule matching one of the above substrings
(`sudo`, `rm -rf`, `systemctl`, `deploy`, `curl`, `wget`, `gpasswd`, `usermod`). `defaultMode` must
**not** be `bypassPermissions` (that bypasses every deny).

The deny list was extended by the **council (f2-13)** with: network egress (`ssh`,`scp`,`rsync`,`nc`,
`netcat`,`socat`,`telnet`,`ftp`), git-destructive/outbound (`git push/reset/clean`), destructive fs
(`dd`,`shred`,`mkfs`), service/power (`service`,`reboot`,`shutdown`), persistence (`crontab`,`at`), and
package managers (`apt`,`pip install`,`npm install`).

### WARNING: the fundamental limitation — prefix-deny is BYPASSABLE (council f2-13)
A deny on `Bash(curl:*)` only matches when `curl` is the leading token. An injected instruction can
bypass that via **interpreter tunnels** (`bash -c "curl …"`, `python -c "import socket…"`,
`node -e …`), **command chaining** (`cd /tmp && curl …`, `x; curl`, `xargs curl`), or **encoding**
(`echo <base64> | base64 -d | sh`). A deny list is therefore **necessary but not sufficient**.

**The robust form is therefore deny-by-default + a curated allow-list** (`mesh-reachable-pm-strict`):
no general `Bash(*)`, no general interpreters (`python`,`node`,`bash -c`,`sh -c`,`eval`,`xargs`)
in `allow`. The lint flags an interpreter/wildcard in `allow` as `bypass_allow` (medium) precisely
because it reopens the entire deny list.

### `mesh-reachable-pm-strict` — extra strict (read-only + repo-internal edits only)
Like `mesh-reachable-pm`, plus: **deny-by-default** (no general `Bash(*)`); `allow` contains only a
curated list of read-only commands (e.g. `Bash(ls:*)`, `Bash(cat:*)`, `Bash(git status:*)`,
`Bash(git diff:*)`, `Bash(python3 -m pytest:*)`) — **no** general interpreter and **no** wildcard;
`Write`/`Edit` only within the repo. This is the recommended form for a mesh-reachable PM because it
closes the prefix-deny bypass. (Template; refine the allow list per role when applying.)

## What this is NOT
No guarantee: the receiving LLM remains the weak point (design §8), and the capability boundary only
covers what goes through tools. It is the **enforcement** layer that replaces the channel convention
"no irreversible actions" with something an injection cannot bypass.
