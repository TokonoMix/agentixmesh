"""Structured inject-hook merge into a user's ~/.claude/settings.json (spec §4.4/§11.3).

Fail-closed: unparseable/JSONC -> no write, return EX_SETTINGS. Atomic: temp+os.replace, O_NOFOLLOW
temp, per-target flock. Upsert BY source so a package update rewrites its own entry in place.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile

from .enroll import EX_OK, EX_SETTINGS, HOOK_SOURCE

_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit")


@contextlib.contextmanager
def _target_lock(settings_path):
    lock = settings_path + ".lock"
    fd = None
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
            # NOTE: the .lock file is intentionally NOT unlinked here. Unlinking an flock'd
            # file would break mutual exclusion (a racing opener creates a new inode and
            # acquires its own lock, leaving two concurrent holders). Persistent .lock is
            # the standard flock pattern.


def _load(settings_path):
    """Return (obj, ok). Missing/empty -> ({}, True). Unparseable -> (None, False)."""
    try:
        with open(settings_path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}, True
    if not text.strip():
        return {}, True
    try:
        return json.loads(text), True
    except (ValueError, json.JSONDecodeError):
        return None, False


def _atomic_write(settings_path, obj):
    directory = os.path.dirname(settings_path) or "."
    os.makedirs(directory, exist_ok=True)
    # mkstemp creates a unique file (O_EXCL, mode 0600) in the same dir, ensuring
    # same-filesystem atomicity for os.replace and no concurrent overwrite under contention.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        fd = None  # fdopen closed it
        os.replace(tmp, settings_path)
        tmp = None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _entry_commands(h):
    """Every command string in a hook-array element ``h``, across BOTH forms:

    * the valid Claude-Code nested form ``{"hooks": [{"type": "command", "command": ...}]}``, and
    * a legacy bare form ``{"command": ...}`` (what a pre-fix write left behind, or a foreign entry).

    Used for identity matching that survives Claude-Code's settings.json normalization (which strips
    unknown top-level keys like ``source``/``version`` but preserves the nested ``command``).
    """
    if not isinstance(h, dict):
        return []
    out = []
    if isinstance(h.get("command"), str):
        out.append(h["command"])
    nested = h.get("hooks")
    if isinstance(nested, list):
        for sub in nested:
            if isinstance(sub, dict) and isinstance(sub.get("command"), str):
                out.append(sub["command"])
    return out


def _entry_matches(h, *, source=None, command=None, marker=None) -> bool:
    """True iff hook-array element ``h`` is OURS — robust across Claude-Code normalization.

    Claude Code rewrites settings.json and STRIPS unknown top-level keys, so ``h["source"]`` cannot be
    relied on after the first normalization. The durable identity is the **command string** (and any
    ``marker`` embedded in it as an inert trailing shell comment), which normalization preserves. The
    ``source`` key is still honored as a fallback for a not-yet-normalized entry.
    """
    if not isinstance(h, dict):
        return False
    if source is not None and h.get("source") == source:
        return True
    cmds = _entry_commands(h)
    if command is not None and command in cmds:
        return True
    if marker is not None and any(marker in c for c in cmds):
        return True
    return False


def _make_entry(*, source, version, command) -> dict:
    """Build a VALID Claude-Code hook-array element: the ``hooks: [{type, command}]`` nesting is
    mandatory (a bare ``{source, version, command}`` has no ``hooks`` array → the parser rejects the
    WHOLE file with "Expected array, but received undefined" → every setting, incl. permissions/bypass,
    falls away). ``source``/``version`` are kept as extra keys for readability + pre-normalization
    matching; Claude Code strips them on its next rewrite (matching then falls back to the command)."""
    return {
        "source": source,
        "version": version,
        "hooks": [{"type": "command", "command": command}],
    }


def merge_hook(settings_path, *, command, version, source=HOOK_SOURCE) -> int:
    with _target_lock(settings_path):
        obj, ok = _load(settings_path)
        if not ok:
            print("mesh-enroll: settings.json is unparseable (JSONC/malformed); not written. "
                  f"Add manually: a {source} hook running {command}")
            return EX_SETTINGS
        hooks = obj.setdefault("hooks", {})
        for event in _HOOK_EVENTS:
            arr = hooks.setdefault(event, [])
            # Upsert: drop any prior OUR entry (by source OR by our exact command — the latter still
            # matches after Claude Code stripped the source key), then append a fresh valid entry.
            arr[:] = [h for h in arr if not _entry_matches(h, source=source, command=command)]
            arr.append(_make_entry(source=source, version=version, command=command))
        _atomic_write(settings_path, obj)
        return EX_OK


def remove_hook(settings_path, *, source=HOOK_SOURCE, expected_command=None, marker=None) -> str:
    with _target_lock(settings_path):
        obj, ok = _load(settings_path)
        if not ok or not isinstance(obj, dict):
            return "absent"
        hooks = obj.get("hooks", {})
        found = False
        modified = False
        # First pass: detect modification in ANY event before touching anything. Identity is by
        # source OR marker (both survive independently of the command); a found entry whose command
        # no longer contains the expected command is a user-modification → leave the file untouched.
        for arr in hooks.values():
            if not isinstance(arr, list):
                continue
            for h in arr:
                if not _entry_matches(h, source=source, command=expected_command, marker=marker):
                    continue
                found = True
                if expected_command is not None and expected_command not in _entry_commands(h):
                    modified = True
        if modified:
            return "modified"
        if not found:
            return "absent"
        # Second pass: remove all matching entries (no modification detected).
        for event, arr in list(hooks.items()):
            if not isinstance(arr, list):
                continue
            hooks[event] = [
                h for h in arr
                if not _entry_matches(h, source=source, command=expected_command, marker=marker)
            ]
        _atomic_write(settings_path, obj)
        return "removed"
