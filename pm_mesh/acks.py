"""Explicit ack markers — stop AUTO re-notify for a message the human confirmed seeing.

AUTO re-notify (``inject._renotify_recent_auto``) re-surfaces a compact metadata reminder for a
recently-consumed same-uid AUTO message a bounded number of times. An explicit ``mesh ack <id>``
records that the human saw it, so re-notify skips it early — the human is the only actor who can
truly confirm presence (design §3.5).

**Dedicated ``<maildrop>/acks/`` subdir — NOT a ``cur/<file>.acked`` dotted sidecar.** A dotted file
in ``cur/`` that is not ``.shown`` is moved back to ``new/`` by the janitor ``maildir.recover_stale``
after 900s, where the next ``consume_new`` quarantines it to ``held/`` → the ack is undone (re-notify
RESUMES on the silenced message) + ``held/`` litter. This is the confirmed sibling-bug class from the
mesh-poll ``.relayed`` sidecar. ``acks/`` is a sibling of ``new/``/``cur/``/``held/`` and — like
``receipts/``/``reminders/`` — is deliberately NOT one of ``maildir._SUBDIRS``, so the janitor and the
``new/`` consume loop never touch it.

Perms mirror ``receipt``/``reminder``: the ``acks/`` dir is created ``0700`` (setgid-stripped) and each
marker via ``os.open(..., O_CREAT|O_EXCL|O_WRONLY, 0o600)``. Fail-open: any OS error degrades to
best-effort — this module never raises out to its callers (``inject``/the CLI), which must never break
delivery.
"""

from __future__ import annotations

import os

from . import maildir

#: Subdir with ack markers, sibling to new/cur/held/seen under a maildrop.
_ACKS_SUBDIR = "acks"

#: Characters allowed in the marker filename (a message id = uuid4 hex + ``-``). Anything else is
#: replaced so a hostile/foreign id can never escape the acks/ dir or forge a path.
_SAFE = set("0123456789abcdefABCDEF-")


def _safe_id(msg_id: str) -> str:
    """Sanitise a message id for use as a flat marker filename (no separators/escapes)."""
    return "".join(c if c in _SAFE else "_" for c in msg_id) or "_"


def _acks_dir(address: str, root=None) -> str:
    """Path to ``address``'s acks dir; create it ``0700`` (setgid-stripped) if absent.

    Mirrors ``receipt._receipts_dir``: the chmod only runs at creation time (idempotent calls
    afterwards are a plain stat), matching the pattern used throughout the maildrop layer.
    """
    drop = maildir.maildrop(address, root=root)
    path = os.path.join(drop, _ACKS_SUBDIR)
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    return path


def ack(address: str, msg_id: str, root=None) -> None:
    """Record an ack marker for ``msg_id`` (atomic, idempotent). Best-effort, never raises."""
    try:
        d = _acks_dir(address, root=root)
        marker = os.path.join(d, _safe_id(str(msg_id)))
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return  # already acked → idempotent no-op
    except OSError:
        return  # fail-open: an ack write failure must never break the caller


def ack_all(address: str, msg_ids, root=None) -> int:
    """Ack each id in ``msg_ids``; return the count attempted. Idempotent per id, never raises."""
    n = 0
    for msg_id in msg_ids:
        ack(address, msg_id, root=root)
        n += 1
    return n


def is_acked(address: str, msg_id: str, root=None) -> bool:
    """True iff an ack marker exists for ``msg_id``. Read-only: does NOT create the acks/ dir."""
    try:
        drop = maildir.maildrop(address, root=root)
        marker = os.path.join(drop, _ACKS_SUBDIR, _safe_id(str(msg_id)))
        return os.path.exists(marker)
    except Exception:
        return False
