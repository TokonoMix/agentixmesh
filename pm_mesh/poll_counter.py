"""Delivery-checks counter for mesh-poll (spec §4.1 / MF-4).

One ``delivery_check`` = one mailbox read by the delivery mechanism (a Claude hook
invocation, or a cron job run). The counter turns the token-free invariant from
faith into evidence (``mesh-poll status`` shows it against "0 LLM calls").

MF-4, verbatim: file at
``${XDG_STATE_HOME:-$HOME/.local/state}/mesh-poll/{mailbox_hash}/delivery_checks``,
mode 0600, atomic read-modify-write (temp + rename). Write failure = log +
**fail-OPEN** (never block delivery on a counter error). The increment path makes NO
LLM call (proven by the token-free harness).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .poll_lock import _state_root, mailbox_hash

_log = logging.getLogger("pm_mesh.poll_counter")


def _counter_path(mailbox_path, state_root: Path | None = None) -> Path:
    root = state_root if state_root is not None else _state_root()
    return Path(root) / mailbox_hash(mailbox_path) / "delivery_checks"


def read(mailbox_path, state_root: Path | None = None) -> int:
    """Current delivery-checks count (0 if never written / unreadable)."""
    try:
        with open(_counter_path(mailbox_path, state_root), "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or "0")
    except (FileNotFoundError, ValueError, OSError):
        return 0


def bump(mailbox_path, state_root: Path | None = None) -> int:
    """Increment the counter atomically and return the new value. Fails OPEN: on any
    write error it logs and returns the best-known value WITHOUT raising, so a
    delivery is never blocked by a counter problem. Makes no LLM call."""
    path = _counter_path(mailbox_path, state_root)
    current = read(mailbox_path, state_root)
    new = current + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(new))
        os.replace(tmp, path)
        return new
    except OSError as exc:  # fail-OPEN
        _log.warning("delivery-checks counter write failed (%s) — continuing", exc)
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[possibly-undefined]
        except (OSError, NameError):
            pass
        return new
