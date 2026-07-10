"""Central audit (phase 2, f2-15) — **read-only monitoring**, append-only JSON lines under
``<mesh-root>/audit/``.

Records mesh activity for the head leader: ``send`` / ``approve`` / ``revoke`` / ``held`` (gate-park)
/ ``block``. Per entry: ``ts_utc``, ``event``, the kernel-verified sender uid, receiver, thread/id,
level — **never body text** (same inert-metadata principle as f2-04). String fields go through
``frame._sanitize_field`` so a crafted thread/from can't inject into the monitoring reader.

> **HONEST LIMITATION (design §14, review):** append-only on a **shared** filesystem is **not
> tamper-proof** — a group member with write access can modify the log. This is **monitoring, not
> accountability**. A tamper-proof trail requires the broker daemon (enforced append-only) —
> deliberately phase 3. Treat this log as *visibility*, not as evidence.

**Best-effort**: a failed audit write never breaks the underlying action (the action is the source
of truth; the log is observation).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import config, frame

#: Subdirectory + file of the central audit log.
AUDIT_SUBDIR = "audit"
AUDIT_FILE = "audit.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_dir(root=None) -> str:
    base = root if root is not None else config.mesh_root()
    return os.path.join(base, AUDIT_SUBDIR)


def audit_path(root=None) -> str:
    return os.path.join(audit_dir(root), AUDIT_FILE)


def _ensure_audit_dir(root=None) -> str:
    """Create ``audit/`` with the appropriate perms (cross-user group-readable for the leader; same-user 0700)."""
    directory = audit_dir(root)
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    if config.cross_user_enabled():
        try:
            os.chmod(directory, 0o2750)  # owner rwx, group r-x (leader/members read), setgid
        except OSError:
            pass
        try:
            from . import maildir

            os.chown(directory, -1, maildir._mesh_gid())
        except Exception:
            pass
    else:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    return directory


def _scrub(value):
    """Make a field both JSON- and monitoring-safe: strings through ``_sanitize_field``, scalars untouched."""
    if isinstance(value, str):
        return frame._sanitize_field(value)
    if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return frame._sanitize_field(str(value))


def append(event: str, root=None, **fields) -> None:
    """Best-effort append one sanitized, body-less JSON line to the audit log. Swallows every error."""
    fields.pop("body", None)  # NEVER body text in the audit (inert-metadata principle, f2-04)
    entry = {"ts_utc": _utc_now_iso(), "event": _scrub(str(event))}
    for key, val in fields.items():
        entry[key] = _scrub(val)
    file_mode = 0o640 if config.cross_user_enabled() else 0o600
    try:
        _ensure_audit_dir(root)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(audit_path(root), os.O_WRONLY | os.O_CREAT | os.O_APPEND, file_mode)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return  # best-effort: the audit must never break the action
