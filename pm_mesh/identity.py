"""Unforgeable sender identity (design §18) — **phase 1: same-user, anti-TOCTOU**.

The sender uid of a message file must NOT come from the path or from the message itself — both
are forgeable. Instead we open the file without following symlinks (``O_NOFOLLOW``) and read the
owner via ``fstat`` on the **opened fd**: this way a path-swap between open and stat (TOCTOU)
cannot change the outcome, because the fd refers to exactly the inode we opened.

Two guards close off the known impersonation tricks:

* **non-regular files** (FIFO/device/socket/dir) are rejected — only ``S_ISREG``;
* **hardlinks** (``st_nlink > 1``) are rejected: a hardlink to someone else's file would
  borrow their owner uid.
"""

from __future__ import annotations

import os
import stat

from . import message

#: Read limit for ``read_verified``: body max plus margin for JSON envelope/overhead. Above this
#: limit we refuse instead of reading unbounded (fd/memory DoS).
_READ_LIMIT = message.MAX_BODY_BYTES + 8 * 1024


class IdentityError(Exception):
    """Identity could not be established unforgeably (symlink/hardlink/non-regular/I-O)."""


def open_verified(path: str) -> tuple[int, int]:
    """Open ``path`` safely and return ``(fd, owner_uid)``; raise ``IdentityError`` on any doubt.

    Steps: ``O_RDONLY|O_NOFOLLOW`` -> ``fstat`` on the fd -> require ``S_ISREG`` and ``st_nlink == 1``.
    The fd remains **open** on success (the caller closes it); on any reject the fd is closed
    cleanly so no fd leaks.
    """
    try:
        # O_NONBLOCK: opening a FIFO read-only would otherwise block until there's a writer —
        # with O_NONBLOCK, open returns immediately and the S_ISREG guard rejects the FIFO. On a
        # regular file, O_NONBLOCK is a no-op.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        # O_NOFOLLOW on a symlink -> ELOOP; missing/inaccessible path -> ENOENT/EACCES.
        raise IdentityError(f"cannot safely open {path!r}: {exc}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IdentityError(f"{path!r} is not a regular file")
        if st.st_nlink > 1:
            raise IdentityError(f"{path!r} has a hardlink (st_nlink={st.st_nlink})")
    except BaseException:
        os.close(fd)
        raise

    return fd, st.st_uid


def read_verified(path: str) -> tuple[bytes, int]:
    """Read ``path`` via ``open_verified`` and return ``(content, owner_uid)``.

    Content is bounded to ``_READ_LIMIT`` bytes; a larger file -> ``IdentityError``
    (no unbounded read). The fd is always closed, even on a read error.
    """
    fd, owner_uid = open_verified(path)
    try:
        # Read one byte more than allowed so we can detect "exactly too large".
        data = _read_all(fd, _READ_LIMIT + 1)
    except OSError as exc:
        raise IdentityError(f"cannot read {path!r}: {exc}") from exc
    finally:
        os.close(fd)
    if len(data) > _READ_LIMIT:
        raise IdentityError(f"{path!r} larger than read limit ({_READ_LIMIT} bytes)")
    return data, owner_uid


def _read_all(fd: int, limit: int) -> bytes:
    """Read up to ``limit`` bytes from ``fd`` (handles short reads)."""
    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
