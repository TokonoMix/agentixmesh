"""Receiver-only release-spool (Design B, spec §3.3 + §9).

After a human-gate approve, the verified message bytes are staged here in a
receiver-only ``release/`` directory (mode 0700) so the inject hook can drain
and render them without ever re-entering a sender-writable directory.

Security invariants:
- ``release/`` is 0700 receiver-only; no mesh member (sender) can write it.
- Spool entry name is ``secrets.token_hex(16)`` (CSPRNG, ≥128-bit) — never
  derived from any sender-controlled data such as ``msg.id``.
- ``drain`` re-validates the directory via ``lstat`` BEFORE reading any entry
  (symlink reject, owner==receiver, mode==0700). Trusting the stored
  ``owner_uid`` is sound only because this check runs first.
- ``drain`` never raises into its caller (fail-closed for the inject hook).
- Stale ``.taken`` claims (>300 s) are re-processed at the start of each drain
  to prevent silent message loss after a crash mid-drain.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import time

from . import config, identity, message


def _warn_release_drift(rdir: str, why: str) -> None:
    """Emit a one-line stderr notice when ``release/`` exists but fails a security check.

    ``drain`` fail-closes (yields nothing) on a symlink/wrong-owner/wrong-mode ``release/`` — the safe
    direction (a drifted dir could hold entries a non-receiver wrote, so its stored ``owner_uid`` is not
    trustworthy). But a silent return hides a self-inflicted delivery pause from the receiver. This makes
    it visible without weakening fail-closed. Never raises (fail-open on the notice itself)."""
    try:
        sys.stderr.write(
            f"pm-mesh: release-spool {rdir!r} {why} — approved bodies WITHHELD this turn "
            f"(fail-closed); restore it to 0700 receiver-only to resume delivery.\n"
        )
    except Exception:
        pass

#: Sub-directory name inside the maildrop.
_RELEASE_SUBDIR = "release"

#: Staleness threshold for claimed-but-not-discarded entries (seconds).
_STALE_TAKEN_AGE_S = 300

#: Read bound for envelope files: sized for the double-escaped envelope wrapper.
#: A body near MAX_BODY_BYTES with heavy escaping (backslashes/quotes/newlines)
#: can expand ~2x inside the JSON string field, plus the outer envelope overhead.
#: Correctness > tightness here (spool is receiver-only/approve-staged).
_ENVELOPE_READ_LIMIT = 4 * message.MAX_BODY_BYTES + 16 * 1024


def release_dir(address: str, root=None) -> str:
    """Return ``<mesh_root>/<address>/release`` (does not create it)."""
    r = root if root is not None else config.mesh_root()
    return os.path.join(r, address, _RELEASE_SUBDIR)


def _ensure_release_dir(rdir: str) -> None:
    """Ensure ``release/`` exists as a receiver-owned, mode-0700 directory.

    Rejects a symlink / non-directory / wrong-owner (fail-closed → propagates to the caller, which
    keeps ``held/`` intact). **Normalizes a drifted mode back to 0700** so ``stage`` and ``drain``
    enforce the *identical* predicate — otherwise a group-permissive ``release/`` would be written by
    ``stage`` but silently refused by ``drain`` (mode-check), losing an already-approved body and
    leaving the entry group-readable (consensus review 2026-07-01, Opus HIGH-1/2).

    Only ``FileNotFoundError`` triggers creation; any other ``lstat`` error propagates (fail-closed).
    """
    receiver_uid = os.getuid()
    try:
        st = os.lstat(rdir)
    except FileNotFoundError:
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        os.chmod(rdir, 0o700)
        return
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"release/ is a symlink — rejecting (defense-in-depth §9.4): {rdir}")
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"release/ is not a directory: {rdir}")
    if st.st_uid != receiver_uid:
        raise OSError(f"release/ owner mismatch: expected uid {receiver_uid}, got {st.st_uid}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(rdir, 0o700)  # repair drift so drain (which requires 0700) will read this entry


def stage(address: str, owner_uid: int, verified_bytes: bytes, root=None) -> str:
    """Write a release-spool entry for ``address``.

    Ensures ``release/`` exists with mode 0700 owned by the current user (the
    receiver who is calling approve).  Writes the JSON envelope
    ``{"owner_uid": <int>, "msg": <verified message json string>}`` with
    ``O_CREAT|O_EXCL|O_NOFOLLOW`` and mode 0600.

    Returns the full path of the created entry.

    Raises ``FileExistsError`` on a token collision (§9.8 — never retry with
    a predictable token).
    """
    rdir = release_dir(address, root)
    _ensure_release_dir(rdir)

    name = secrets.token_hex(16)  # CSPRNG, ≥128-bit (§9.8)
    entry = os.path.join(rdir, name)

    msg_str = verified_bytes.decode("utf-8")
    envelope = json.dumps({"owner_uid": owner_uid, "msg": msg_str}, ensure_ascii=False)
    data = envelope.encode("utf-8")

    # O_EXCL: raises FileExistsError on collision — NEVER silently overwrite.
    try:
        fd = os.open(entry, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise  # propagate: caller must NOT retry with same/predictable token (§9.8)

    try:
        os.write(fd, data)
    finally:
        os.close(fd)

    return entry


def discard(entry: str) -> None:
    """Best-effort unlink of a claimed spool entry (swallow OSError)."""
    try:
        os.unlink(entry)
    except OSError:
        pass


def drain(address: str, root=None):
    """Generator yielding ``(entry_path, owner_uid, msg)`` for staged entries.

    Re-validates ``release/`` via ``lstat`` before reading anything:
    - Reject symlinks.
    - Require ``st_uid == receiver_uid`` (from ``parse_address``).
    - Require mode exactly ``0700``.
    Any failure or missing directory → yield nothing (fail-closed, §9.4).

    Stale ``.taken`` recovery (§9.2): entries ending ``.taken`` older than
    ``_STALE_TAKEN_AGE_S`` are treated as claimable again BEFORE fresh entries.

    Each entry is claimed via atomic ``os.rename(entry, entry + ".taken")``.
    Malformed/oversized/parse-error entries are discarded+skipped (§9.9).
    """
    rdir = release_dir(address, root)

    # --- Dir re-validation (§9.4) — run BEFORE any entry is read ---
    try:
        receiver_uid, _ = config.parse_address(address)
    except Exception:
        return

    try:
        st = os.lstat(rdir)
    except OSError:
        return  # missing dir → yield nothing

    if stat.S_ISLNK(st.st_mode):
        _warn_release_drift(rdir, "is a symlink"); return  # reject (fail-closed)
    if not stat.S_ISDIR(st.st_mode):
        _warn_release_drift(rdir, "is not a directory"); return
    if st.st_uid != receiver_uid:
        _warn_release_drift(rdir, f"owner uid {st.st_uid} != receiver {receiver_uid}"); return
    if stat.S_IMODE(st.st_mode) != 0o700:
        _warn_release_drift(rdir, f"mode {oct(stat.S_IMODE(st.st_mode))} != 0o700"); return

    # --- Build processing list: stale .taken first, then fresh entries ---
    try:
        entries = os.listdir(rdir)
    except OSError:
        return

    now = time.time()
    stale_taken = []
    fresh = []
    for name in entries:
        if name.endswith(".taken"):
            full = os.path.join(rdir, name)
            try:
                mtime = os.lstat(full).st_mtime
            except OSError:
                continue
            if now - mtime >= _STALE_TAKEN_AGE_S:
                stale_taken.append(full)
        else:
            fresh.append(os.path.join(rdir, name))

    for entry in stale_taken + fresh:
        if entry.endswith(".taken"):
            # Stale-recovery: rename to a FRESH UNIQUE target so exactly one
            # concurrent caller wins (others get ENOENT → continue).
            claimed = os.path.join(
                rdir, os.path.basename(entry)[:-len(".taken")] + "." + secrets.token_hex(8) + ".taken"
            )
        else:
            claimed = entry + ".taken"
        try:
            os.rename(entry, claimed)
        except OSError:
            continue  # another concurrent drain claimed it first (ENOENT or EEXIST)

        # For stale-recovery claims: touch the claimed file so concurrent scanners
        # see a fresh mtime and skip it (avoids double-recovery of the re-named entry).
        if entry.endswith(".taken"):
            try:
                os.utime(claimed, None)
            except OSError:
                pass  # best-effort; failure doesn't break correctness

        # Read and parse the envelope.
        try:
            fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            discard(claimed)
            continue

        try:
            raw = os.read(fd, _ENVELOPE_READ_LIMIT + 1)
        except OSError:
            os.close(fd)
            discard(claimed)
            continue
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        if len(raw) > _ENVELOPE_READ_LIMIT:
            discard(claimed)
            continue

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except Exception:
            discard(claimed)
            continue

        # Strict envelope validation (§9.9)
        try:
            env_owner_uid = envelope["owner_uid"]
            env_msg = envelope["msg"]
        except (KeyError, TypeError):
            discard(claimed)
            continue

        if not isinstance(env_owner_uid, int) or env_owner_uid < 0:
            discard(claimed)
            continue
        if not isinstance(env_msg, str):
            discard(claimed)
            continue

        try:
            msg = message.from_json(env_msg)
        except Exception:
            discard(claimed)
            continue

        yield claimed, env_owner_uid, msg
