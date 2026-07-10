"""Replay/dedup guard (design §18) — prevents an already-processed or too-old message from acting again.

Two independent checks protect against replay:

* **dedup on ``id``** — a message id that's already been processed must not act again. The
  seen-state lives per address under the mesh root as an append-only ``seen/`` dir: one empty file
  per id, created atomically (``O_CREAT|O_EXCL``). That's resistant to multiple concurrent sessions
  (no read-modify-write on one file) and idempotent (a duplicate marking is a no-op).
* **age on ``ts_utc``** — a message older than ``max_age_s`` is rejected regardless, even if
  it's never been seen before (bounds the replay window).

Same-user only: the ``seen/`` dir inherits the ``0700`` of the maildrop; no groups/locks/privilege.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from . import maildir, message

#: Subdir with seen markers within a maildrop.
_SEEN_SUBDIR = "seen"

#: Subdir (within ``seen/``) with the per-thread turn counters. Per thread its own dir with one
#: empty file per shown message id: append-only, atomic-create, multi-session, NO locks — the turn
#: count is simply ``len(listdir)``. Inherits the ``0700`` of the maildrop.
_TURNS_SUBDIR = "turns"

#: Characters we allow in an id filename (uuid4 = hex + ``-``). Other characters are replaced so a
#: deviant id can never escape the path.
_SAFE = set("0123456789abcdefABCDEF-")

#: Allowed forward clock skew. A ``ts_utc`` more than this in the future (negative age) is treated
#: as not-fresh — otherwise a future timestamp would bypass the age floor (``now - ts`` becomes
#: negative and thus always stays ``< max_age_s``). Small legitimate skew (a few minutes) is still
#: tolerated.
MAX_FUTURE_SKEW_S = 300


def _safe_id(msg_id: str) -> str:
    """Make ``msg_id`` safe as a filename (no path separator/escape)."""
    return "".join(c if c in _SAFE else "_" for c in msg_id) or "_"


def _parse_ts(ts_utc: str) -> float:
    """Parse a ``%Y-%m-%dT%H:%M:%SZ`` timestamp to epoch seconds (UTC)."""
    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.timestamp()


class SeenStore:
    """Persists processed message ``id``s per address and decides whether a message is still fresh."""

    def __init__(self, root=None):
        self._root = root

    def _seen_dir(self, address: str) -> str:
        drop = maildir.maildrop(address, root=self._root)
        path = os.path.join(drop, _SEEN_SUBDIR)
        if not os.path.isdir(path):
            os.makedirs(path, mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        return path

    def _is_seen(self, address: str, msg_id: str) -> bool:
        return os.path.exists(os.path.join(self._seen_dir(address), _safe_id(msg_id)))

    def is_seen(self, address: str, msg_id: str) -> bool:
        """Public **dedup-only** check (NO age gate): ``True`` if ``msg_id`` has already been seen.

        Deliberately decoupled from ``is_fresh``: the gate-release path (``inject`` drain) must be
        able to show a deliberately-approved message **regardless of age** — approval IS the
        freshness decision (design §2 inv. 7). ``is_fresh`` (with the age floor) stays unchanged for
        the AUTO path. ``msg_id`` goes through ``_safe_id`` (no path escape).
        """
        return self._is_seen(address, msg_id)

    def mark_seen(self, address: str, msg_id: str) -> None:
        """Record that ``msg_id`` for ``address`` has been processed (idempotent, atomic-create)."""
        path = os.path.join(self._seen_dir(address), _safe_id(msg_id))
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return  # already marked — idempotent
        os.close(fd)

    def _turns_dir(self, address: str, thread: str) -> str:
        path = os.path.join(self._seen_dir(address), _TURNS_SUBDIR, _safe_id(thread))
        if not os.path.isdir(path):
            os.makedirs(path, mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        return path

    def record_turn(self, address: str, thread: str, msg_id: str) -> int:
        """Mark (atomic, append-only) that ``msg_id`` in ``thread`` has been shown; return the new count.

        Idempotent per id (``O_CREAT|O_EXCL`` — a duplicate marking is a no-op), resistant to
        multiple concurrent sessions (no read-modify-write, no locks). The turn count is the
        number of markers in the thread dir.
        """
        d = self._turns_dir(address, thread)
        path = os.path.join(d, _safe_id(msg_id))
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        return len(os.listdir(d))

    def is_fresh(
        self, msg: message.Message, max_age_s: int = 86400, now=None, address=None
    ) -> bool:
        """``False`` if ``msg.id`` has already been seen OR ``ts_utc`` is too old/too far in the
        future; ``True`` otherwise.

        Dedup keys on the VERIFIED receive address (``address``), not on the attacker-controlled
        ``msg.to`` (which is never validated on the receive path). The receive path MUST pass
        ``address`` (= ``config.current_address()``, the maildir where the message was actually
        found); the ``msg.to`` fallback exists only for legacy/test callers where
        ``to == receive-address``.

        A corrupt/unparseable ``ts_utc`` is treated as not-fresh (safe side). A ``ts_utc`` more than
        ``MAX_FUTURE_SKEW_S`` in the future likewise counts as not-fresh (otherwise a negative age
        would bypass the age floor). A missing store counts as empty (never seen) — no crash.
        """
        if now is None:
            now = time.time()
        recv = address if address is not None else msg.to
        if self._is_seen(recv, msg.id):
            return False
        try:
            ts = _parse_ts(msg.ts_utc)
        except (ValueError, TypeError):
            return False  # unreadable timestamp -> don't trust it
        age = now - ts
        if age >= max_age_s:
            return False
        if age < -MAX_FUTURE_SKEW_S:
            return False  # too far in the future -> don't trust it (age-floor bypass)
        return True
