"""Durable per-held-message reminder ledger — bounds held re-notification.

A cross-user ``held`` message is shown as inert metadata ONCE when it lands (``inject`` §gate). If the
receiver misses that single notice, the message would sit unapproved in ``held/`` forever with no
further reminder. ``inject`` re-emits the SAME inert metadata on later turns (never the body — the
withholding gate is unchanged); this ledger is the anti-spam brake so a re-notify does not nag on
every prompt and stops after a bounded number of reminders.

Design mirrors ``replay``'s ``seen/turns`` counter: an append-only marker dir per message id, so the
count is ``len(listdir)`` — no read-modify-write, no locks, multi-session safe. Each marker's mtime
records WHEN that reminder fired, so the newest mtime is the throttle reference. Lives under the
receiver-only maildrop (inherits its ``0700``); never touches ``held/`` itself (strictly non-consuming).
"""

from __future__ import annotations

import os
import secrets
import time

from . import maildir

#: Subdir (within the maildrop) holding one dir per reminded message id.
_REMINDER_SUBDIR = "reminders"

#: Characters allowed in an id path component (uuid4 = hex + ``-``); anything else → ``_`` so a
#: deviant id can never escape the path. Same policy as ``replay._safe_id``.
_SAFE = set("0123456789abcdefABCDEF-")


def _safe_id(msg_id: str) -> str:
    return "".join(c if c in _SAFE else "_" for c in msg_id) or "_"


class ReminderLedger:
    """Counts and time-stamps held-message reminders per address+id (append-only, lock-free)."""

    def __init__(self, root=None):
        self._root = root

    def _msg_dir(self, address: str, msg_id: str, create: bool = False) -> str:
        drop = maildir.maildrop(address, root=self._root)
        path = os.path.join(drop, _REMINDER_SUBDIR, _safe_id(msg_id))
        if create and not os.path.isdir(path):
            os.makedirs(path, mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        return path

    def count(self, address: str, msg_id: str) -> int:
        """Number of reminders already emitted for ``msg_id`` (0 if never reminded)."""
        try:
            return len(os.listdir(self._msg_dir(address, msg_id)))
        except OSError:
            return 0

    def last_ts(self, address: str, msg_id: str):
        """Epoch seconds of the most recent reminder, or ``None`` if never reminded."""
        d = self._msg_dir(address, msg_id)
        try:
            names = os.listdir(d)
        except OSError:
            return None
        newest = None
        for n in names:
            try:
                mt = os.stat(os.path.join(d, n)).st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
        return newest

    def record(self, address: str, msg_id: str, now=None) -> int:
        """Record that a reminder fired for ``msg_id`` at ``now`` (epoch s); return the new count.

        Creates one fresh unguessable-named marker (``token_hex`` sidesteps any concurrent-writer name
        collision) and stamps its mtime to ``now`` so ``last_ts`` is deterministic in tests.
        """
        d = self._msg_dir(address, msg_id, create=True)
        marker = os.path.join(d, secrets.token_hex(8))
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            if now is not None:
                os.utime(marker, (now, now))
        except OSError:
            pass  # a marker write failure must never break delivery; count is best-effort
        return self.count(address, msg_id)

    def should_remind(self, address: str, msg_id: str, now=None,
                      max_reminders: int = None, min_interval_s: int = None) -> bool:
        """True iff ``msg_id`` is under the count cap AND past the throttle window."""
        from . import config
        if max_reminders is None:
            max_reminders = config.HELD_REMINDER_MAX
        if min_interval_s is None:
            min_interval_s = config.HELD_REMINDER_MIN_INTERVAL_S
        if now is None:
            now = time.time()
        if self.count(address, msg_id) >= max_reminders:
            return False
        last = self.last_ts(address, msg_id)
        if last is not None and (now - last) < min_interval_s:
            return False
        return True
