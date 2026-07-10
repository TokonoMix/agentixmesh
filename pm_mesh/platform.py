"""Pure platform detection + structural POSIX guard (spec §9). No side effects, no privilege.

The guard keys on STRUCTURE (os.name / hasattr), never on an st_uid value: st_uid == 0 is
legitimate root because enroll's host phase runs as root. Every input is injectable so tests
never read the real host.
"""
from __future__ import annotations

import os
import secrets as _secrets
import sys as _sys

LINUX = "linux"
WSL1 = "wsl1"
WSL2 = "wsl2"
MACOS = "macos"
UNSUPPORTED = "unsupported"

#: Curated ALLOWLIST of known-local POSIX filesystems. Everything else (incl. unknown / fuse.*)
#: is refused — an allowlist fails closed by construction (spec §9.3).
FS_ALLOWLIST = frozenset({"ext4", "ext3", "xfs", "btrfs", "f2fs", "zfs", "tmpfs"})

_LINUX_ROOT = "/srv/mesh"
_MACOS_ROOT = "/Users/Shared/mesh"  # /srv is read-only SIP/APFS System volume on macOS


def posix_structural_ok() -> bool:
    """Structural fail-closed guard. NEVER inspects an st_uid value."""
    return (
        os.name == "posix"
        and hasattr(os, "getuid")
        and hasattr(os, "O_NOFOLLOW")
    )


class Platform:
    def __init__(self, kind: str, posix_ok: bool):
        self.kind = kind
        self.posix_ok = posix_ok

    def cross_user_root(self) -> str:
        return _MACOS_ROOT if self.kind == MACOS else _LINUX_ROOT


def _is_wsl2(osrelease: str, version: str, run_wsl: bool) -> bool:
    rel = (osrelease or "").lower()
    if "wsl2" in rel or "microsoft-standard" in rel or run_wsl:
        return True
    return False


def _is_wsl(osrelease: str, version: str, run_wsl: bool) -> bool:
    rel = (osrelease or "").lower()
    ver = (version or "").lower()
    return (
        run_wsl
        or "microsoft" in rel
        or "-wsl" in rel  # matches -wsl, -wsl2, etc. but not mid-token "swsl"
        or "microsoft" in ver
    )


def detect(platform_str=None, proc_version_reader=None, mountinfo_reader=None,
           uname=None, run_probe=None) -> Platform:
    """Detect the platform variant from injected inputs (all default to real host readers)."""
    plat = platform_str if platform_str is not None else _sys.platform
    read_version = proc_version_reader or _read_proc_version
    read_uname = uname or os.uname
    probe = run_probe or _path_exists

    if plat.startswith("darwin"):
        return Platform(MACOS, posix_structural_ok())
    if not plat.startswith("linux"):
        return Platform(UNSUPPORTED, False)

    osrelease = getattr(read_uname(), "release", "")
    version = read_version()
    run_wsl = probe("/run/WSL")
    if _is_wsl(osrelease, version, run_wsl):
        if _is_wsl2(osrelease, version, run_wsl):
            return Platform(WSL2, posix_structural_ok())
        return Platform(WSL1, False)  # WSL1 VolFs/DrvFs don't honor POSIX uid/setgid -> reject
    return Platform(LINUX, posix_structural_ok())


def _read_proc_version() -> str:
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _path_exists(path: str) -> bool:
    return os.path.exists(path)


def fs_type_allowed(fs_type) -> bool:
    """ALLOWLIST: accept only known-local POSIX filesystems; refuse everything else (fail-closed)."""
    return fs_type in FS_ALLOWLIST


def mesh_root_fs_type(resolved_root, mountinfo_reader=None, realpath=os.path.realpath):
    """Return the fs-type of the mount owning ``resolved_root`` via /proc/self/mountinfo, or None.

    Chooses the longest mount-point prefix owning the resolved root; the fs-type is the field
    immediately after the ' - ' separator. Python has no statvfs.f_type, so we parse mountinfo.
    """
    read = mountinfo_reader or _read_mountinfo
    root = realpath(resolved_root)
    best_len = -1
    best_fs = None
    for line in read().splitlines():
        if " - " not in line:
            continue
        pre, post = line.split(" - ", 1)
        fields = pre.split()
        if len(fields) < 5:
            continue
        mount_point = fields[4]
        fs_type = post.split()[0] if post.split() else None
        if root == mount_point or root.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > best_len:
                best_len = len(mount_point)
                best_fs = fs_type
    return best_fs


def _read_mountinfo() -> str:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def is_case_insensitive(dir_path, opener=None) -> bool:
    """Runtime probe (spec §9.3 r2-11): create a lower-case name, test if its upper-case twin resolves.

    Cannot be inferred from fs-type (APFS/NTFS vary by format choice). Best-effort: any error -> False.
    """
    op = opener or os.open
    tag = _secrets.token_hex(8)
    lower = os.path.join(dir_path, f"cix-{tag}-x")
    upper = os.path.join(dir_path, f"cix-{tag}-X")
    fd = None
    try:
        fd = op(lower, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        return os.path.exists(upper)
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(lower)
        except OSError:
            pass
