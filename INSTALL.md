# agentixmesh — host integration (reproducible)

The repo is self-contained, but hangs off **four** host integration points outside the repo.
After a move/reboot/fresh machine, reproduce them like this (`1001` is an example login uid —
substitute your own; `$REPO` = wherever you cloned this repo):

| # | Integration point | Location | What |
|---|---|---|---|
| 1 | Python path | `~/.local/lib/python3.12/site-packages/pm-mesh.pth` | one line: `$REPO` (your clone path) → makes `python3 -m pm_mesh.*` importable everywhere |
| 2 | CLI wrappers | `/usr/local/bin/mesh-send`, `/usr/local/bin/mesh-inject`, `/usr/local/bin/mesh-who`, `/usr/local/bin/mesh-resolve`, `/usr/local/bin/mesh-trust`, `/usr/local/bin/mesh-consent`, `/usr/local/bin/mesh-enroll`, `/usr/local/bin/mesh-badge`, `/usr/local/bin/mesh-onboard` (root), `/usr/local/bin/mesh-whoami` | thin `exec python3 -m pm_mesh.{send,inject,who,resolve,trust_cli,consent,enroll,badge,onboard,whoami} "$@"` (`mesh-whoami` = print your own `uid:project` address — the reliable way to find your uid instead of guessing; `mesh-who` = same-user session discovery; `mesh-resolve` = address-book alias lookup, sender-side convenience only; `mesh-trust` = per-sender-uid trust-level CLI, receiver-owned; `mesh-consent` = leader-read grant/revoke/status, phase 2; `mesh-enroll` = member onboarding/offboarding; `mesh-badge` = harness-independent statusbar unread/held indicator, read-only; `mesh-onboard` = steward/participant Q&A wizard that writes the address book + intent permission matrix, spec §6c) |
| 3 | Skill | `~/.claude/skills/pm-mesh` → symlink → `…/pm-mesh/skill` | canonical source lives in the repo (`skill/SKILL.md`), versioned; the symlink keeps it discoverable for Claude Code |
| 4 | Inject hook | `~/.claude/settings.json` (SessionStart + UserPromptSubmit) | `command: /usr/local/bin/mesh-inject` |

Runtime state lives **outside** the repo (it does not move with it): maildirs in
`~/.local/share/pm-mesh/<uid>:<project>/{new,cur,held}`.

## Reproducing (idempotent)

```sh
REPO="$HOME/agentixmesh"   # wherever you cloned this repo

# 1) Python path
echo "$REPO" > ~/.local/lib/python3.12/site-packages/pm-mesh.pth

# 2) CLI wrappers (root)
sudo tee /usr/local/bin/mesh-send >/dev/null <<'EOF'
#!/usr/bin/env sh
# Defaults to the shared go-live root so a bare send doesn't silently vanish into the
# local root (where an inject hook no longer reads). pm_mesh.group_reexec acquires the
# 'mesh' gid in-process via sg if the process lacks it (no relogin needed). Caller can
# override MESH_ROOT.
: "${MESH_ROOT:=/srv/mesh}"; export MESH_ROOT
exec python3 -m pm_mesh.send "$@"
EOF
sudo tee /usr/local/bin/mesh-inject >/dev/null <<'EOF'
#!/usr/bin/env sh
: "${MESH_ROOT:=/srv/mesh}"; export MESH_ROOT
exec python3 -m pm_mesh.inject "$@"
EOF
sudo tee /usr/local/bin/mesh-who >/dev/null <<'EOF'
#!/usr/bin/env sh
exec python3 -m pm_mesh.who "$@"
EOF
sudo tee /usr/local/bin/mesh-resolve >/dev/null <<'EOF'
#!/usr/bin/env bash
exec python3 -m pm_mesh.resolve "$@"
EOF
sudo tee /usr/local/bin/mesh-trust >/dev/null <<'EOF'
#!/usr/bin/env bash
exec python3 -m pm_mesh.trust_cli "$@"
EOF
sudo tee /usr/local/bin/mesh-consent >/dev/null <<'EOF'
#!/usr/bin/env sh
exec python3 -m pm_mesh.consent "$@"
EOF
sudo tee /usr/local/bin/mesh-enroll >/dev/null <<'EOF'
#!/usr/bin/env bash
exec python3 -m pm_mesh.enroll "$@"
EOF
sudo tee /usr/local/bin/mesh-badge >/dev/null <<'EOF'
#!/usr/bin/env sh
: "${MESH_ROOT:=/srv/mesh}"; export MESH_ROOT
exec python3 -m pm_mesh.badge "$@"
EOF
sudo tee /usr/local/bin/mesh-onboard >/dev/null <<'EOF'
#!/usr/bin/env sh
: "${MESH_ROOT:=/srv/mesh}"; export MESH_ROOT
exec python3 -m pm_mesh.onboard "$@"
EOF
sudo tee /usr/local/bin/mesh-whoami >/dev/null <<'EOF'
#!/usr/bin/env sh
# mesh-whoami — print THIS session's own mesh address (uid:project). No maildir access, so no
# MESH_ROOT needed; uid is os.getuid(), project is the basename of the caller's cwd.
exec python3 -m pm_mesh.whoami "$@"
EOF
sudo chmod +x /usr/local/bin/mesh-send /usr/local/bin/mesh-inject /usr/local/bin/mesh-who \
  /usr/local/bin/mesh-resolve /usr/local/bin/mesh-trust /usr/local/bin/mesh-consent \
  /usr/local/bin/mesh-enroll /usr/local/bin/mesh-badge /usr/local/bin/mesh-onboard \
  /usr/local/bin/mesh-whoami

# 3) Skill symlink
rm -rf ~/.claude/skills/pm-mesh
ln -s "$REPO/skill" ~/.claude/skills/pm-mesh

# 4) Inject hook: see ~/.claude/settings.json (SessionStart + UserPromptSubmit →
#    /usr/local/bin/mesh-inject). Not touched by this script.
```

## Verifying

```sh
cd /tmp && python3 -c "import pm_mesh,os;print(os.path.dirname(pm_mesh.__file__))"  # → $REPO/pm_mesh
mesh-send --help                                                                    # → usage
mesh-resolve --list                                                                 # → the address book
mesh-trust show                                                                     # → your current trust policy
mesh-badge --json                                                                   # → {"new": 0, "held": 0, "senders": [], "address": "..."}
head -1 ~/.claude/skills/pm-mesh/SKILL.md                                           # → "---"
cd "$REPO" && python3 -m pytest -q                                                  # → green
```
