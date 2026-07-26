"""Fast-mode (snel-modus) per-address state — prompt-free persistence for the quick-reply cadence.

**Why this exists:** an early implementation persisted the fast-mode ladder step by writing
``~/.claude/mesh-fastmode.state`` directly with a raw shell write. That write triggered a
permission prompt every tick; on an unattended run the prompt hung the tick, the next wakeup was
never armed, and the whole loop silently died. Two design faults, both fixed here:

- **Prompt-free:** the state is read/written ONLY via the ``mesh-poll fastmode`` CLI, which is
  allowlisted (``Bash(mesh-poll:*)``), so a tick never hits a permission prompt — no hang, the
  loop survives.
- **Per-address, not one shared file:** state is keyed by the session's ``uid:project`` address
  and lives in the **user-local** pm-mesh data dir, NEVER ``~/.claude`` (the harness config dir,
  which also trips the write-gate). A single shared state file lets concurrent fast-mode sessions
  under the same uid clobber each other's step.

State is advisory session-cadence data (not a trust artifact): a corrupt/missing file fails safe to
``mode=off`` (the token-free default), never raises.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

#: The decaying ladder (seconds): 5 -> 15 -> 30 -> 60 min. Step N (1-based) uses ``LADDER[N-1]``.
LADDER = (300, 900, 1800, 3600)

#: An address filename must be a plain ``uid:project`` token — reject anything with a path separator or
#: other surprising character (defence against a crafted address escaping the fastmode dir).
_ADDR_RE = re.compile(r"[0-9]+:[A-Za-z0-9._-]+")


def _data_dir(home=None) -> str:
    base = home if home is not None else os.path.expanduser("~")
    return os.path.join(base, ".local", "share", "pm-mesh", "fastmode")


def state_path(address: str, home=None) -> str:
    """Per-address state file: ``~/.local/share/pm-mesh/fastmode/<uid:project>.json``. Raises
    ``ValueError`` on a malformed address (never lets a bad address escape the dir)."""
    if not _ADDR_RE.fullmatch(address or ""):
        raise ValueError(f"refusing fastmode state for malformed address {address!r}")
    return os.path.join(_data_dir(home), address + ".json")


def _default(now: float) -> dict:
    return {"mode": "off", "step": 0, "interval_s": 0, "ladder": list(LADDER),
            "activated_at_utc": None, "deactivated_at_utc": None,
            "last_handled_inbound_utc": None, "note": ""}


def load(address: str, *, home=None, now: float = 0.0) -> dict:
    """Return the current fast-mode state, or the token-free default (``mode=off``) if it is
    missing/corrupt/unsafe. Never raises — advisory cadence data, fail-safe to OFF."""
    try:
        path = state_path(address, home)
    except ValueError:
        return _default(now)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return _default(now)
    if not isinstance(data, dict):
        return _default(now)
    return data


def interval_for_step(step: int) -> int:
    """Ladder interval (s) for a 1-based step, clamped to [1, len(LADDER)]. Step <=0 -> 0 (off)."""
    if step <= 0:
        return 0
    return LADDER[min(step, len(LADDER)) - 1]


def save(address: str, *, mode: str, step: int, now: float, note: str = "",
         last_handled_inbound_utc=None, home=None) -> dict:
    """Atomically persist the fast-mode state for ``address`` (tempfile + ``os.replace``). Derives
    ``interval_s`` from ``step`` via the ladder. Returns the written record. Called ONLY by the
    ``mesh-poll fastmode`` CLI (allowlisted -> prompt-free)."""
    path = state_path(address, home)  # raises on malformed address (caught by the CLI)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    on = str(mode).lower() == "on"
    step = int(step) if on else 0
    rec = {
        "mode": "on" if on else "off",
        "step": step,
        "interval_s": interval_for_step(step),
        "ladder": list(LADDER),
        "activated_at_utc": _iso(now) if on else None,
        "deactivated_at_utc": None if on else _iso(now),
        "last_handled_inbound_utc": last_handled_inbound_utc,
        "note": str(note or ""),
    }
    data = (json.dumps(rec, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".fastmode-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return rec


def _iso(now: float) -> str:
    # UTC ISO-8601 without importing datetime.now (tests pass an explicit epoch; the CLI stamps real time).
    import datetime
    return datetime.datetime.fromtimestamp(float(now), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
