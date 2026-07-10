"""agentixmesh member onboarding CLI (spec §4). Adds an EXISTING user to an EXISTING group and wires
the per-user side. enroll ASSERTS the cross-user substrate exists — it never provisions it (§0/§2).

Every side-effecting boundary is injectable: a ``runner`` (defaults to subprocess.run), ``os.geteuid``,
and ``platform.detect`` — so no test runs real usermod/sudo/groupadd or touches real /srv/mesh.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import grp
import os
import re
import shlex
import stat as _stat
import subprocess
import sys

from . import config, platform

EX_OK = 0
EX_USAGE = 2
EX_REFUSED = 3
EX_SUBSTRATE = 10
EX_MEMBERSHIP = 11
EX_PERUSER = 12
EX_SETTINGS = 13

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
HOOK_SOURCE = "agentixmesh"


def default_runner(cmd, **kw):
    """Thin subprocess.run wrapper (production boundary). Never raises on non-zero (check=False)."""
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kw)


def validate_username(name) -> bool:
    return bool(isinstance(name, str) and USERNAME_RE.fullmatch(name))


def user_in_group(user, group) -> bool:
    """Idempotence check via stdlib (never parse command stderr)."""
    try:
        return user in grp.getgrnam(group).gr_mem
    except KeyError:
        return False


def _protected_hardlinks_on() -> bool:
    try:
        with open("/proc/sys/fs/protected_hardlinks", encoding="ascii") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return False


def _defer_substrate(group) -> None:
    print(
        f"mesh-enroll: cross-user substrate for group {shlex.quote(group)} is not ready. "
        f"Ask your admin to run the substrate setup — see pm_mesh/CROSS-USER-SETUP.md.",
        file=sys.stderr,
    )


def assert_substrate(plat, *, group=None, statter=os.stat) -> int:
    """Assert (never create) the substrate: group exists, shared root exists with root owner + expected
    mode, and (Linux/WSL2) protected_hardlinks=1. Missing -> EX_SUBSTRATE (defer to CROSS-USER-SETUP.md)."""
    group = group or config.MESH_GROUP
    try:
        grp.getgrnam(group)
    except KeyError:
        _defer_substrate(group)
        return EX_SUBSTRATE
    root = plat.cross_user_root()
    try:
        st = statter(root)
    except OSError:
        _defer_substrate(group)
        return EX_SUBSTRATE
    if st.st_uid != 0 or _stat.S_IMODE(st.st_mode) != config.CROSS_USER_ROOT_MODE:
        _defer_substrate(group)
        return EX_SUBSTRATE
    if plat.kind in ("linux", "wsl2") and not _protected_hardlinks_on():
        _defer_substrate(group)
        return EX_SUBSTRATE
    return EX_OK


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-enroll",
        description="Enroll an existing OS user into the agentixmesh cross-user pool.",
    )
    p.add_argument("user", help="existing OS username to enroll")
    p.add_argument("--root", default=None, help="override mesh root (tests/experimental)")
    p.add_argument("--yes", action="store_true", help="confirm non-interactive run (required by an agent)")
    p.add_argument("--revoke", action="store_true", help="undo the per-user wiring this tool installed")
    p.add_argument("--verify", action="store_true", help="read-only diagnostic; no mutation")
    p.add_argument("--out-of-band-message", action="store_true",
                   help="print a copy-pasteable notice to hand the user")
    p.add_argument("--per-user-only", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if not validate_username(args.user):
        print(f"mesh-enroll: invalid username {args.user!r} (must match {USERNAME_RE.pattern})",
              file=sys.stderr)
        return EX_USAGE
    # Read-only verbs: --verify and --out-of-band-message do no mutation; they are safe
    # to run from non-interactive contexts (agents) without --yes.
    if args.verify:
        report = verify(args.user, root=args.root)
        for k, v in report.items():
            print(f"{k}: {v}")
        return EX_OK
    if getattr(args, "out_of_band_message", False):
        plat = platform.detect()
        print(out_of_band_message(args.user, plat=plat))
        return EX_OK
    # Confused-deputy guard (§6/sec-6): a non-interactive invocation MUST pass --yes.
    # The internal per-user re-exec (--per-user-only) is already authorized by the parent.
    if not args.per_user_only and not args.yes and not sys.stdin.isatty():
        print("mesh-enroll: refusing non-interactive run without --yes (human-initiated only)",
              file=sys.stderr)
        return EX_REFUSED
    if args.per_user_only:
        return _per_user_inline(args.user)
    if args.revoke:
        import pwd
        result = revoke_peruser(pwd.getpwnam(args.user).pw_dir)
        for k, v in result.items():
            print(f"{k}: {v}")
        print(f"Note: group membership is NOT removed by --revoke. To finish offboarding, run: "
              f"gpasswd -d {shlex.quote(args.user)} {shlex.quote(config.MESH_GROUP)}")
        return EX_OK
    return enroll(args.user, root=args.root, yes=args.yes)


def enroll(user, *, root=None, yes=False, revoke=False, per_user_only=False,
           runner=None, geteuid=os.geteuid, plat=None, isatty=sys.stdin.isatty) -> int:
    """Orchestrator: substrate (10) → host membership (11) → per-user wiring (12/13).
    Lowest-numbered exit-code precedence (§4.3)."""
    # Confused-deputy guard, durable at the public-API level. Skipped for the internal
    # per-user re-exec (per_user_only), which the parent invocation already authorized.
    if not per_user_only and not yes and not isatty():
        print("mesh-enroll: refusing non-interactive run without --yes (human-initiated only)",
              file=sys.stderr)
        return EX_REFUSED
    # per_user_only: the parent already did substrate+membership; just wire per-user side.
    if per_user_only:
        return _per_user_inline(user)
    plat = plat if plat is not None else platform.detect()
    runner = runner or default_runner
    # 10 — substrate first (most foundational).
    rc = assert_substrate(plat)
    if rc != EX_OK:
        return rc
    # 11 — host membership.
    rc = add_to_group(user, config.MESH_GROUP, runner=runner, geteuid=geteuid)
    if rc != EX_OK:
        return rc
    # 12/13 — per-user wiring (re-exec unless already running AS the target).
    # The per-user child (_per_user_inline) does no substrate work and never reads root,
    # so the mesh root is not threaded to it.
    if geteuid() == _target_uid(user):
        return _per_user_inline(user)
    return run_per_user_phase(user, runner=runner, geteuid=geteuid)


def _target_uid(user) -> int:
    import pwd
    return pwd.getpwnam(user).pw_uid


def _per_user_inline(user) -> int:
    """The per-user wiring, executed AS the target (euid==target or after re-exec)."""
    import pwd
    pkg_parent = _pkg_parent()
    home = pwd.getpwnam(user).pw_dir
    try:
        install_pth(home, pkg_parent)
    except PermissionError:
        return EX_PERUSER
    skill_src = os.path.join(pkg_parent, "skill")
    skill_mode = install_skill(home, skill_src)
    settings_path = os.path.join(home, ".claude", "settings.json")
    from . import settings_merge
    hook_cmd = "/usr/local/bin/mesh-inject"
    rc = settings_merge.merge_hook(settings_path, command=hook_cmd, version=_package_version())
    if rc != EX_OK:
        return rc  # 13
    drop_onboarding_marker(home)
    write_manifest(home, package_version=_package_version(), skill_mode=skill_mode,
                   hook_command=hook_cmd, enrolled_by_uid=os.geteuid())
    return EX_OK


def _package_version() -> str:
    try:
        from . import __version__  # noqa: F401
        return str(__version__)
    except Exception:
        return "0"


LOCK_PATH = "/var/lock/agentixmesh-enroll.lock"


@contextlib.contextmanager
def _host_lock(lock_path=LOCK_PATH):
    """Advisory flock around the non-atomic /etc/group mutation (§11.2). Best-effort open."""
    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError:
        yield  # lock file unavailable -> proceed unlocked rather than block enroll
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def add_to_group(user, group, *, runner=None, geteuid=os.geteuid) -> int:
    """Add an existing user to an existing group (the only root mutation). Idempotent; needs root."""
    runner = runner or default_runner
    if user_in_group(user, group):
        return EX_OK
    if geteuid() != 0:
        print(
            f"mesh-enroll: adding {shlex.quote(user)} to group {shlex.quote(group)} needs root; "
            f"to finish, run: sudo usermod -aG {shlex.quote(group)} {shlex.quote(user)}",
            file=sys.stderr,
        )
        return EX_MEMBERSHIP
    with _host_lock():
        result = runner(["usermod", "-aG", group, user])
    return EX_OK if result.returncode == 0 else EX_MEMBERSHIP


import shutil
import site


def build_reexec_argv(user, pkg_parent, extra_args) -> list[str]:
    """Re-exec the per-user phase AS the target.

    ``env`` must come AFTER ``sudo`` so that sudo's env_reset runs first and then the child
    env is set — placing ``env`` before ``sudo`` causes sudo to strip it via env_reset.
    Verified empirically: ``sudo -n env PYTHONPATH=X python3`` propagates X; ``env
    PYTHONPATH=X sudo -n python3`` does not.

    The positional ``user`` arg is appended after ``--per-user-only`` because the child parser
    requires it (``user`` is a required positional argument, §4.1/parser).
    """
    return (
        ["sudo", "-u", user, "-H", "env", f"PYTHONPATH={pkg_parent}",
         "python3", "-m", "pm_mesh.enroll", "--per-user-only", user]
        + list(extra_args)
    )


def _pkg_parent() -> str:
    """Absolute parent dir of the pm_mesh package (the root-owned checkout)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def package_readable_by(user, pkg_dir, statter=os.stat) -> bool:
    """The package dir must be world-readable+traversable so an enrolled user can import it.
    If not readable by other, the per-user phase cannot even import -> caller defers (EX_PERUSER)."""
    try:
        st = statter(pkg_dir)
    except OSError:
        return False
    return bool(_stat.S_IMODE(st.st_mode) & 0o005)  # other read+execute


def run_per_user_phase(user, *, runner=None, geteuid=os.geteuid, extra_args=None) -> int:
    """Dispatch the per-user phase via sudo re-exec. Propagates the child exit code (§10).
    Returns EX_PERUSER immediately if the package dir is not readable by the target user."""
    runner = runner or default_runner
    pkg_parent = _pkg_parent()
    pkg_dir = os.path.join(pkg_parent, "pm_mesh")
    if not package_readable_by(user, pkg_dir):
        print(
            f"mesh-enroll: package dir {shlex.quote(pkg_dir)} is not readable by {shlex.quote(user)}; "
            f"per-user wiring deferred (make the checkout world-readable).",
            file=sys.stderr,
        )
        return EX_PERUSER
    argv = build_reexec_argv(user, pkg_parent, extra_args or [])
    result = runner(argv)
    return result.returncode


def install_pth(home, pkg_parent, *, getusersitepackages=None) -> str:
    """Write the import .pth AS the target user into their own site-packages (§4.1).
    Uses O_NOFOLLOW; refuses if the site-packages dir is not owned by the current euid."""
    site_dir = (getusersitepackages or site.getusersitepackages)()
    os.makedirs(site_dir, exist_ok=True)
    my_uid = os.geteuid()
    if os.stat(site_dir).st_uid != my_uid:
        raise PermissionError(f"site-packages {site_dir!r} not owned by the enrolling user")
    pth = os.path.join(site_dir, "pm-mesh.pth")
    fd = os.open(pth, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(pkg_parent + "\n")
    return pth


def install_skill(home, skill_src, *, copier=shutil.copytree) -> str:
    """Make the pm-mesh skill available at ~/.claude/skills/pm-mesh.
    Symlinks to the canonical skill/; falls back to a copy if os.symlink raises.
    Returns 'symlink' or 'copy'."""
    skills_dir = os.path.join(home, ".claude", "skills")
    os.makedirs(skills_dir, exist_ok=True)
    dest = os.path.join(skills_dir, "pm-mesh")
    if os.path.islink(dest):
        os.unlink(dest)
    elif os.path.isdir(dest):
        shutil.rmtree(dest)
    try:
        os.symlink(skill_src, dest)
        return "symlink"
    except OSError:
        copier(skill_src, dest)
        return "copy"


import json
from datetime import datetime, timezone


def drop_onboarding_marker(home):
    """Create the user-private pending marker (§4.2). Skip (return None) if the done-sentinel exists
    (arch-4: do not re-drop on re-enroll). Content is a boolean trigger only — never read back."""
    if os.path.exists(config.onboarding_done_path(home)):
        return None
    path = config.onboarding_marker_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    return path


def write_manifest(home, *, package_version, skill_mode, hook_command, enrolled_by_uid, now=None) -> str:
    """Write the per-user audit/drift manifest (§11.3)."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = config.enroll_manifest_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "package_version": package_version,
        "skill_mode": skill_mode,
        "hook_command": hook_command,
        "enrolled_by_uid": enrolled_by_uid,
        "ts_utc": ts,
    }
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    return path


def group_effective(gid, getgroups=os.getgroups) -> bool:
    """§11.1: the supplementary-gid set is fixed at login; usermod -aG does NOT affect a running session.
    True only when the gid is in the CURRENT process's group set."""
    try:
        return gid in getgroups()
    except OSError:
        return False


def root_writable_as_self(root, opener=os.open) -> bool:
    """True if the shared root is writable AS the current process (probe, best-effort).
    NOT for verify() — verify() is read-only; this probe mutates (create+unlink) and is for the caller/CLI layer only."""
    probe = os.path.join(root, f".wtest-{os.getpid()}")
    try:
        fd = opener(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def verify(user, *, root=None, geteuid=os.geteuid, plat=None, getgroups=os.getgroups) -> dict:
    """Read-only diagnostic (§11.4). No mutation. Home of the §11.1 activation probe."""
    # reserved: caller-layer euid vs declared-user cross-check
    plat = plat if plat is not None else platform.detect()
    try:
        g = grp.getgrnam(config.MESH_GROUP)
        member = user in g.gr_mem
        gid = g.gr_gid
    except KeyError:
        member, gid = False, None
    return {
        "group_membership": member,
        "group_effective": group_effective(gid, getgroups) if gid is not None else False,
        "substrate_ok": assert_substrate(plat) == EX_OK,
        "cross_user_root": root if root is not None else plat.cross_user_root(),
    }


def out_of_band_message(user, *, plat=None) -> str:
    """A copy-pasteable notice the admin hands the user through their normal channel (§11.5)."""
    if not validate_username(user):
        raise ValueError(f"invalid username: {user!r}")
    plat = plat if plat is not None else platform.detect()
    lines = [
        f"You're enrolled in agentixmesh, {user}.",
        "To activate it, start a NEW login session (a fresh Claude session inside your current",
        "login is not enough — the group membership takes effect only at login).",
    ]
    if plat.kind == "wsl2":
        lines.append("On WSL2 you must run `wsl.exe --shutdown` (from Windows) and reopen — WARNING: "
                     "this terminates the ENTIRE distro (all shells/processes), not just your shell.")
    lines.append("Your mesh address will be <your-uid>:<project> (project = your session's directory).")
    lines.append("Docs: the pm-mesh skill explains the protocol.")
    return "\n".join(lines)


def revoke_peruser(home, *, settings_path=None, hook_source=HOOK_SOURCE, expected_command=None) -> dict:
    """Inverse of the per-user wiring (§4.1/§11.3): remove the hook (by source), the skill symlink OR
    copy, and the markers. A user-modified hook command is REPORTED, not silently skipped (sec r2-5).
    Idempotent: revoking twice is safe; absent items are reported as absent, not as errors.
    Does NOT touch OS group membership, /srv/mesh, or host substrate (use mesh revoke / f2-12 for that)."""
    from . import settings_merge
    settings_path = settings_path or os.path.join(home, ".claude", "settings.json")
    hook = settings_merge.remove_hook(settings_path, source=hook_source,
                                      expected_command=expected_command)
    # skill: remove whichever was installed (symlink OR copy).
    dest = os.path.join(home, ".claude", "skills", "pm-mesh")
    skill = "absent"
    if os.path.islink(dest):
        os.unlink(dest)
        skill = "removed-symlink"
    elif os.path.isdir(dest):
        shutil.rmtree(dest)
        skill = "removed-copy"
    # markers: pending marker, done sentinel, enroll manifest — best-effort, idempotent.
    removed_markers = []
    for path in (config.onboarding_marker_path(home), config.onboarding_done_path(home),
                 config.enroll_manifest_path(home)):
        try:
            os.unlink(path)
            removed_markers.append(path)
        except OSError:
            pass
    return {"hook": hook, "skill": skill, "markers": removed_markers}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
