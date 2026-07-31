"""``mesh doctor`` (HARD-01) — **strictly read-only** self-diagnosis of the host wiring.

When a receiver says "I never saw my mesh message", the answer is almost always one of a small,
fixed checklist that used to be walked by hand with ``sudo ls /srv/mesh/...`` / ``head settings.json``:
is the inject-hook wired, is the skill symlink correct, what are MESH_ROOT / MESH_ACL, and are the
mailbox perms sane. This module reports that checklist. It NEVER mutates anything, NEVER raises across
the boundary (every check is individually guarded), and ``main`` always returns ``0``.

Each check is a dict ``{"name", "ok", "detail"}`` where ``ok`` is ``True``/``False`` for a pass/fail
check or ``None`` for a purely informational line (an env value has no pass/fail).
"""

from __future__ import annotations

import json
import os
import stat
import sys

from . import config, presence


def _check(name: str, ok, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def _home(home=None) -> str:
    return home if home is not None else os.path.expanduser("~")


def _inject_hook_check(home: str) -> dict:
    """Is the inject-hook wired in ``~/.claude/settings.json``? Absent/unreadable/malformed => not ok."""
    path = os.path.join(home, ".claude", "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return _check("inject-hook", False, f"{path} not readable — hook not wired")
    # Structural presence is enough for a diagnosis: the wrapper name appears in a hook command.
    wired = "mesh-inject" in text or "pm_mesh.inject" in text
    return _check("inject-hook", wired,
                  "mesh-inject found in settings.json" if wired
                  else f"no mesh-inject/pm_mesh.inject command in {path}")


def _skill_symlink_check(home: str) -> dict:
    """Is ``~/.claude/skills/pm-mesh`` present and resolving to an existing target?"""
    link = os.path.join(home, ".claude", "skills", "pm-mesh")
    if not os.path.lexists(link):
        return _check("skill-symlink", False, f"{link} missing")
    target = os.path.realpath(link)
    ok = os.path.isdir(target)
    detail = f"{link} -> {target}" + ("" if ok else " (target missing)")
    return _check("skill-symlink", ok, detail)


def _mesh_member_uids() -> list:
    """Distinct uids that appear in the address book — the one enumerable list of who is on the mesh.

    Not the shared root: a member cannot list ``/srv/mesh`` (that is deliberate — members drop and
    traverse, they do not enumerate the mesh), so the book is the only source available to a normal
    user. Never raises; an unreadable/absent book yields ``[]``.
    """
    from . import addressbook
    uids = []
    for entry in addressbook.load().entries():
        try:
            uid = int(str(entry.address).split(":", 1)[0])
        except (ValueError, AttributeError, IndexError):
            continue
        if uid not in uids:
            uids.append(uid)
    return uids


def _home_of_uid(uid: int):
    """Home directory of ``uid``, or ``None`` if it cannot be determined."""
    import pwd
    try:
        return pwd.getpwuid(int(uid)).pw_dir
    except (KeyError, ValueError, OSError):
        return None


def _member_skill_checks(self_uid=None) -> list:
    """Does every OTHER mesh member actually have the pm-mesh skill installed?

    The gap this exists for (found 2026-07-22): three colleague accounts had the delivery hook wired
    — they received frames — but no skill at all. A frame therefore met an agent with no operating
    knowledge: not knowing the frame is inert DATA, not knowing ``mesh-send``, not knowing
    ``mesh-whoami``. That had been true for weeks, and nothing reported it, because the
    ``skill-symlink`` check only ever looked at ``$HOME`` — the one home that was always right. A
    self-check cannot find a fleet gap.

    **Unknown is not ok.** A normal user cannot traverse another user's home, so the honest result
    there is ``None`` (unknown) — run it under sudo for a definitive answer. A check that quietly
    degrades to "ok" would be the same silence with a green tick on it.
    """
    out = []
    try:
        if self_uid is None:
            self_uid = os.geteuid()
        uids = _mesh_member_uids()
    except Exception:
        return out
    for uid in uids:
        if uid == self_uid:
            continue  # the existing skill-symlink check covers this one
        try:
            home = _home_of_uid(uid)
            if not home:
                out.append(_check("member-skill", None, f"uid {uid}: unknown (no home directory found)"))
                continue
            link = os.path.join(home, ".claude", "skills", "pm-mesh")
            # Probe traversability FIRST. os.path.lexists swallows PermissionError and returns
            # False, so on a 0700 home this check would report a confident "NO skill" for an
            # account that is perfectly fine — a false alarm, which is how a check gets ignored.
            # Homes are private by default, so this is the COMMON case, not an edge case.
            try:
                os.stat(os.path.join(home, ".claude"))
            except PermissionError:
                out.append(_check("member-skill", None, f"uid {uid}: unknown (no permission — try sudo)"))
                continue
            except OSError:
                pass  # absent .claude is a real answer (no skill), not a permission problem
            present = os.path.lexists(link)
            if not present:
                out.append(_check("member-skill", False,
                                  f"uid {uid}: NO pm-mesh skill at {link} — that agent meets mesh "
                                  f"frames with no operating knowledge"))
            else:
                out.append(_check("member-skill", True, f"uid {uid}: {link}"))
        except Exception as exc:
            out.append(_check("member-skill", None, f"uid {uid}: unknown ({exc!r})"))
    return out


def _mailbox_perms_check() -> dict:
    """Report the own maildrop dir mode (informational — cross-user vs same-user modes differ)."""
    try:
        address = presence.resolve_own_address()[0]
        drop = os.path.join(config.mesh_root(), address)
        if not os.path.isdir(drop):
            return _check("mailbox-perms", None, f"{drop} not created yet")
        mode = stat.S_IMODE(os.stat(drop).st_mode)
        return _check("mailbox-perms", None, f"{drop} mode {oct(mode)}")
    except Exception as exc:  # never let a stat failure crash the doctor
        return _check("mailbox-perms", None, f"unavailable: {exc!r}")


def _repair_command(addr: str, path: str, addr_uid: int) -> str:
    """The exact root repair recipe for a mis-owned dropbox (printed, NEVER auto-run — chown needs root)."""
    import pwd

    try:
        user = pwd.getpwuid(addr_uid).pw_name
    except (KeyError, OverflowError):
        user = str(addr_uid)  # unknown uid → still emit a usable (uid-based) command
    return (
        f"Repair (root): sudo chown -R {user}:{config.MESH_GROUP} {path} "
        f"&& sudo chmod 2710 {path} && sudo chmod 3730 {path}/new "
        f"&& sudo chmod 700 {path}/cur {path}/held"
    )


def _dropbox_owner_checks() -> list[dict]:
    """Flag any cross-user dropbox whose owner ≠ the uid in its own address (the mis-owned-dropbox symptom).

    Detection only — a ``chown`` needs root, so a mismatch prints the exact operator repair command
    rather than mutating anything. The shared root ``/srv/mesh`` is intentionally not group-listable
    (``3730``, no group-read), so a member usually cannot enumerate every address; when the root
    cannot be listed we scope the check to the CURRENT address's own dropbox (which still catches the
    receiver's own broken inbox — the case that actually blocks delivery) and note the limitation.
    Fail-open: any error degrades to an informational note, never a raise.
    """
    if not config.cross_user_enabled():
        return []  # same-user (phase 1) has no shared-ownership dropbox to mis-own
    root = config.mesh_root()
    addresses: set[str] = set()
    scoped_note = None
    try:
        for name in os.listdir(root):
            try:
                config.parse_address(name)
            except ValueError:
                continue  # not an address dir
            addresses.add(name)
    except OSError:
        # Shared root not listable (by design: 3730, no group-read) → scope to our own dropbox.
        scoped_note = _check(
            "dropbox-ownership", None,
            f"{root} not enumerable (no group-read on the shared root) — checked only the current "
            f"address's own dropbox",
        )
    try:
        addresses.add(presence.resolve_own_address()[0])
    except Exception:
        pass

    results: list[dict] = []
    checked = 0
    for addr in sorted(addresses):
        drop = os.path.join(root, addr)
        try:
            if not os.path.isdir(drop):
                continue
            addr_uid, _ = config.parse_address(addr)
            actual_uid = os.lstat(drop).st_uid
        except (OSError, ValueError):
            continue  # unreadable/odd entry → skip; never crash the doctor
        checked += 1
        if actual_uid != addr_uid:
            results.append(_check(
                "dropbox-ownership", False,
                f"{addr}: dropbox owned by uid {actual_uid}, expected {addr_uid} "
                f"(a mis-owned remnant permanently blocks this receiver). "
                + _repair_command(addr, drop, addr_uid),
            ))
    if scoped_note is not None:
        results.append(scoped_note)
    if not results and checked:
        results.append(_check("dropbox-ownership", True,
                              f"{checked} dropbox(es) owned by the correct receiver uid"))
    return results


def diagnose(home=None) -> list[dict]:
    """Run every read-only wiring check and return the list of results (never raises)."""
    home = _home(home)
    checks: list[dict] = []
    for fn in (lambda: _inject_hook_check(home),
               lambda: _skill_symlink_check(home),
               _mailbox_perms_check):
        try:
            checks.append(fn())
        except Exception as exc:  # a broken check degrades to an informational note, never a crash
            checks.append(_check("check-error", None, repr(exc)))
    try:
        checks.extend(_member_skill_checks())
    except Exception as exc:  # fail-open: a fleet check never crashes the doctor
        checks.append(_check("member-skill", None, f"unavailable: {exc!r}"))
    try:
        checks.extend(_dropbox_owner_checks())
    except Exception as exc:  # fail-open: ownership detection never crashes the doctor
        checks.append(_check("dropbox-ownership", None, f"unavailable: {exc!r}"))
    checks.append(_check("mesh-root", None, config.mesh_root()))
    acl_state = "on" if config.acl_enabled() else "off"
    checks.append(_check("mesh-acl", None,
                         f"{acl_state} (MESH_ACL={os.environ.get('MESH_ACL', '')!r})"))
    checks.append(_check("cross-user", None,
                         "on" if config.cross_user_enabled() else "off (same-user phase 1)"))
    return checks


def main(argv=None, home=None) -> int:
    """Console entry: print the read-only diagnosis. Always returns ``0`` (never blocks/fails)."""
    print("mesh doctor — read-only wiring check\n")
    for c in diagnose(home=home):
        if c["ok"] is True:
            mark = "OK  "
        elif c["ok"] is False:
            mark = "FAIL"
        else:
            mark = "--  "
        print(f"[{mark}] {c['name']}: {c['detail']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    from .group_reexec import reexec_under_mesh_group_if_needed
    reexec_under_mesh_group_if_needed("pm_mesh.doctor")
    raise SystemExit(main(sys.argv[1:]))
