"""``mesh-team-init`` — the machine-checked half of ``CROSS-USER-SETUP.md``.

That runbook is a 163-line manual checklist a human walks with ``sudo`` on the host. This module is
the **read-only** inspection: per provisioning step it reports whether the host is already correct
and, if not, the exact command a human would paste to fix it. It **never mutates anything** — the
``--apply`` half lives in the CLI and may create only the caller's own dropbox.

Each step is a dict in the shape ``pm_mesh.doctor._check`` uses (``{name, ok, detail}``) plus two
fields: ``privileged`` (needs root/sudo — we never run those) and ``remedy`` (the exact paste-able
command). ``ok`` is ``True`` (correct), ``False`` (missing/wrong) or ``None`` (could not be
determined). Fail-closed: a check that cannot run reports ``None`` — never a false ``ok``.
"""

from __future__ import annotations

import grp
import os
import site
import stat

from . import config, doctor, maildir, presence

__all__ = ["own_address", "plan", "resolve_root", "summary"]

#: ``/proc`` entry for the ``fs.protected_hardlinks`` sysctl (module-level so tests can redirect it).
_PROTECTED_HARDLINKS_PATH = "/proc/sys/fs/protected_hardlinks"

#: Directory the thin CLI wrappers are installed into (module-level so tests can redirect it).
_WRAPPER_DIR = "/usr/local/bin"

#: A representative subset of the wrappers whose presence stands in for "the wrappers are installed".
_REQUIRED_WRAPPERS = ("mesh-send", "mesh-inject")


def _step(name: str, ok, detail: str, *, privileged: bool, remedy: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "privileged": privileged, "remedy": remedy}


def resolve_root(root=None) -> str:
    """The shared root to inspect: explicit arg > ``$MESH_ROOT`` > the platform shared default.

    Never calls ``config.mesh_root()`` (which would *create* the directory) — this module writes
    nothing.
    """
    if root is not None:
        return root
    env = os.environ.get("MESH_ROOT")
    if env:
        return env
    return config.cross_user_root()


def own_address(uid=None, root=None) -> str:
    """This session's real address — ``MESH_CWD`` > the session heartbeat > ``basename(cwd)``.

    Deriving it from the cwd basename alone (as this once did) addresses the wrong mailbox for any
    session that carries a path-qualified label: a git worktree and its main checkout share a
    basename, so the second session registers ``<base>--<segment>`` and reads *that* box. Inspecting
    — or worse, creating — ``<base>`` then reports on a mailbox this session never touches.
    """
    resolved = resolve_root(root)
    # ``presence.resolve_own_address`` reaches the heartbeat through ``presence_dir``, which
    # *creates* that directory. This module's whole contract is that inspecting a host changes
    # nothing, so the heartbeat is only consulted when it is already there — and a host with no
    # presence dir has no session record to find anyway.
    if os.path.isdir(os.path.join(resolved, presence.PRESENCE_SUBDIR)):
        addr, _source = presence.resolve_own_address(resolved)
    else:
        env_cwd = (os.environ.get("MESH_CWD") or "").strip()
        addr = config.current_address(env_cwd) if env_cwd else config.current_address()
    if uid is None:
        return addr
    _, project = config.parse_address(addr)
    return f"{uid}:{project}"


def _step_group_mesh_exists() -> dict:
    remedy = f"sudo groupadd --system {config.MESH_GROUP}"
    try:
        grp.getgrnam(config.MESH_GROUP)
        return _step("group_mesh_exists", True, f"group '{config.MESH_GROUP}' exists",
                     privileged=True, remedy=remedy)
    except KeyError:
        return _step("group_mesh_exists", False, f"group '{config.MESH_GROUP}' does not exist",
                     privileged=True, remedy=remedy)
    except OSError as exc:
        return _step("group_mesh_exists", None, f"cannot query the group database: {exc}",
                     privileged=True, remedy=remedy)


def _step_caller_in_group() -> dict:
    remedy = (f"sudo usermod -aG {config.MESH_GROUP} <user>   "
              f"# then log out and back in (a fresh login is required)")
    try:
        gid = grp.getgrnam(config.MESH_GROUP).gr_gid
    except KeyError:
        return _step("caller_in_group", False,
                     f"group '{config.MESH_GROUP}' does not exist yet — create it, then add "
                     f"yourself (a fresh login is needed after 'usermod -aG')",
                     privileged=True, remedy=remedy)
    except OSError as exc:
        return _step("caller_in_group", None, f"cannot query the group database: {exc}",
                     privileged=True, remedy=remedy)
    # getgroups() does not include the primary gid on every platform, so a user whose *primary*
    # group is mesh would otherwise be reported as not a member — a false alarm that sends someone
    # off to run a usermod they do not need.
    if gid in os.getgroups() or gid == os.getgid():
        return _step("caller_in_group", True, f"caller is a member of group '{config.MESH_GROUP}'",
                     privileged=True, remedy=remedy)
    return _step("caller_in_group", False,
                 f"caller is not in group '{config.MESH_GROUP}' — a fresh login is needed after "
                 f"'usermod -aG {config.MESH_GROUP}'",
                 privileged=True, remedy=remedy)


def _step_shared_root_present(root: str) -> dict:
    remedy = (f"sudo mkdir -p {root} && sudo chown root:{config.MESH_GROUP} {root} "
              f"&& sudo chmod {config.CROSS_USER_ROOT_MODE:o} {root}")
    if os.path.islink(root):
        return _step("shared_root_present", False,
                     f"{root} is a symlink — the shared root must be a real directory (refused)",
                     privileged=True, remedy=remedy)
    if not os.path.exists(root):
        return _step("shared_root_present", False, f"{root} does not exist",
                     privileged=True, remedy=remedy)
    if not os.path.isdir(root):
        return _step("shared_root_present", False, f"{root} exists but is not a directory",
                     privileged=True, remedy=remedy)
    return _step("shared_root_present", True, f"{root} is present", privileged=True, remedy=remedy)


def _step_shared_root_mode(root: str) -> dict:
    expected = config.CROSS_USER_ROOT_MODE
    remedy = f"sudo chown root:{config.MESH_GROUP} {root} && sudo chmod {expected:o} {root}"
    if os.path.islink(root) or not os.path.isdir(root):
        return _step("shared_root_mode", None,
                     f"{root} is not present as a real directory — cannot check its mode "
                     f"(expected {expected:o}, owner root, group {config.MESH_GROUP})",
                     privileged=True, remedy=remedy)
    try:
        st = os.stat(root)
    except OSError as exc:
        return _step("shared_root_mode", None, f"cannot stat {root}: {exc}",
                     privileged=True, remedy=remedy)
    mode = stat.S_IMODE(st.st_mode)
    owner_ok = st.st_uid == 0
    try:
        gid = grp.getgrnam(config.MESH_GROUP).gr_gid
        group_known = True
        group_ok = st.st_gid == gid
    except (KeyError, OSError):
        group_known = False
        group_ok = None
    detail = (f"{root}: mode {mode:o} (expected {expected:o}), owner uid {st.st_uid} (expected 0), "
              f"gid {st.st_gid}")
    if not group_known:
        detail += f"; group '{config.MESH_GROUP}' not resolvable"
        ok = None
    elif mode == expected and owner_ok and group_ok:
        ok = True
    else:
        ok = False
    return _step("shared_root_mode", ok, detail, privileged=True, remedy=remedy)


def _step_protected_hardlinks() -> dict:
    remedy = ("sudo sysctl -w fs.protected_hardlinks=1   "
              "# persist it in /etc/sysctl.d/*.conf")
    try:
        with open(_PROTECTED_HARDLINKS_PATH, encoding="utf-8") as fh:
            val = fh.read().strip()
    except OSError:
        return _step("protected_hardlinks", None,
                     f"{_PROTECTED_HARDLINKS_PATH} not readable (non-Linux?) — unknown, not assumed",
                     privileged=True, remedy=remedy)
    ok = val == "1"
    detail = f"fs.protected_hardlinks={val}" + ("" if ok else " (must be 1)")
    return _step("protected_hardlinks", ok, detail, privileged=True, remedy=remedy)


def _step_own_dropbox(root: str, uid) -> dict:
    addr = own_address(uid, root)
    drop = os.path.join(root, addr)
    remedy = (f"open a mesh session in that project dir (it self-provisions), or "
              f"`mesh-team-init --apply` — which creates only your OWN dropbox {addr} "
              f"(a dropbox is created by its receiver, never by a sender)")
    if os.path.islink(drop):
        # The shared-root step refuses a symlink; the dropbox holds the same line. `isdir` follows
        # symlinks, so without this a link into someone else's tree reads as a healthy mailbox.
        return _step("own_dropbox", False,
                     f"{drop} is a symlink — a dropbox must be a real directory (refused)",
                     privileged=False, remedy=remedy)
    if not os.path.isdir(drop):
        return _step("own_dropbox", False,
                     f"{drop} not created yet (your own dropbox — created by you, never a sender)",
                     privileged=False, remedy=remedy)
    # Expected modes mirror what maildir enforces for the active mode (cross-user vs same-user).
    if config.cross_user_enabled():
        expected = {"": maildir.CROSS_USER_DROP_MODE, "new": maildir.CROSS_USER_NEW_MODE,
                    "cur": 0o700, "held": 0o700}
    else:
        expected = {"": 0o700, "new": 0o700, "cur": 0o700, "held": 0o700}
    problems = []
    try:
        st = os.lstat(drop)
        drop_mode = stat.S_IMODE(st.st_mode)
        if drop_mode != expected[""]:
            problems.append(f"dir mode {drop_mode:o} (expected {expected['']:o})")
        # Ownership is the identity spine of the model — a dropbox is owned by its receiver. The
        # maildir layer refuses a mis-owned drop at delivery time, so a checker that looks only at
        # mode bits can report "correct" for a mailbox that will be rejected in practice.
        if st.st_uid != os.getuid():
            problems.append(f"owner uid {st.st_uid} (expected your own {os.getuid()})")
        for sub in ("new", "cur", "held"):
            path = os.path.join(drop, sub)
            if not os.path.isdir(path):
                problems.append(f"missing {sub}/")
            else:
                m = stat.S_IMODE(os.lstat(path).st_mode)
                if m != expected[sub]:
                    problems.append(f"{sub}/ mode {m:o} (expected {expected[sub]:o})")
    except OSError as exc:
        return _step("own_dropbox", None, f"cannot inspect {drop}: {exc}",
                     privileged=False, remedy=remedy)
    if problems:
        return _step("own_dropbox", False, f"{drop}: " + "; ".join(problems),
                     privileged=False, remedy=remedy)
    return _step("own_dropbox", True, f"{drop} present with new/cur/held and the expected modes",
                 privileged=False, remedy=remedy)


def _site_dirs() -> list:
    """Site-package directories a *fresh* interpreter would search. Best-effort, never raises."""
    dirs = []
    for getter in (getattr(site, "getsitepackages", None), getattr(site, "getusersitepackages", None)):
        if getter is None:
            continue
        try:
            found = getter()
        except Exception:
            continue
        dirs.extend([found] if isinstance(found, str) else list(found))
    return [d for d in dirs if d]


def _step_python_path() -> dict:
    """Would a *fresh* interpreter find pm_mesh — not merely this one, which found it by definition.

    The old check reported the directory of its own ``__file__`` and always said yes: true in every
    running process, so it could never fail and never told the adopter anything. What matters is
    whether the package is reachable from a new process (the wrappers spawn one), which means it is
    installed into a site directory or pointed at by a ``.pth`` there.
    """
    remedy = ("install the package (`pip install -e <repo>`), or drop a path file: "
              "echo <repo-abs-path> > <site-packages>/pm-mesh.pth")
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for directory in _site_dirs():
        if os.path.normpath(directory) == os.path.normpath(parent):
            return _step("python_path", True, f"pm_mesh installed in {directory}",
                         privileged=False, remedy=remedy)
        try:
            names = [n for n in os.listdir(directory) if n.endswith(".pth")]
        except OSError:
            continue
        for name in names:
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    lines = [ln.strip() for ln in fh]
            except OSError:
                continue
            if any(os.path.normpath(ln) == os.path.normpath(parent) for ln in lines if ln):
                return _step("python_path", True,
                             f"{os.path.join(directory, name)} points at {parent}",
                             privileged=False, remedy=remedy)
    return _step("python_path", False,
                 f"no site directory installs or points at {parent} — this process imported "
                 f"pm_mesh, but a fresh interpreter (which the wrappers spawn) would not",
                 privileged=False, remedy=remedy)


def _step_wrappers() -> dict:
    remedy = "see INSTALL.md 'CLI wrappers' section (sudo tee <bindir>/mesh-* … exec python3 -m pm_mesh.*)"
    # "present" is not the property that matters — a non-executable file, or a directory with the
    # right name, would satisfy exists() and still fail the moment someone types the command.
    missing, not_executable = [], []
    try:
        for w in _REQUIRED_WRAPPERS:
            path = os.path.join(_WRAPPER_DIR, w)
            if not os.path.isfile(path):
                missing.append(w)
            elif not os.access(path, os.X_OK):
                not_executable.append(w)
    except OSError as exc:
        return _step("wrappers", None, f"cannot inspect {_WRAPPER_DIR}: {exc}",
                     privileged=False, remedy=remedy)
    if missing or not_executable:
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        if not_executable:
            parts.append(f"present but not executable: {', '.join(not_executable)}")
        return _step("wrappers", False, f"{_WRAPPER_DIR} — " + "; ".join(parts),
                     privileged=False, remedy=remedy)
    return _step("wrappers", True, f"wrappers present in {_WRAPPER_DIR}",
                 privileged=False, remedy=remedy)


def _step_skill_symlink(home: str) -> dict:
    d = doctor._skill_symlink_check(home)  # reuse doctor's read-only check
    return _step("skill_symlink", d["ok"], d["detail"], privileged=False,
                 remedy="ln -s <repo>/skill ~/.claude/skills/pm-mesh")


def _step_inject_hook(home: str) -> dict:
    d = doctor._inject_hook_check(home)  # reuse doctor's read-only check
    return _step("inject_hook", d["ok"], d["detail"], privileged=False,
                 remedy="wire SessionStart + UserPromptSubmit hooks to mesh-inject in "
                        "~/.claude/settings.json (see INSTALL.md step 4)")


def plan(root=None, home=None, uid=None) -> list[dict]:
    """Return the ordered provisioning steps for the shared root. **Reads only — never writes.**

    ``root`` defaults to ``$MESH_ROOT`` then the platform shared default; ``home`` to ``~``; ``uid``
    to the caller's own uid (the address whose dropbox is checked).
    """
    root = resolve_root(root)
    home = home if home is not None else os.path.expanduser("~")
    return [
        _step_group_mesh_exists(),
        _step_caller_in_group(),
        _step_shared_root_present(root),
        _step_shared_root_mode(root),
        _step_protected_hardlinks(),
        _step_own_dropbox(root, uid),
        _step_python_path(),
        _step_wrappers(),
        _step_skill_symlink(home),
        _step_inject_hook(home),
    ]


def summary(steps: list[dict]) -> dict:
    """Aggregate: ``{ok, missing, privileged_missing, unprivileged_missing}``.

    A step is "missing" when its ``ok`` is not ``True`` — an unknown (``None``) counts as not-yet-
    satisfied (fail-closed), so ``ok`` is only true when every step is affirmatively correct.
    """
    missing = [s["name"] for s in steps if s["ok"] is not True]
    privileged_missing = [s["name"] for s in steps if s["ok"] is not True and s["privileged"]]
    unprivileged_missing = [s["name"] for s in steps if s["ok"] is not True and not s["privileged"]]
    return {
        "ok": not missing,
        "missing": missing,
        "privileged_missing": privileged_missing,
        "unprivileged_missing": unprivileged_missing,
    }
