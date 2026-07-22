# Leader-read consent artifact (agentixmesh phase 2, f2-14 — F4 / condition 5)

> **Leader-read = the head leader (e.g. uid 1100) can read the inbox of a subject (e.g. uid 1200)**,
> including messages from third parties to that subject. That is a **privacy choice the subject
> itself explicitly grants and can revoke**. Default = **OFF** (fail-closed): without a valid
> consent artifact, `consent.leader_read_allowed(...)` returns `False` and no leader-read may
> take place.

## WARNING: OBTAINING CONSENT = HUMAN-GATED (PARKED)

The template + the fail-closed default are **built**. The subject's actual consent is a
**human action** and is **not** obtained autonomously.

**Human-next-action (exact):**
1. Discuss with the subject what leader-read entails (see "What the head leader can see" below).
2. If the subject agrees, **one command** is run **in the subject's own context** (as the OS user
   for that uid, with group `mesh` — so the file has their kernel ownership). This can be done
   **within Claude Code** (the subject's agent runs as their own uid with mesh active → they simply
   tell their session *"grant the head leader leader-read consent"* and the agent runs the command)
   **or** in a terminal — either is equally valid, because it is their uid. **No re-login needed**
   if the session was started after their group membership was granted. The command:
   ```sh
   MESH_ROOT=/srv/mesh mesh-consent grant --leader-uid <head-leader-uid>
   ```
   It remains the subject's **conscious** consent (they issue the command); the requirement is only
   that a *third party* (another uid) cannot fabricate it — that still holds via the
   kernel-ownership check.
   That's all. The CLI (`pm_mesh.consent grant`) writes the artifact itself: `subject_uid` = their
   own uid (self-owned = load-bearing), all fields filled in, `ts_utc` = now, mode `0o640` (owner rw,
   group `mesh` r so the leader can read it; no group/other write — the fail-closed check requires
   that). The consent dropbox `/srv/mesh/consent` is provisioned in advance (3730 root:mesh), so the
   subject doesn't need to create anything. The CLI **immediately prints**
   `leader_read_allowed(...): True` for confirmation.
   > Manual alternative (if the wrapper isn't available): `python3 -m pm_mesh.consent grant --leader-uid <head-leader-uid>`.
   > Revoking: `mesh-consent revoke`. Checking status: `mesh-consent status --leader-uid <head-leader-uid>`.
3. Verify (by the head leader or the subject): `MESH_ROOT=/srv/mesh mesh-consent status --subject-uid <subject-uid> --leader-uid <head-leader-uid>`
   → `leader_read_allowed(...): True`.

## What the head leader can see with active leader-read
- The content of the subject's inbox messages (incl. from third parties to the subject).
- This is **monitoring-with-consent**, NOT a tamper-proof accountability/audit model (see OPERATING.md
  phase-2 privacy section). It does not guarantee that logs cannot be modified by a participant.

## Revoking
The subject revokes by removing the artifact or setting `"revoked": true`, or automatically via
**offboarding**: `mesh revoke <subject-uid>` (f2-12) removes the consent artifact along with it
(`consent_revoked` in the revoke summary). After revoking, `leader_read_allowed` returns `False` again.

## Fields
| Field | Meaning |
|---|---|
| `subject_uid` | uid whose inbox may be read (must == file owner) |
| `leader_uid` | uid of the head leader who may read |
| `granted` | `true` = consent given |
| `revoked` | `true` = revoked (→ leader-read OFF) |
| `ts_utc` | UTC timestamp of signing |
| `expires_utc` | optional expiry date (UTC ISO-8601) or `null` |
| `confirmation` / `signed_by` | human-readable confirmation + signer |
