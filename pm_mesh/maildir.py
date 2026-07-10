"""Maildir layout + atomic delivery (design §17) — **phase 1: same-user, 0700/0600**.

Each address has its own maildrop ``<root>/<address>/`` with ``new/`` (just delivered), ``cur/``
(picked up) and ``held/`` (parked). Delivery is **atomic**: first write to a temp file in the same
dir, then ``os.rename`` — a reader never sees a half-written file. The final name carries >=128
bits of randomness so messages can't be enumerated or guessed.

Same-user only: all dirs ``0700``, all messages ``0600``; no groups/setgid/sticky (phase 2).
"""

from __future__ import annotations

import grp
import os
import secrets
import stat

from . import config, identity, message

#: Subdirectories of a maildrop.
_SUBDIRS = ("new", "cur", "held")


class MaildropError(Exception):
    """The maildrop dir is no longer safe: symlink, different type, wrong owner, or too-loose mode."""

#: Bytes of randomness in a final name — 16 bytes == 128 bits (``token_hex`` -> 32 hex chars).
_NAME_BYTES = 16

#: **Hard lower bound** on the name entropy (F5): a final name must carry >=128 bits of randomness,
#: because with group-readable cross-user drops, confidentiality between co-senders rests on the
#: name being unguessable. ``_final_name`` enforces this; a future lowering breaks a test.
_MIN_NAME_BYTES = 16


def _final_name() -> str:
    """Generate an unguessable final name with >=128 bits of entropy; raise if ``_NAME_BYTES`` is too low."""
    if _NAME_BYTES < _MIN_NAME_BYTES:
        raise ValueError(
            f"name entropy {_NAME_BYTES * 8} bits < required {_MIN_NAME_BYTES * 8} bits (F5)"
        )
    return secrets.token_hex(_NAME_BYTES)

#: Mode of a **cross-user** ``new/`` dropbox (phase 2): owner ``rwx`` (receiver: list+read+delete),
#: group ``-wx`` (senders: drop + traverse, **not** list/read), other nothing — plus **setgid**
#: (``02000``: dropped files inherit group ``mesh``) and **sticky** (``01000``: a sender cannot
#: delete/rename another sender's pending message). Octal ``0o3730`` = ``setgid | sticky | 0o730``.
#: (The ticket text wrote "``1730`` + setgid"; that is exactly this value —
#: ``0o1730 | 0o2000``. The prose mentions setgid twice for a functional reason, so that wins over
#: the accidentally-omitted digit.)
CROSS_USER_NEW_MODE = 0o3730

#: Mode of the cross-user **drop dir** (``<root>/<address>/``): owner ``rwx`` + group ``--x``
#: (traverse, no list) + setgid (children inherit group ``mesh``). The receiver owns it; senders
#: may only pass through it to ``new/``. ``0o2710`` = ``setgid | 0o710``.
CROSS_USER_DROP_MODE = 0o2710


def _resolve_root(root):
    """Use an explicit root, otherwise the owner-only mesh root from ``config``."""
    return root if root is not None else config.mesh_root()


def _resolve_mode(mode):
    """Determine the effective maildrop mode. ``None`` => derived from config (phase-1 default same-user)."""
    if mode is None:
        return "cross_user" if config.cross_user_enabled() else "same_user"
    if mode not in ("same_user", "cross_user"):
        raise ValueError(f"unknown maildrop mode {mode!r} (expected 'same_user' or 'cross_user')")
    return mode


def _mesh_gid() -> int:
    """GID of the shared group ``config.MESH_GROUP``; ``MaildropError`` if that group doesn't exist.

    A missing ``mesh`` group is a provisioning error (see ``CROSS-USER-SETUP.md``): fail-closed
    instead of silently falling back to a wrong group.
    """
    try:
        return grp.getgrnam(config.MESH_GROUP).gr_gid
    except KeyError as exc:
        raise MaildropError(
            f"group {config.MESH_GROUP!r} does not exist — cross-user provisioning is missing"
        ) from exc


def assert_secure_maildrop(drop: str, mode: str = "same_user", expected_uid=None) -> None:
    """Re-validate that ``drop`` is a secure (sub)maildrop dir; raise ``MaildropError`` on drift.

    Structural fail-closed hardening (design §14): with ``os.lstat`` (does NOT follow symlinks)
    check that it's still a **real directory** (no symlink/different type).

    * ``mode="same_user"`` (phase 1, unchanged): owner == current euid AND exact mode ``0700``.
    * ``mode="cross_user"`` (phase 2, on the ``new/`` dropbox): owner == ``expected_uid`` (the
      **intended receiver** — not necessarily the sender currently validating), group == ``mesh``,
      **setgid AND sticky bit set**, perm bits exactly ``0o730`` (no group/other-read, no setuid).
      Anything looser -> ``MaildropError``.

    ``expected_uid`` defaults to ``os.geteuid()`` (same-user, or cross-user when the receiver
    validates themself).
    """
    try:
        st = os.lstat(drop)
    except OSError as exc:
        raise MaildropError(f"cannot stat maildrop {drop!r}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise MaildropError(f"maildrop {drop!r} is a symlink (rejected)")
    if not stat.S_ISDIR(st.st_mode):
        raise MaildropError(f"maildrop {drop!r} is not a directory")

    want_uid = os.geteuid() if expected_uid is None else expected_uid

    if mode == "same_user":
        if st.st_uid != want_uid:
            raise MaildropError(
                f"maildrop {drop!r} is not owned by the intended owner (st_uid={st.st_uid}, want={want_uid})"
            )
        m = stat.S_IMODE(st.st_mode)
        if m != 0o700:
            raise MaildropError(f"maildrop {drop!r} has mode {oct(m)} (expected 0o700)")
        return

    # cross_user: the shared dropbox (``new/``).
    if st.st_uid != want_uid:
        raise MaildropError(
            f"cross-user dropbox {drop!r} is not owned by the receiver (st_uid={st.st_uid}, want={want_uid})"
        )
    gid = _mesh_gid()
    if st.st_gid != gid:
        raise MaildropError(
            f"cross-user dropbox {drop!r} does not belong to group {config.MESH_GROUP!r} "
            f"(st_gid={st.st_gid}, want={gid})"
        )
    if not st.st_mode & stat.S_ISGID:
        raise MaildropError(f"cross-user dropbox {drop!r} is missing the setgid bit")
    if not st.st_mode & stat.S_ISVTX:
        raise MaildropError(f"cross-user dropbox {drop!r} is missing the sticky bit")
    if st.st_mode & stat.S_ISUID:
        raise MaildropError(f"cross-user dropbox {drop!r} has a setuid bit (rejected)")
    perms = stat.S_IMODE(st.st_mode) & 0o777
    if perms != 0o730:
        raise MaildropError(
            f"cross-user dropbox {drop!r} has perm bits {oct(perms)} (expected 0o730, no group/other-read)"
        )


def _ensure_dir(path: str, mode: int, gid=None) -> None:
    """Guarantee a dir at ``path``; set group+mode **only on creation** (never heal).

    Deliberately no healing of an existing dir: just like the same-user path, this doesn't
    silently fix drift but lets ``assert_secure_maildrop`` reject it (fail-closed). A **sender**
    does not own another receiver's pre-provisioned dropbox and must not modify it — that's
    correct (the provisioning, ``CROSS-USER-SETUP.md``, already set the perms); creation then
    fails with ``PermissionError`` which we ignore best-effort, after which the assert is the
    real gate.
    """
    if os.path.isdir(path) or os.path.lexists(path):
        return  # already exists (or is something else, e.g. symlink) -> don't heal; assert decides.
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError:
        return
    if gid is not None:
        try:
            os.chown(path, -1, gid)
        except (PermissionError, OSError):
            pass
    # chmod after makedirs (umask-independent) so the exact mode (incl. setgid/sticky) is guaranteed.
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def maildrop(address: str, root=None, mode=None) -> str:
    """Path to the maildrop of ``address``; create ``new/``/``cur/``/``held/`` (idempotent).

    ``address`` may contain a ``:`` (``"<uid>:<project>"``); on POSIX that's a safe dir name.
    ``mode`` (``None`` = derived from config; explicit ``"same_user"`` / ``"cross_user"``) chooses between:

    * **same_user** (phase 1, default, byte-identical): all dirs ``0700``, owner == euid.
    * **cross_user** (phase 2): ``new/`` is the shared dropbox ``0o3730`` (setgid+sticky+``730``)
      group ``mesh``, owned by the receiver; ``cur/``/``held/`` stay receiver-only ``0700``.

    The shared dropbox is **re-validated every turn** (``assert_secure_maildrop``): drift
    (symlink/wrong owner/group/too-loose mode/missing sticky-or-setgid) -> ``MaildropError``
    before anything is written or changed in it (fail-closed).
    """
    # Validate the address before using it as a path component: `parse_address` enforces the
    # `uid:project` form (project charset without `/`), so a crafted `address` (e.g.
    # `../evil`, `a/b`, `notanaddress`) is rejected here instead of writing outside the mesh root.
    receiver_uid, _ = config.parse_address(address)
    drop = os.path.join(_resolve_root(root), address)

    if _resolve_mode(mode) == "same_user":
        # ---- phase-1 same-user path (unchanged, byte-identical) ----
        if not os.path.lexists(drop):
            os.makedirs(drop, mode=0o700, exist_ok=True)
        assert_secure_maildrop(drop)
        for sub in _SUBDIRS:
            path = os.path.join(drop, sub)
            if not os.path.isdir(path):
                os.makedirs(path, mode=0o700, exist_ok=True)
            # force 0700 (umask-independent) on the subdirectory.
            os.chmod(path, 0o700)
        return drop

    # ---- phase-2 cross-user path ----
    gid = _mesh_gid()
    # Drop dir: owned by receiver, group mesh, group-traverse + setgid (children inherit mesh).
    _ensure_dir(drop, CROSS_USER_DROP_MODE, gid=gid)
    # new/ = shared dropbox (setgid+sticky+730, group mesh).
    new_dir = os.path.join(drop, "new")
    _ensure_dir(new_dir, CROSS_USER_NEW_MODE, gid=gid)
    # cur/ + held/ = receiver-only (0700); senders must not be able to reach these.
    for sub in ("cur", "held"):
        _ensure_dir(os.path.join(drop, sub), 0o700)
    # Fail-closed re-validation of the load-bearing dropbox: ownership by the receiver from the address.
    assert_secure_maildrop(new_dir, mode="cross_user", expected_uid=receiver_uid)
    return drop


def deliver(msg: message.Message, root=None, mode=None) -> str:
    """Write ``msg`` atomically into the ``new/`` of ``msg.to``; return the final path.

    First writes to a temp file in the same dir (so ``os.rename`` on the same filesystem is
    atomic), then renames to an unguessable final name (>=128 bits entropy, ``_final_name``).

    **Same-user** (phase 1, unchanged): file stays ``0600``.

    **Cross-user** (phase 2): the receiver (a different uid) must be able to read the file held
    by the sender uid. Two modes:

    * default: **group-read** (``0640``, group ``mesh`` via setgid on ``new/``) — a co-sender can
      only read the message WITH the (unguessable) name (F5: name-secrecy is then load-bearing).
    * ACL hardening (``config.acl_enabled()``): file stays ``0600`` (NO group-read) + a POSIX ACL
      ``u:<receiver>:r`` so only the receiver reads — name-secrecy no longer load-bearing.
      **Best-effort + fail-closed**: if the ACL fails (no ``setfacl``/no ACL fs) -> fall back to
      group-read (the drop stays functional) AND log it on stderr.
    """
    resolved = _resolve_mode(mode)
    new_dir = os.path.join(maildrop(msg.to, root=root, mode=mode), "new")
    data = message.to_json(msg).encode("utf-8")

    # Temp file in the same dir -> rename stays within one filesystem (atomic).
    fd, tmp_path = _mkstemp_0600(new_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        final_path = os.path.join(new_dir, _final_name())
        os.rename(tmp_path, final_path)
    except BaseException:
        # Clean up the temp file so a failed deliver doesn't leave a remnant in new/.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if resolved == "cross_user":
        _apply_cross_user_read(final_path, msg.to)
    return final_path


def _apply_cross_user_read(path: str, address: str) -> None:
    """Make a cross-user message readable for the receiver (group-read, or receiver-only ACL).

    See ``deliver`` for the two modes. Fail-closed: ACL failure -> group-read fallback + stderr
    log; never a crash, never an other-readable file.
    """
    receiver_uid, _ = config.parse_address(address)
    if not config.acl_enabled():
        # group-read so the receiver (member of mesh) can read; a co-sender only with the name.
        _chmod_best_effort(path, 0o640)
        return
    # ACL hardening: receiver-only read, no group-read.
    # chmod MUST come before setfacl: on Linux, ``chmod`` recomputes the ACL mask from the group
    # permission bits, so a chmod after ``setfacl -m u:<uid>:r`` would set the mask to ``---`` and
    # make the receiver ACL ineffective (``#effective:---``) — the message would then be unreadable
    # for the receiver. chmod 0600 first (owner-only base) and then setfacl, so setfacl recomputes
    # the mask to ``r--`` and the ACL stays effective.
    _chmod_best_effort(path, 0o600)  # no group-read; the ACL handles the receiver-read
    if _try_setfacl(path, receiver_uid):
        return
    # Fallback: ACL not available -> group-read (name-secrecy load-bearing again) + log.
    _chmod_best_effort(path, 0o640)
    import sys

    print(
        f"pm-mesh: setfacl not available for {path!r}; falling back to group-read "
        f"(name-secrecy load-bearing, F5)",
        file=sys.stderr,
    )


def _chmod_best_effort(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _try_setfacl(path: str, uid: int) -> bool:
    """Best-effort set a POSIX ACL ``u:<uid>:r`` on ``path``; ``True`` on success, ``False`` otherwise.

    ``False`` if ``setfacl`` is missing or the fs doesn't support ACLs (non-zero exit). Never raises.
    """
    import shutil
    import subprocess

    if shutil.which("setfacl") is None:
        return False
    try:
        res = subprocess.run(
            ["setfacl", "-m", f"u:{uid}:r", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def list_new(address: str, root=None):
    """Paths of messages in ``new/`` of ``address`` (sorted, deterministic).

    Hidden names (``.`` prefix, incl. the ``.deliver-`` temp remnant of an in-progress ``deliver``)
    are skipped — those aren't delivered messages.
    """
    new_dir = os.path.join(maildrop(address, root=root), "new")
    names = sorted(n for n in os.listdir(new_dir) if not n.startswith("."))
    return [os.path.join(new_dir, n) for n in names]


def claim(path: str):
    """Claim ``new/<f>`` with an **atomic** ``os.rename`` to ``cur/<f>``.

    Returns the new ``cur/`` path on success, or ``None`` if the rename raises
    ``FileNotFoundError`` — then another session won the claim (the source was already gone).
    """
    new_dir = os.path.dirname(path)
    drop = os.path.dirname(new_dir)
    cur_path = os.path.join(drop, "cur", os.path.basename(path))
    try:
        os.rename(path, cur_path)
    except FileNotFoundError:
        return None
    return cur_path


def quarantine(cur_path: str) -> None:
    """Move an unverifiable/corrupt ``cur/`` file to ``held/`` (best-effort).

    A hardlink or corrupt file can NEVER become verifiable. Were it to stay in ``cur/``,
    ``recover_stale`` would move it back to ``new/`` every ``max_age_s``, where it gets claimed and
    rejected again — an endless churn loop. Putting it in ``held/`` (which the janitor doesn't
    scan) stops that loop and preserves the evidence for inspection."""
    drop = os.path.dirname(os.path.dirname(cur_path))
    held_path = os.path.join(drop, "held", os.path.basename(cur_path))
    try:
        os.rename(cur_path, held_path)
    except OSError:
        pass  # best-effort: if moving fails, leave the file where it is (no crash)


def hold(cur_path: str):
    """Move a ``cur/`` message to ``held/`` (gate: awaiting approval); return the held path.

    Used by the gate layer (f2-04): a message that isn't ``auto`` per the trust policy is taken
    out of the active flow and parked in ``held/`` until ``mesh approve`` (f2-05) releases it. The
    janitor (``recover_stale``) doesn't scan ``held/``, so it stays put without churn. ``None`` if
    the move fails (best-effort, no crash)."""
    drop = os.path.dirname(os.path.dirname(cur_path))
    held_path = os.path.join(drop, "held", os.path.basename(cur_path))
    try:
        os.rename(cur_path, held_path)
    except OSError:
        return None
    return held_path


def consume_new(address: str, root=None, limit=None):
    """Generator: claim every ``new/`` message and yield ``(Message, owner_uid, cur_path)``.

    Per message: ``claim`` (skip on ``None`` — another session won it), then
    ``identity.read_verified`` on the **cur/** path (kernel-verified owner_uid, anti-TOCTOU), then
    ``message.from_json``. A corrupt or unverifiable file is **quarantined to
    ``held/``** (see ``quarantine``) without crashing.

    ``limit`` (optional): stop after ``limit`` successfully yielded messages and leave the rest
    untouched in ``new/`` for the next turn (bounds context-/cost-DoS).
    """
    yielded = 0
    for path in list_new(address, root=root):
        if limit is not None and yielded >= limit:
            return  # cap reached: rest stays in new/ (not claimed) for the next turn
        cur_path = claim(path)
        if cur_path is None:
            continue
        try:
            data, owner_uid = identity.read_verified(cur_path)
            msg = message.from_json(data.decode("utf-8"))
        except (identity.IdentityError, ValueError, UnicodeDecodeError, TypeError):
            # unverifiable/corrupt -> quarantine to held/ (prevents janitor churn), no crash.
            # TypeError catches a hostile hand-crafted field type (e.g. a non-string subject) so
            # one poisoned file can never bring down the whole inject turn (fail-closed per message).
            quarantine(cur_path)
            continue
        yielded += 1
        yield msg, owner_uid, cur_path


def mark_shown(cur_path: str) -> None:
    """Set the "shown" sidecar ``<cur_path>.shown`` (atomic, idempotent) — t07 convention.

    This makes the janitor (``recover_stale``) leave the message alone: it has genuinely been
    shown to the user and must not be recovered as orphaned back to ``new/``.
    """
    path = cur_path + SHOWN_SUFFIX
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)


#: Suffix of the "shown" sidecar in ``cur/``. The inject hook (t10) creates ``<name>.shown`` as
#: soon as a message has actually been shown to the user; the janitor leaves such messages alone
#: and only recovers claimed-but-never-shown (orphaned) messages.
SHOWN_SUFFIX = ".shown"


def recover_stale(address: str, root=None, max_age_s: int = 900, now=None) -> int:
    """Move orphaned ``cur/`` messages back to ``new/``; return the number recovered.

    A session that crashes after ``claim`` (``new/`` -> ``cur/``) but before showing it silently
    leaves a message behind in ``cur/``. For every regular file in ``cur/`` that (a) has no
    sidecar marker ``<name>.shown`` and (b) is older than ``max_age_s`` (based on ``mtime``), we
    rename it back to ``new/`` so a subsequent turn delivers it again.

    ``<name>.shown`` sidecars themselves are skipped (they're markers, not messages).
    ``now`` is injectable (epoch seconds) for deterministic tests; defaults to ``time.time()``.
    """
    import time

    if now is None:
        now = time.time()
    drop = maildrop(address, root=root)
    cur_dir = os.path.join(drop, "cur")
    new_dir = os.path.join(drop, "new")

    recovered = 0
    for name in os.listdir(cur_dir):
        if name.endswith(SHOWN_SUFFIX):
            continue  # marker, not a message
        path = os.path.join(cur_dir, name)
        if not os.path.isfile(path):
            continue
        # Shown? Then leave it alone.
        if os.path.exists(os.path.join(cur_dir, name + SHOWN_SUFFIX)):
            continue
        age = now - os.stat(path).st_mtime
        if age <= max_age_s:
            continue  # still fresh (or exactly at the boundary) — don't recover
        os.rename(path, os.path.join(new_dir, name))
        recovered += 1
    return recovered


def _mkstemp_0600(directory: str):
    """Create a 0600 temp file with an inconspicuous, non-colliding name in ``directory``."""
    fd, path = _mkstemp_in(directory)
    os.fchmod(fd, 0o600)
    return fd, path


def _mkstemp_in(directory: str):
    import tempfile

    # Hidden prefix so a listing of new/ never mistakes the temp remnant for a message;
    # deliver renames it away, and the except branch cleans it up on failure.
    return tempfile.mkstemp(prefix=".deliver-", dir=directory)
