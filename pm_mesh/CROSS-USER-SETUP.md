# Cross-user provisioning runbook (agentixmesh phase 2, f2-01)

> **DO NOT auto-execute.** These are `sudo` steps that a human deliberately runs on the host. The
> unattended run documents them; it does not blindly execute them (cross-user go-live = f2-16,
> human-gated).
>
> Background: `.specs/2026-06-26-pm-mesh-fase2-cross-user-plan.md` §4. Same-user (phase 1, `0700`
> owner-only) remains the default and is unchanged; cross-user is **additive** and only activates
> with `$MESH_ROOT=/srv/mesh` or `$MESH_CROSS_USER=1`.

## Perm model (the core)

| Path | Mode | Group | Who may do what |
|---|---|---|---|
| `/srv/mesh` (shared root) | **`rwx-wx--T` + setgid = `3730`** (self-service) | `mesh` | members **mkdir their own mailbox** (group-write) + traverse; **no list** (no group-read → the mesh is not enumerable); **sticky** = no cross-delete of another's mailbox |
| `<root>/<uid>:<project>/` (drop dir, **receiver-owned**) | `2710` (setgid + `710`) | `mesh` | senders traverse (`--x`), no list |
| `<root>/<uid>:<project>/new/` (**dropbox**) | **`1730` + setgid = `3730`** | `mesh` | owner (receiver) `rwx`; group `-wx` = drop+traverse, **no list/read**; **sticky** = no cross-delete |
| `.../cur/`, `.../held/` | `0700` | receiver | receiver-only |

The `new/` mode is octal `0o3730`: **setgid** (`02000`, dropped files inherit group `mesh`) +
**sticky** (`01000`, a sender cannot delete/rename another's pending message) + `0730`
(owner `rwx`, group `-wx`, other nothing). The code (`maildir.CROSS_USER_NEW_MODE`) and
`assert_secure_maildrop(..., mode="cross_user")` enforce exactly this value (fail-closed: anything
looser → `MaildropError`, re-validated every turn with `os.lstat`, no symlink-follow).

**The shared root is also `0o3730` (self-service, NOT `2750`):** `maildir.maildrop()` creates the
receiver's mailbox **on-demand** (`_ensure_dir(drop)`), so a non-root receiver must be able to `mkdir`
under `/srv/mesh` → the root is group-writable. No group-read means members cannot enumerate the mesh;
the sticky bit stops a member from deleting another's mailbox dir. A sender cannot create someone else's
mailbox: `assert_secure_maildrop(expected_uid=<uid-from-address>)` rejects any drop dir not owned by the
correct uid — squatting is refused (fail-closed), not honoured (worst case: a trusted member DoS's an
address, never intercepts it). `config.CROSS_USER_ROOT_MODE` = `0o3730` and `enroll.assert_substrate`
check the root against exactly this value.

## Steps (sudo, once per host)

```sh
# 1. Shared group
sudo groupadd --system mesh

# 2. Add members (senders + receivers). Logout/login needed to activate the group.
sudo usermod -aG mesh alice
sudo usermod -aG mesh bob
sudo usermod -aG mesh claude

# 3. Shared root: self-service — 3730 (setgid+sticky+rwx-wx---) root:mesh.
#    Members mkdir their own mailbox (group-write), cannot list the root (no group-read),
#    sticky prevents cross-delete. (NOT 2750: then a non-root receiver cannot create a mailbox.)
sudo mkdir -p /srv/mesh
sudo chown root:mesh /srv/mesh
sudo chmod 3730 /srv/mesh
```

## Steps (per receiver address)

Run this **as the receiver** (or via `sudo -u <receiver>`), so the dirs are owned by the
receiver — the identity pivot of the model:

```sh
ADDR="1100:backend"          # <uid>:<project> of the RECEIVER
DROP="/srv/mesh/$ADDR"

mkdir -p "$DROP/new" "$DROP/cur" "$DROP/held"
chgrp mesh "$DROP" "$DROP/new"           # drop dir + dropbox in group mesh
chmod 2710 "$DROP"                        # setgid + owner rwx, group --x (traverse, no list)
chmod 3730 "$DROP/new"                    # setgid + sticky + owner rwx, group -wx (drop, no list/read)
chmod 0700 "$DROP/cur" "$DROP/held"       # receiver-only
```

> The application also creates these dirs itself when the receiver uses the mesh
> (`maildir.maildrop(addr, mode="cross_user")`), with exactly the same perms. A **sender**
> cannot modify another receiver's dirs (`PermissionError` is best-effort ignored); the
> fail-closed guarantee comes from re-validation, not from the initial set.

## Verification

```sh
stat -c '%A %U:%G %n' /srv/mesh /srv/mesh/1100:backend /srv/mesh/1100:backend/new
# expected, among others:  drwxr-s---  root:mesh  /srv/mesh
#                          drwx--s---  <receiver>:mesh  .../1100:backend
#                          drwx-ws--T  <receiver>:mesh  .../1100:backend/new   (s=setgid, T=sticky without other-x)
```

## Hardening requirements on the host (council findings f2-01)

Two host properties are **load-bearing** for this model and must hold true on the provisioning host:

1. **The receiver must be a member of `mesh`.** An unprivileged process that sets the setgid bit on
   `new/` with `chmod` while the group is not one of its own groups has the setgid bit **silently
   cleared** by the kernel. Therefore: (a) set the group first (`chgrp mesh`), then `chmod 3730`
   (the code does chown→chmod in that order), and (b) the receiver must be *in* group `mesh`. If
   that fails, `assert_secure_maildrop` refuses fail-closed (missing setgid → `MaildropError`) — no
   silent weakening, but the mesh then simply doesn't work until provisioning is correct.

2. **`fs.protected_hardlinks=1`** (default on modern kernels). The group has `-wx` on `new/`, so a
   sender can create files in it; sticky prevents deleting/renaming another's pending message, but
   **not** creating a hardlink. With `protected_hardlinks`, a user may only hardlink to a file they
   can read/write — that blocks a hardlink to a fellow sender's `0600` message. Verify:
   `sysctl fs.protected_hardlinks` ⇒ `= 1`.

> **Daylight review (before go-live f2-16):** the council also asked for defense-in-depth
> re-validation of the drop-dir owner (not just `new/`) and `openat`/`renameat`-based dirfd pinning
> against TOCTOU. The current exploit paths are already blocked by non-writable parent dirs + the
> lstat assert; these extras are hardening a human weighs before going live.

## Offboarding / revoke (F6, f2-12)

`mesh revoke <uid>` does the **mesh-internal** cleanup (remove from group configs, presence
heartbeats, pending `held/` messages to/from that uid — with an audit entry, idempotent). The
**OS-level** step is a deliberate human/sudo action and belongs alongside it, since the cross-user
drop perms rely on group `mesh`:

```sh
# Remove the user from the shared group — dropping/traversing in /srv/mesh then no longer works.
sudo gpasswd -d bob mesh
# (verify:)  id bob   # 'mesh' should no longer be in the group list
```

Without this OS step, the user retains OS-level access to the shared dropboxes; the mesh tool cannot
enforce that (that is kernel/group administration). Full offboarding checklist (all 3 steps):

```sh
mesh revoke <uid>              # drop consent + presence artifacts (mesh-internal)
mesh-enroll --revoke <user>    # remove the inject hook, skill symlink/copy, onboarding markers
gpasswd -d <user> mesh         # remove OS group membership (admin step; NOT done by --revoke)
```

**Activation note (WSL2):** after enrolling a user, they must start a new login session for group
membership to take effect. On WSL2, this requires `wsl.exe --shutdown` (run from Windows) and
reopening — **WARNING: this terminates the ENTIRE distro** (all shells and processes), not just the
user's shell. Coordinate with the user before running it.

## Optional (F5, see f2-02)

Name secrecy is no longer load-bearing: a dropped message can be made receiver-only readable with
`setfacl -m u:<receiver>:r` so a fellow sender cannot read a pending message, even with the
(>=128-bit) filename. That is the scope of f2-02, not f2-01.
