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
    uid, base_project = config.parse_address(config.current_address(session_cwd))

    started = ts
    prev = None
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
        if isinstance(prev, dict) and isinstance(prev.get("started"), str):
            started = prev["started"]
    except (OSError, ValueError):
        prev = None  # no/unreadable previous file -> start = now

    # Path-qualified addressing: under a live same-basename collision the session registers a
    # qualified label so replies can target exactly this session. Fail-open: any error in the
    # collision scan keeps the plain base label (previous behaviour).
    try:
        project = _qualified_project(uid, base_project, session_cwd, prev, directory, pid)
    except Exception:
        project = base_project

    record = {
        "user": uid,
        "project": project,
        "project_base": base_project,
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




# --- path-qualified addressing ------------------------------------------------------------
# Two dirs sharing a basename resolve to one label (a main checkout and a worktree named
# alike is the classic case): one mailbox, two sessions, replies read by whichever sibling's
# hook runs first. Under a LIVE collision each session registers "<base>--<qual>", where the
# qualifier is the first path component distinguishing it from every colliding sibling
# (readable and self-describing on purpose); a short path-hash is the last-resort fallback.
# Sticky per session lifetime: a label that flaps mid-conversation would break reply routing
# worse than the ambiguity it solves.


def path_qualifier(cwd, sibling_cwds):
    """The first path component (parent dir upward) distinguishing ``cwd`` from every sibling,
    sanitized to the label charset — or ``None`` when nothing distinguishes (caller falls back
    to a hash)."""
    mine = [c for c in os.path.realpath(cwd).split(os.sep) if c]
    others = [[c for c in os.path.realpath(s).split(os.sep) if c] for s in sibling_cwds]
    for depth in range(2, len(mine) + 1):
        cand = config._sanitize_project(mine[-depth])
        if not cand or cand == "_":
            continue
        clash = False
        for o in others:
            oc = config._sanitize_project(o[-depth]) if depth <= len(o) else None
            if oc == cand:
                clash = True
                break
        if not clash:
            return cand
    return None


def _hash_qualifier(real_cwd, n) -> str:
    import hashlib

    return hashlib.sha256(real_cwd.encode("utf-8", "replace")).hexdigest()[:n]


def _qualified_project(uid, base, session_cwd, prev, directory, my_pid) -> str:
    """The label this session registers under: sticky previous qualification, else
    ``base--<qual>`` under a live collision, else plain ``base``."""
    # Sticky: once qualified in THIS dir, keep the label for the session's lifetime.
    if (
        isinstance(prev, dict)
        and prev.get("project_base") == base
        and isinstance(prev.get("project"), str)
        and prev["project"] != base
        and os.path.realpath(prev.get("cwd") or "") == os.path.realpath(session_cwd)
    ):
        return prev["project"]

    my_real = os.path.realpath(session_cwd)
    sib_cwds: list = []
    sib_labels: set = set()
    for name in os.listdir(directory):
        if not name.endswith(".json") or name.startswith("."):
            continue
        if name == f"{my_pid}.json":
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("user") != uid:
            continue
        if (rec.get("project_base") or rec.get("project")) != base:
            continue
        rcwd = rec.get("cwd")
        if not isinstance(rcwd, str) or not rcwd or os.path.realpath(rcwd) == my_real:
            continue  # same dir on purpose = shared address, not a collision
        if not _pid_alive(rec.get("pid")):
            continue
        sib_cwds.append(rcwd)
        if isinstance(rec.get("project"), str):
            sib_labels.add(rec["project"])
    if not sib_cwds:
        return base

    qual = path_qualifier(my_real, sib_cwds)
    candidates = ([f"{base}--{qual}"] if qual else []) + [
        f"{base}--{_hash_qualifier(my_real, 4)}",
        f"{base}--{_hash_qualifier(my_real, 8)}",
    ]
    for candidate in candidates:
        if candidate not in sib_labels:
            return candidate
    return f"{base}--{_hash_qualifier(my_real, 16)}"


def address_for_path(uid: int, path: str, root=None) -> str:
    """Path addressing: the address of the session living IN ``path`` — its live
    (possibly qualified) registered label when one exists, else the dir's basename label.
    Sender-side convenience only (like aliases): it can never forge identity."""
    real = os.path.realpath(path)
    project = config._sanitize_project(os.path.basename(real))
    try:
        directory = presence_dir(root)
        for name in os.listdir(directory):
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("user") != uid:
                continue
            rcwd = rec.get("cwd")
            if not isinstance(rcwd, str) or not rcwd or os.path.realpath(rcwd) != real:
                continue
            if not _pid_alive(rec.get("pid")):
                continue
            if isinstance(rec.get("project"), str) and rec["project"]:
                return f"{uid}:{rec['project']}"
    except Exception:
        pass
    return f"{uid}:{project}"


def live_base_variants(address, root=None) -> list:
    """Ambiguity signal: ``[(label, cwd)]`` of live sessions claiming ``address`` as
    their BASE label, one entry per distinct real path. Two or more entries = a base-label
    send is ambiguous (which session is meant?). Best-effort: unreadable state → ``[]``."""
    try:
        uid, project = config.parse_address(address)
        directory = presence_dir(root)
        variants: dict = {}
        for name in os.listdir(directory):
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("user") != uid:
                continue
            if (rec.get("project_base") or rec.get("project")) != project:
                continue
            if not _pid_alive(rec.get("pid")):
                continue
            rcwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else ""
            real = os.path.realpath(rcwd) if rcwd else ""
            label = rec.get("project") if isinstance(rec.get("project"), str) else project
            variants.setdefault(real, (f"{uid}:{label}", rcwd))
        return list(variants.values())
    except Exception:
        return []


# --- own-address resolution ----------------------------------------------------------------
# The own address used to be raw ``basename(os.getcwd())`` everywhere. Any uid-shared process
# standing in (or drifted into) another session's project dir then SPOKE AS that session — a
# real incident: a gateway agent sharing the uid answered a colleague under another live
# session's label. The session heartbeat above already registers each session's real address;
# make it the canonical source instead of the shell cwd of the moment.


def session_heartbeat_record(root=None):
    """The presence record written by this process's agent-session ancestor, or ``None``.

    Only trusted when the record's ``user`` equals our uid (pid reuse across users on the
    shared presence dir must never lend us someone else's label). Best-effort: any read or
    walk failure → ``None``."""
    try:
        directory = presence_dir(root)
        path = _heartbeat_path(directory, session_pid())
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        if isinstance(record, dict) and record.get("user") == os.getuid():
            return record
    except Exception:
        return None
    return None


def resolve_own_address(root=None) -> tuple[str, str]:
    """``(address, source)`` — the canonical own ``uid:project`` for send/read/state commands.

    Precedence:

    * ``"env"`` — ``MESH_CWD`` env var: explicit identity (relay/responder/cron discipline).
    * ``"session"`` — the agent-session heartbeat's registered address: immune to shell
      cwd drift (``cd /somewhere/else`` no longer changes who you are).
    * ``"cwd"`` — legacy fallback ``basename(os.getcwd())`` for processes with neither.
    """
    env_cwd = (os.environ.get("MESH_CWD") or "").strip()
    if env_cwd:
        return config.current_address(env_cwd), "env"
    rec = session_heartbeat_record(root)
    if rec is not None and isinstance(rec.get("project"), str) and rec["project"]:
        return f"{os.getuid()}:{rec['project']}", "session"
    return config.current_address(), "cwd"


def _ancestry_pids(limit: int = 64) -> set:
    """The pids of this process's /proc ancestor chain (self included), bounded and cycle-safe.
    pid 1 (init) is everyone's root ancestor and is deliberately EXCLUDED — it is never a
    session."""
    pids = {os.getpid()}
    cur = os.getpid()
    for _ in range(limit):
        info = _read_proc(cur)
        if info is None:
            break
        ppid = info[0]
        if ppid <= 1 or ppid in pids:
            break
        pids.add(ppid)
        cur = ppid
    return pids


def live_foreign_owner(address, root=None):
    """A live presence record registered on ``address`` whose pid lies OUTSIDE this process's
    ancestry, or ``None``. Used by the send-side accident guard: a cwd-derived from-label that
    a live foreign session owns means the sender is about to wear that session's identity.

    Best-effort and fail-open: unreadable state or any internal error → ``None`` (the guard
    must never block a send by accident)."""
    try:
        uid, project = config.parse_address(address)
        directory = presence_dir(root)
        try:
            names = os.listdir(directory)
        except OSError:
            return None
        ancestry = _ancestry_pids()
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("user") != uid or rec.get("project") != project:
                continue
            pid = rec.get("pid")
            if not isinstance(pid, int) or pid in ancestry:
                continue
            if _pid_alive(pid):
                return rec
    except Exception:
        return None
    return None

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
