"""Presence/heartbeat per session (phase 2, f2-06) — design §6.

Every active session writes a heartbeat file **per inject turn**
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

#: Subdirectory under the mesh root holding the per-session heartbeats.
PRESENCE_SUBDIR = "presence"

#: Default offline threshold in seconds (~2x a generous turn interval). Adjustable per ``is_online`` call.
DEFAULT_MAX_AGE_S = 600

#: Stall threshold (seconds): a live pid whose heartbeat is older than this has stopped taking turns
#: for longer than any plausible tool-loop — it is ``stalled`` (hung on a prompt / trust dialog), not
#: merely ``busy``. Field incident 2026-07-22: five sessions hung ~8h on an unattended dialog and
#: every check read them ``busy``. 30 min is comfortably past a real tool-loop (a test run, an
#: install) yet far below the 24h prune, so a genuine long job is not mislabelled while an
#: hours-long stall is. Overridable per call (an unattended responder may want it tighter).
STALL_S = 1800

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
            os.chmod(path, 0o3775)   # 2775 + sticky (S_ISVTX): only owners rename/delete their heartbeat
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


# --- session pid resolution (bugfix 2026-07-15) -------------------------------------------
# heartbeat() runs inside the short-lived inject-hook SUBPROCESS, whose pid dies the moment the
# hook returns. Recording THAT pid makes every liveness/occupancy check (session_state "busy",
# prune_stale, the unattended-responder stand-down) see a DEAD pid within a second — so a live session goes
# invisible in `mesh-who` and its inbox is wrongly treated as unoccupied (the coordinator's bug
# report + the parked presence stand-down gap). Instead we record the pid of the long-lived AGENT
# SESSION process (the `claude`/`codex`/… process this hook is a descendant of), found by walking
# up the parent chain. Harness-agnostic; falls back to the caller's own pid when no agent ancestor
# is identifiable (unknown harness / no /proc) — never worse than the old behavior.

# Matched EXACTLY on comm (the process NAME), never as a cmdline substring: an intermediate shell
# whose cmdline merely contains "/home/alice/..." (e.g. the shell-snapshot bash) must NOT match.
_AGENT_COMMS = {"claude", "codex", "gemini", "copilot", "cursor", "windsurf"}
# Strong cmdline tokens for harnesses that run under a generic interpreter comm (e.g. node) — these
# are binary identifiers, not incidental path components, so they don't fire on a "/home/alice" path.
_AGENT_CMD_TOKENS = ("claude-code", "anthropic.claude", "codex-cli", "codex exec",
                     "gemini-cli", "@google/gemini", "@github/copilot", "cursor-agent")
_PROC_WALK_MAX = 64


def _read_proc(pid):
    """``(ppid:int, comm:str, cmdline:str)`` for ``pid`` from /proc, or ``None`` if unreadable.

    Parses /proc/<pid>/stat robustly: comm is parenthesised and may itself contain spaces or a
    ``)``, so split on the LAST ``)``; ppid is the 2nd whitespace token after it. cmdline is
    best-effort (NUL-separated argv joined with spaces)."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        rp = raw.rindex(")")
        comm = raw[raw.index("(") + 1:rp]
        ppid = int(raw[rp + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None
    cmdline = ""
    try:
        with open(f"/proc/{int(pid)}/cmdline", encoding="utf-8", errors="replace") as fh:
            cmdline = fh.read().replace("\x00", " ").strip()
    except OSError:
        pass
    return ppid, comm, cmdline


def _is_agent_proc(comm, cmdline) -> bool:
    if (comm or "").strip() in _AGENT_COMMS:
        return True
    c = cmdline or ""
    return any(tok in c for tok in _AGENT_CMD_TOKENS)


def session_pid(start_pid=None, read_proc=_read_proc) -> int:
    """The pid of the nearest agent-session ancestor of ``start_pid`` (default: this process), or
    ``start_pid`` itself when none is identifiable. Bounded and cycle-safe."""
    start = int(start_pid) if start_pid is not None else os.getpid()
    cur, seen = start, set()
    for _ in range(_PROC_WALK_MAX):
        if cur <= 1 or cur in seen:
            break
        seen.add(cur)
        info = read_proc(cur)
        if info is None:
            break
        ppid, comm, cmdline = info
        if _is_agent_proc(comm, cmdline):
            return cur
        cur = ppid
    return start


def heartbeat(root=None, now=None, cwd=None) -> str:
    """Write/refresh the heartbeat file of the current session; return the path.

    ``started`` is preserved across turns (read from an existing file); ``last_seen`` = now.
    Atomic (temp + ``os.replace``). ``now`` (ISO string) injectable for tests. ``cwd`` is the
    SESSION working dir (the hook may run from a different cwd than the session — e.g. a
    background sweep from ``/tmp``); pass it so the heartbeat is tagged under the session's real
    address, not ``basename(os.getcwd())``. Defaults to ``os.getcwd()`` (Claude Code / manual runs
    unchanged).
    """
    ts = now if now is not None else _utc_now_iso()
    base = root if root is not None else config.mesh_root()
    directory = presence_dir(base)
    pid = session_pid()  # the long-lived SESSION pid, not the short-lived hook-subprocess pid
    path = _heartbeat_path(directory, pid)

    session_cwd = cwd if cwd is not None else os.getcwd()
    uid, project = config.parse_address(config.current_address(session_cwd))

    started = ts
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
        if isinstance(prev, dict) and isinstance(prev.get("started"), str):
            started = prev["started"]
    except (OSError, ValueError):
        pass  # no/unreadable previous file -> start = now

    record = {
        "user": uid,
        "project": project,
        "cwd": session_cwd,
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


#: TTL for heartbeat GC (fallback). A heartbeat with ``last_seen`` older than this is cleaned up
#: regardless — even if its pid (through reuse) happens to be alive again, or if the record is corrupt.
#: A **dead** pid is cleaned up immediately, regardless of age. Chosen generously (24h) so that an
#: idle-but-alive session doesn't prematurely disappear from ``who``.
PRUNE_TTL_S = 86400


def _pid_alive(pid) -> bool:
    """``True`` if process ``pid`` is (probably) alive. ``signal 0`` touches nothing: ``ESRCH`` ->
    dead; ``EPERM`` (process of another user) -> alive. When in doubt -> alive (never clean up on
    uncertainty; the TTL fallback catches genuinely stale records)."""
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
    fresh** session — including the current one, which just wrote its heartbeat — stays.

    **Best-effort + fail-open** (like this whole layer): an unreadable/unremovable file (e.g. from
    another user in cross-user) is skipped, never a crash. Meant to run on every inject turn (the
    janitor), so that ``presence/`` doesn't fill up unboundedly with dead sessions."""
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


def session_state(heartbeat_record, now: float,
                  fresh_s: int = DEFAULT_MAX_AGE_S, prune_s: int = PRUNE_TTL_S,
                  stall_s: int = STALL_S) -> str:
    """Classify a session's liveness for display (pure). MR-01 + stall (2026-07-22).

    * ``"online"``  — ``last_seen`` within ``fresh_s`` (a fresh heartbeat; definitely turn-taking).
    * ``"busy"``    — heartbeat older than ``fresh_s`` but within ``stall_s`` AND pid alive: a live
      session in a real tool-loop (a test run, an install) that hasn't re-stamped its heartbeat.
    * ``"stalled"`` — pid alive but the heartbeat is older than ``stall_s`` (yet within ``prune_s``):
      the process is up but has taken no turn for longer than any plausible tool-loop. This is the
      hung-on-a-prompt / trust-dialog case (field incident 2026-07-22, five sessions ~8h). "Process
      alive, taking no turns" — distinct from ``busy`` precisely because a consumer (a waiting peer,
      an unattended responder deciding whether to answer on this session's behalf) must NOT treat it as live.
    * ``"offline"`` — pid dead, ``last_seen`` beyond ``prune_s``, or missing/unparseable (fail-closed).

    pid-liveness is the **primary** signal (same-machine ground truth); heartbeat age splits
    online / busy / stalled. The heartbeat is written only on the inject hook, i.e. once per TURN,
    so "no fresh heartbeat despite a live pid" is exactly "taking no turns" — the older that is, the
    more it is a stall rather than a loop.
    """
    last = heartbeat_record.get("last_seen") if isinstance(heartbeat_record, dict) else None
    if not isinstance(last, str):
        return "offline"
    try:
        age = now - _iso_to_epoch(last)
    except (ValueError, TypeError):
        return "offline"
    if age <= fresh_s:
        return "online"
    pid = heartbeat_record.get("pid")
    # pid-liveness is the primary signal, so a record with NO pid cannot be live — treat missing
    # pid as not-alive (mirrors ``prune_stale``'s guard; ``_pid_alive`` would otherwise fail OPEN on
    # ``int(None)`` and wrongly show a pid-less stale record as live).
    if age <= prune_s and pid is not None and _pid_alive(pid):
        return "busy" if age <= stall_s else "stalled"
    return "offline"
