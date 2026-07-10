"""Root config + address derivation (design §15-§16) — **phase 1: same-user, no privilege**.

The mesh root is an owner-only (``0700``) directory under the user's own account; there are NO
groups, setgid/sticky bits, or shared root (that's phase 2). Addresses have the form
``"<uid>:<project>"`` — identical to the form ``message.validate`` enforces — so the same label
convention runs through the whole stack.
"""

from __future__ import annotations

import os
import re

#: Allowed characters in a project label (the route component of an address).
_PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Address = ``"<uid>:<project>"`` — uid numeric, project a sanitized label. NO ``$`` anchor
#: (would let a trailing ``\n`` through); ``parse_address`` matches with ``fullmatch``.
_ADDRESS_RE = re.compile(r"(\d+):([A-Za-z0-9._-]+)")

#: Advisory turn cap per thread (design §8) — conservative; a normal back-and-forth stays well
#: under this. **Detection aid, NOT enforcement**: exceeding it only logs (stderr), never blocks
#: and never withholds a message.
MAX_TURNS_PER_THREAD = 50

#: Hard upper bound on the number of messages one inject turn shows. Bounds the context-/cost-DoS
#: of a flooded inbox (accidental or a ping loop): the first N appear, the rest stays in ``new/``
#: for the next turn. **DoS backstop, not traffic control**: deliberately set well above the
#: advisory ``MAX_TURNS_PER_THREAD`` so normal bursts are never deferred.
MAX_MESSAGES_PER_TURN = 200

#: Per-sender rate cap on shown ``notify-only`` previews per inject turn (measure D, f2-11,
#: anti-DoS). Deliberately generous; above it a single summary line appears instead of each preview.
NOTIFY_RATE_CAP_PER_TURN = 20

#: Name of the POSIX group that shares cross-user maildrops (phase 2). Members (senders) may drop +
#: traverse but not list/read; only the receiver reads. Provisioning: ``CROSS-USER-SETUP.md``.
MESH_GROUP = "mesh"

#: Shared mesh root that cross-user is automatically derived from (if ``$MESH_CROSS_USER`` is not
#: explicitly set). Same-user remains the default for every other root (byte-identical to phase 1).
CROSS_USER_ROOT = "/srv/mesh"

#: Mode of the shared cross-user root: setgid + sticky + rwx-wx--- (``0o3730``, "self-service"). Group
#: members self-create their own ``<uid>:<project>`` mailbox on the root — that mkdir needs group-write,
#: so the root is group-writable but NOT group-readable (members cannot enumerate the mesh), and the
#: sticky bit blocks a member from deleting/renaming another's mailbox. Delivery re-verifies each drop is
#: owned by the address's uid (``maildir.assert_secure_maildrop``), so a squatted dir is refused rather
#: than honoured — worst case a trusted member DoS's an address, never intercepts it. The substrate-
#: assertion in enroll checks the root against this; the runbook (CROSS-USER-SETUP.md) provisions it.
CROSS_USER_ROOT_MODE = 0o3730

#: Values of ``$MESH_CROSS_USER`` that explicitly turn cross-user on resp. off.
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def mesh_root() -> str:
    """Path to the mesh root; create it owner-only (``0700``) if absent.

    Order: ``$MESH_ROOT`` -> ``$XDG_DATA_HOME/pm-mesh`` -> ``~/.local/share/pm-mesh``.
    Same-user only: the dir is created with mode ``0700`` (umask-independent).
    """
    root = os.environ.get("MESH_ROOT")
    if not root:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            root = os.path.join(xdg, "pm-mesh")
        else:
            root = os.path.join(os.path.expanduser("~"), ".local", "share", "pm-mesh")
    if not os.path.isdir(root):
        os.makedirs(root, mode=0o700, exist_ok=True)
        # makedirs honors umask; force 0700 explicitly (owner-only).
        os.chmod(root, 0o700)
    return root


def cross_user_enabled() -> bool:
    """Determine whether the mesh runs in **cross-user** mode (phase 2) instead of same-user (phase 1).

    Order (fail-closed default = same-user):
    1. ``$MESH_CROSS_USER`` explicitly set -> that value wins (``1/true/yes/on`` => True,
       ``0/false/no/off`` => False).
    2. Otherwise derived from the root: a shared root (``$MESH_ROOT == /srv/mesh``) => cross-user.
    3. Otherwise same-user (owner-only 0700, unchanged phase-1 behavior).
    """
    flag = os.environ.get("MESH_CROSS_USER", "").strip().lower()
    if flag in _TRUE:
        return True
    if flag in _FALSE:
        return False
    return os.environ.get("MESH_ROOT", "") == cross_user_root()


def cross_user_root() -> str:
    """Per-OS shared root, platform-selected (spec §9.3). Linux/WSL2 /srv/mesh; macOS /Users/Shared/mesh."""
    from . import platform  # local import: platform imports nothing heavy, avoids cycles at import time
    return platform.detect().cross_user_root()


def _pm_mesh_data_dir(home=None) -> str:
    base = home if home is not None else os.path.expanduser("~")
    return os.path.join(base, ".local", "share", "pm-mesh")


def onboarding_marker_path(home=None) -> str:
    """Single source for the user-private pending marker (spec §4.2). NEVER under mesh_root()."""
    return os.path.join(_pm_mesh_data_dir(home), "onboarding-pending")


def onboarding_done_path(home=None) -> str:
    return os.path.join(_pm_mesh_data_dir(home), "onboarding-done")


def enroll_manifest_path(home=None) -> str:
    return os.path.join(_pm_mesh_data_dir(home), "enroll-manifest.json")


def acl_enabled() -> bool:
    """Whether the F5 hardening is on: a receiver-only read ACL after ``deliver`` (``$MESH_ACL`` truthy).

    Default off: cross-user drops are then group-readable and confidentiality between co-senders
    rests on the unguessable filename (>=128 bits). On: a POSIX ACL ``u:<receiver>:r`` + no
    group-read makes name-secrecy no longer load-bearing (see ``maildir.deliver``).
    """
    return os.environ.get("MESH_ACL", "").strip().lower() in _TRUE


def _sanitize_project(name: str) -> str:
    """Reduce a directory basename to a valid project label.

    Disallowed characters -> ``_``; an empty or edge form (e.g. basename of ``/``) -> ``"_"``.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return cleaned or "_"


def current_address(cwd: str | None = None) -> str:
    """Derive the own address ``"<uid>:<project>"`` with ``uid = os.getuid()`` and
    ``project`` = sanitized basename of the session working directory.

    ``cwd`` defaults to ``os.getcwd()``. A caller may pass an explicit session directory
    when the process cwd is not the session cwd — e.g. a delivery hook that a harness runs
    from a different directory but reports the session cwd out-of-band (see
    ``inject._effective_cwd``). Passing nothing keeps the original behaviour exactly."""
    uid = os.getuid()
    base = cwd if cwd is not None else os.getcwd()
    project = _sanitize_project(os.path.basename(base))
    return f"{uid}:{project}"


def parse_address(addr: str) -> tuple[int, str]:
    """Split an address into ``(uid:int, project:str)``; raise ``ValueError`` on malformed input."""
    m = _ADDRESS_RE.fullmatch(addr)
    if not m:
        raise ValueError(f"invalid address (expected '<uid>:<project>'): {addr!r}")
    return int(m.group(1)), m.group(2)
