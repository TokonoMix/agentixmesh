"""Presence/heartbeat per session (phase 2, f2-06) — design §6.

Every active session writes **per inject turn** a heartbeat file
``{user, project, cwd, pid, started, last_seen}`` in ``<mesh-root>/presence/``. A session whose
``last_seen`` is older than a threshold counts as **offline** (``is_online`` — pure, deterministically
testable with an injectable ``now``). Heartbeat timing metadata is an **accepted leak** (design
Q2). The file is owned by the session uid.

**Fail-open**: any error in this layer must never break inject delivery (the caller catches it).
Discovery over these files (group members only) is f2-07.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from . import config

#: Subdirectory under the mesh root with the per-session heartbeats.
PRESENCE_SUBDIR = "presence"

#: Default offline threshold in seconds (~2x a generous turn interval). Adjustable per ``is_online`` call.
DEFAULT_MAX_AGE_S = 600

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_ISO_FMT)


def _iso_to_epoch(s: str) -> float:
    return datetime.strptime(s, _ISO_FMT).replace(tzinfo=timezone.utc).timestamp()


def presence_dir(root=None) -> str:
    """Path to the ``presence/`` dir; create it with appropriate perms (idempotent).

    Cross-user: group-/other-readable (``0o2775`` + group ``mesh`` best-effort) so group members
    can read each other's heartbeat (timing metadata is an accepted leak). Same-user: ``0700``.
    """
    base = root if root is not None else config.mesh_root()
    path = os.path.join(base, PRESENCE_SUBDIR)
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    if config.cross_user_enabled():
        try:
            os.chmod(path, 0o2775)
        except OSError:
            pass
        try:
            from . import maildir

            os.chown(path, -1, maildir._mesh_gid())
        except Exception:  # _mesh_gid can raise MaildropError; best-effort, never break
            pass
    else:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _heartbeat_path(directory: str, pid: int) -> str:
    # One heartbeat per process (pid is host-globally unique while the process lives).
    return os.path.join(directory, f"{pid}.json")


def heartbeat(root=None, now=None) -> str:
    """Write/refresh the heartbeat file of the current session; return the path.

    ``started`` is preserved across turns (read from an existing file); ``last_seen`` = now.
    Atomic (temp + ``os.replace``). ``now`` (ISO string) injectable for tests.
    """
    ts = now if now is not None else _utc_now_iso()
    base = root if root is not None else config.mesh_root()
    directory = presence_dir(base)
    pid = os.getpid()
    path = _heartbeat_path(directory, pid)

    uid, project = config.parse_address(config.current_address())

    started = ts
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
        if isinstance(prev, dict) and isinstance(prev.get("started"), str):
            started = prev["started"]
    except (OSError, ValueError):
        pass  # no/unreadable previous file -> started = now

    record = {
        "user": uid,
        "project": project,
        "cwd": os.getcwd(),
        "pid": pid,
        "started": started,
        "last_seen": ts,
    }
    data = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    file_mode = 0o644 if config.cross_user_enabled() else 0o600

    fd, tmp = tempfile.mkstemp(prefix=".hb-", dir=directory)
    try:
        os.fchmod(fd, file_mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


#: TTL for heartbeat GC (fallback). A heartbeat with ``last_seen`` older than this gets cleaned up
#: regardless — even if its pid happens to be alive again (through reuse), or if the record is
#: corrupt. A **dead** pid is cleaned up immediately, regardless of age. Set generously (24h) so
#: an idle-but-alive session doesn't prematurely disappear from ``who``.
PRUNE_TTL_S = 86400


def _pid_alive(pid) -> bool:
    """``True`` if process ``pid`` (probably) is alive. ``signal 0`` touches nothing: ``ESRCH`` ->
    dead; ``EPERM`` (process of a different user) -> alive. When in doubt -> alive (never clean up
    on uncertainty; the TTL fallback catches genuinely-old records)."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return True


def prune_stale(root=None, now: float = None, max_age_s: int = PRUNE_TTL_S) -> int:
    """Clean up heartbeat files that are **no longer needed**; return the number removed.

    Remove a heartbeat if (a) its ``pid`` is no longer alive (the session is gone) **or** (b)
    ``last_seen`` is older than ``max_age_s`` (fallback against pid reuse/corruption). A **live,
    fresh** session — including the current one, which just wrote its heartbeat — stays put.

    **Best-effort + fail-open** (like this whole layer): an unreadable/unremovable file (e.g. from
    another user in cross-user) is skipped, never a crash. Meant to run every inject turn (the
    janitor), so ``presence/`` doesn't fill up unboundedly with dead sessions."""
    nowf = now if now is not None else datetime.now(timezone.utc).timestamp()
    try:
        directory = presence_dir(root)
        names = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue  # only heartbeat records; skip temp/.hb- and stray files
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            rec = None  # corrupt/unreadable -> no longer needed -> clean up
        stale = True
        if isinstance(rec, dict):
            alive = _pid_alive(rec.get("pid")) if rec.get("pid") is not None else False
            fresh = False
            last = rec.get("last_seen")
            if isinstance(last, str):
                try:
                    fresh = (nowf - _iso_to_epoch(last)) <= max_age_s
                except (ValueError, TypeError):
                    fresh = False
            stale = not (alive and fresh)
        if stale:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass  # not ours / already gone -> skip (best-effort)
    return removed


def is_online(heartbeat_record, now: float, max_age_s: int = DEFAULT_MAX_AGE_S) -> bool:
    """``True`` if ``last_seen`` is <= ``max_age_s`` ago relative to ``now`` (epoch seconds).

    Pure function. A missing/unparseable ``last_seen`` -> offline (fail-closed for presence).
    """
    last = heartbeat_record.get("last_seen") if isinstance(heartbeat_record, dict) else None
    if not isinstance(last, str):
        return False
    try:
        epoch = _iso_to_epoch(last)
    except (ValueError, TypeError):
        return False
    return (now - epoch) <= max_age_s
