"""Per-mailbox delivery lock — a reconciled CLAIM, not the source of truth
(spec §8.3 / §4.2 / MF-2, MF-3, MF-11).

One address = one delivery path (MF-3): the lock holds exactly ONE
``{harness, destination, mechanism}`` at a time. ``supported_destinations``
enumerates *switchable* targets, not concurrent channels — a second path needs a
destination-switch or ``--force``.

**The lock is a claim, NOT the source of truth.** ``reconcile`` compares the lock
against the REAL wiring (supplied by the controller as a ``wiring_probe`` callable)
and derives the true state. ``status`` / ``disable`` are reconciliation ops.

Concurrency design (load-bearing — hardened after an independent review and a cross-model
consensus caught a recover-path race):

1. **Strict claim = atomic create-with-content.** ``claim`` writes the FULL record
   to a temp file, then ``os.link``s it onto ``lock.json`` — atomic, and fails with
   ``FileExistsError`` if the lock already exists. Because the content is complete
   before the name becomes visible, a concurrent reader NEVER sees a 0-byte/partial
   lock (closes the corrupt-partial-lock class + the flaky-holder read). Two
   concurrent claimants on a fresh mailbox → exactly one winner + one lock file.

2. **Stale-recovery is a separate, serialized, liveness-checked step**
   (``recover_stale``). It is NOT on the concurrent claim path, so a peer's fresh
   lock is not stolen by the naive "wiring absent ⇒ stale" test. Recoverers
   serialize on an ``O_EXCL`` recovery marker (only one proceeds). The one that
   proceeds re-probes wiring AND checks whether the lock owner's process is still
   ALIVE (``os.kill(pid, 0)`` — process existence, NOT mtime/"ran recently", which
   the spec forbids). The dangerous window — a live peer that claimed but has not
   yet installed wiring — is EXACTLY when that peer's process is still running, so a
   live owner ⇒ refuse recovery (the caller is the loser); a dead owner (or wiring
   present-again) ⇒ genuinely stale ⇒ overwrite. On pid reuse we fail SAFE (refuse
   to clobber; ``--force`` / ``mesh-poll repair`` remains the escape hatch).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .delivery import MailboxAlreadyClaimedError

_log = logging.getLogger("pm_mesh.poll_lock")

# Reconciled state names (mirror poll_types.StateName).
DISABLED = "disabled"
ENABLED = "enabled"
DRIFT_MISSING_WIRING = "drifted_missing_wiring"
DRIFT_MISSING_LOCK = "drifted_missing_lock"
DRIFT_MISMATCH = "drifted_mismatch"

# Fields that must agree for lock and wiring to be "enabled".
_AGREE_FIELDS = ("harness", "destination", "mechanism", "wiring_id")


def mailbox_hash(mailbox_path) -> str:
    """Stable 12-hex identifier for a mailbox path (shared by the lock + the
    delivery-checks counter, MF-2/MF-4). The path is normalised through ``Path``
    FIRST so ``/a/b`` and ``/a/b/`` (which ``str(Path(...))`` collapses) hash
    identically — the module function and ``PollLock`` must never diverge."""
    return hashlib.sha256(str(Path(mailbox_path)).encode("utf-8")).hexdigest()[:12]


def wiring_id(harness: str, destination: str, mailbox_path) -> str:
    """MF-2, verbatim: ``mesh-poll:{harness}:{destination}:{sha256(path)[:12]}``."""
    return f"mesh-poll:{harness}:{destination}:{mailbox_hash(mailbox_path)}"


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / "mesh-poll"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _owner_alive(record: dict) -> bool:
    """Is the lock owner's process still running? Used ONLY to distinguish a live
    peer mid-enable from a genuinely stale lock (see module docstring). Fails SAFE:
    an unknown/foreign/ambiguous owner is treated as ALIVE so recovery refuses to
    clobber. This is process existence, not "ran recently"."""
    pid = record.get("pid")
    owner = record.get("owner_uid")
    if not isinstance(pid, int):
        # No pid recorded → cannot prove death → treat as recoverable ONLY if it is
        # clearly not one of our live processes. Our own claims always record a pid,
        # so a pid-less record is hand-crafted/legacy → treat as dead (recoverable).
        return False
    if owner is not None and owner != os.getuid():
        # Different uid: we cannot signal it; assume alive → fail safe.
        return True
    try:
        os.kill(pid, 0)
        return True  # process exists (and is signalable) → alive
    except ProcessLookupError:
        return False  # ESRCH → dead → genuinely stale
    except PermissionError:
        return True  # EPERM → exists but not ours → fail safe (alive)


class PollLock:
    """Owns the lock file for ONE mailbox."""

    def __init__(self, mailbox_path, state_root: Path | None = None):
        self.mailbox_path = Path(mailbox_path)
        root = state_root if state_root is not None else _state_root()
        self.dir = Path(root) / mailbox_hash(self.mailbox_path)
        self.lock_path = self.dir / "lock.json"
        self._marker_path = self.dir / "lock.recovering"

    # -- helpers ------------------------------------------------------------

    def wiring_id(self, harness: str, destination: str) -> str:
        return wiring_id(harness, destination, self.mailbox_path)

    def _ensure_dir(self) -> None:
        # 0700: the state dir is owner-only (the lock file is 0600; keep the dir
        # private too so a peer cannot even enumerate mailbox hashes).
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def read(self) -> dict | None:
        """Return the lock record, or None if absent/corrupt. Callers that must
        distinguish 'absent' from 'present-but-corrupt' use ``self.lock_path.exists()``."""
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _record(self, harness: str, destination: str, mechanism: str) -> dict:
        return {
            "harness": harness,
            "destination": destination,
            "mechanism": mechanism,
            "wiring_id": self.wiring_id(harness, destination),
            "owner_uid": os.getuid(),
            "pid": os.getpid(),  # liveness signal for stale-recovery (not mtime)
            "ts": _now_iso(),
        }

    def _write_temp(self, record: dict) -> Path:
        """Write ``record`` to a fresh, per-call-unique temp file (0600) in ``self.dir``
        and return its path. Never collides across processes OR threads."""
        self._ensure_dir()
        tmp = self.dir / f".lock.new.{os.getpid()}.{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return tmp

    def _write_atomic(self, record: dict) -> None:
        """Overwrite the lock atomically (temp + os.replace), 0600. Content is fully
        written before it becomes visible under ``lock.json``."""
        tmp = self._write_temp(record)
        try:
            os.replace(tmp, self.lock_path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # -- claim / recover / release -----------------------------------------

    def claim(
        self,
        harness: str,
        destination: str,
        mechanism: str,
        *,
        force: bool = False,
    ) -> dict:
        """Strict atomic claim (the race arbiter, MF-11).

        ``force=False`` (default): write the full record to a temp, then
        ``os.link`` it onto ``lock.json`` — atomic create-with-content. On conflict
        (``FileExistsError``) → raise ``MailboxAlreadyClaimedError`` naming the
        holder. NEVER probes wiring, NEVER recovers. A reader never sees a partial
        lock (the content exists before the name).

        ``force=True``: atomically overwrite an existing lock (destination-switch /
        explicit ``--force`` / stale-recovery).
        """
        record = self._record(harness, destination, mechanism)
        if force:
            self._write_atomic(record)
            return record
        tmp = self._write_temp(record)
        try:
            os.link(tmp, self.lock_path)  # atomic; FileExistsError if lock present
        except FileExistsError:
            holder = self.read() or {}
            raise MailboxAlreadyClaimedError(
                f"mailbox already claimed by "
                f"harness={holder.get('harness')!r} "
                f"destination={holder.get('destination')!r} "
                f"wiring_id={holder.get('wiring_id')!r} "
                f"owner_uid={holder.get('owner_uid')!r}",
                holder=holder,
            )
        finally:
            tmp.unlink(missing_ok=True)
        return record

    def recover_stale(
        self,
        harness: str,
        destination: str,
        mechanism: str,
        *,
        wiring_probe,
    ) -> dict:
        """Stale-lock recovery for the enable path (spec §4.2), race-safe.

        No lock → a plain claim. Otherwise serialize on an ``O_EXCL`` recovery
        marker (only one recoverer proceeds — the rest raise
        ``MailboxAlreadyClaimedError``). Under the marker, re-probe: if wiring is
        now present OR the lock owner's process is still ALIVE → the lock is NOT
        stale (a genuine or in-flight claim) → refuse. Only a dead owner with absent
        wiring (or a corrupt-but-present lock) is overwritten, logged.
        """
        existing = self.read()
        if existing is None and not self.lock_path.exists():
            # Truly no lock → plain strict claim (no recovery semantics).
            return self.claim(harness, destination, mechanism)

        # A lock (valid or corrupt) is present → serialize recovery on the marker.
        self._ensure_dir()
        try:
            mfd = os.open(
                self._marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            os.close(mfd)
        except FileExistsError:
            raise MailboxAlreadyClaimedError(
                "stale-recovery already in progress by another enabler",
                holder=existing or {},
            )
        try:
            existing = self.read()  # re-read under the marker
            corrupt = existing is None and self.lock_path.exists()
            if not corrupt:
                if wiring_probe() is not None:
                    raise MailboxAlreadyClaimedError(
                        "wiring present — lock is genuinely claimed",
                        holder=existing or {},
                    )
                if _owner_alive(existing or {}):
                    raise MailboxAlreadyClaimedError(
                        "lock owner still alive — not stale (peer mid-enable)",
                        holder=existing or {},
                    )
            _log.info(
                "recovering %s lock for mailbox %s (wiring absent, owner not alive)",
                "corrupt" if corrupt else "stale",
                self.mailbox_path,
            )
            return self.claim(harness, destination, mechanism, force=True)
        finally:
            self._marker_path.unlink(missing_ok=True)

    def release(self) -> None:
        """Remove the lock. Idempotent (no lock → no-op). Fails CLOSED: refuses if
        the lock owner is not exactly the caller's uid — a record missing
        ``owner_uid`` is treated as foreign, not free-for-all."""
        existing = self.read()
        if existing is None:
            return
        if existing.get("owner_uid") != os.getuid():
            raise PermissionError(
                f"refusing to release lock owned by uid "
                f"{existing.get('owner_uid')!r} (caller uid {os.getuid()})"
            )
        self.lock_path.unlink(missing_ok=True)

    # -- reconciliation -----------------------------------------------------

    def reconcile(self, wiring_probe) -> str:
        """Derive the true state by comparing the lock against the REAL wiring.

        ``wiring_probe() -> dict | None`` returns the actual wiring descriptor
        (harness/destination/mechanism/wiring_id) or None if absent.
        """
        lock = self.read()
        wiring = wiring_probe()
        # A present-but-corrupt lock file counts as a (broken) lock, not "no lock".
        lock_present = lock is not None or self.lock_path.exists()
        if not isinstance(wiring, dict):
            wiring = None
        if not lock_present and wiring is None:
            return DISABLED
        if lock_present and wiring is None:
            return DRIFT_MISSING_WIRING
        if not lock_present and wiring is not None:
            return DRIFT_MISSING_LOCK
        if lock is None:
            # corrupt lock + wiring present → cannot confirm agreement → drift.
            return DRIFT_MISMATCH
        for field in _AGREE_FIELDS:
            if lock.get(field) != wiring.get(field):
                return DRIFT_MISMATCH
        return ENABLED
