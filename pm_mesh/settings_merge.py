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


def merge_hook(settings_path, *, command, version, source=HOOK_SOURCE) -> int:
    with _target_lock(settings_path):
        obj, ok = _load(settings_path)
        if not ok:
            print("mesh-enroll: settings.json is unparseable (JSONC/malformed); not written. "
                  f"Add manually: a {source} hook running {command}")
            return EX_SETTINGS
        hooks = obj.setdefault("hooks", {})
        entry = {"source": source, "version": version, "command": command}
        for event in _HOOK_EVENTS:
            arr = hooks.setdefault(event, [])
            arr[:] = [h for h in arr if not (isinstance(h, dict) and h.get("source") == source)]
            arr.append(dict(entry))
        _atomic_write(settings_path, obj)
        return EX_OK


def remove_hook(settings_path, *, source=HOOK_SOURCE, expected_command=None) -> str:
    with _target_lock(settings_path):
        obj, ok = _load(settings_path)
        if not ok or not isinstance(obj, dict):
            return "absent"
        hooks = obj.get("hooks", {})
        found = False
        modified = False
        # First pass: detect modification in ANY event before touching anything.
        if expected_command is not None:
            for arr in hooks.values():
                for h in arr:
                    if isinstance(h, dict) and h.get("source") == source:
                        found = True
                        if h.get("command") != expected_command:
                            modified = True
        else:
            # Without expected_command, removal is unconditional by design (caller opted out
            # of the user-modification check).
            for arr in hooks.values():
                for h in arr:
                    if isinstance(h, dict) and h.get("source") == source:
                        found = True
        if modified:
            # One or more events have a user-modified command → leave file byte-unchanged.
            return "modified"
        if not found:
            return "absent"
        # Second pass: remove all matching entries (no modification detected).
        for event, arr in list(hooks.items()):
            hooks[event] = [h for h in arr if not (isinstance(h, dict) and h.get("source") == source)]
        _atomic_write(settings_path, obj)
        return "removed"
