"""Leader-read consent (phase 2, f2-14, F4/condition 5) — **fail-closed default = NO leader-read**.

Leader-read means the head leader (an operator/lead role, e.g. uid 1001) can read the inbox of a
subject (another team member, e.g. uid 1002), including messages from third parties to that subject.
That is a **privacy choice the subject must explicitly grant** and can **revoke**. Until there is a
valid, subject-owned consent artifact, ``leader_read_allowed`` returns **False** (fail-closed): no
conscious consent, no leader-read.

The artifact is a file **owned by the subject itself** (kernel ownership == subject): a third party
cannot fabricate consent on the subject's behalf. Revoking = setting the artifact's ``revoked`` flag
or removing it; ``mesh revoke`` (f2-12) cleans it up as part of offboarding.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone

from . import config

#: Subdirectory with consent artifacts under the mesh root.
CONSENT_SUBDIR = "consent"

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def consent_dir(root=None) -> str:
    base = root if root is not None else config.mesh_root()
    return os.path.join(base, CONSENT_SUBDIR)


def consent_path(subject_uid: int, root=None) -> str:
    return os.path.join(consent_dir(root), f"leader-read-{subject_uid}.json")


def leader_read_allowed(subject_uid: int, root=None, leader_uid=None, now=None) -> bool:
    """``True`` only for a valid, subject-owned, non-revoked consent artifact.

    Fail-closed: missing/unreadable/unsafe/revoked/expired artifact, or an artifact not owned by the
    subject itself (kernel ownership != subject), → ``False``. If ``leader_uid`` is given, the
    artifact must name that leader.
    """
    path = consent_path(subject_uid, root)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False  # no (or no safely openable) artifact → no consent
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False
        # Ownership = the subject itself: nobody else can consent on the subject's behalf.
        if st.st_uid != subject_uid:
            return False
        if stat.S_IMODE(st.st_mode) & 0o022:
            return False  # group/other-writable → not to be trusted
        with os.fdopen(fd, encoding="utf-8") as fh:
            data = json.load(fh)
        fd = None  # taken over by fdopen
    except (ValueError, OSError):
        if fd is not None:
            os.close(fd)
        return False
    finally:
        if fd is not None:
            os.close(fd)

    if not isinstance(data, dict):
        return False
    if data.get("subject_uid") != subject_uid:
        return False
    if data.get("granted") is not True or data.get("revoked"):
        return False
    if leader_uid is not None and data.get("leader_uid") != leader_uid:
        return False
    expires = data.get("expires_utc")
    if isinstance(expires, str):
        try:
            exp = datetime.strptime(expires, _ISO_FMT).replace(tzinfo=timezone.utc).timestamp()
            ref = now if now is not None else datetime.now(timezone.utc).timestamp()
            if ref > exp:
                return False
        except ValueError:
            return False  # unparseable expiry date → fail-closed
    return True


def revoke_consent(subject_uid: int, root=None) -> bool:
    """Revoke leader-read consent for ``subject_uid`` by removing the artifact; ``True`` if one existed.

    Hook for ``mesh revoke`` (f2-12): offboarding = consent void.
    """
    path = consent_path(subject_uid, root)
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def grant_consent(leader_uid: int, subject_uid=None, root=None, expires_utc=None, now=None) -> str:
    """Write a valid, subject-owned leader-read-consent artifact; return its path.

    Replaces the manual "hand-write JSON + chmod" procedure (``LEADER-READ-CONSENT.md``) with one
    call that the **subject itself** runs: ``subject_uid`` defaults to ``os.getuid()``, so the file
    is kernel-owned by the caller — exactly the load-bearing property ``leader_read_allowed`` relies
    on (a third party cannot fabricate consent on the subject's behalf).

    **Perms 0o640** (owner rw, group r, no other, no group/other WRITE). The head leader is an
    *other* uid in group ``mesh`` and must be able to **read** the artifact for leader-read to
    work; ``leader_read_allowed`` only refuses group/other WRITE (``& 0o022``) and deliberately
    allows group-read. The content is a non-secret consent statement, not a secret. Writing is
    in-place (no temp+rename); for a self-grant with no concurrent readers that is sufficient. The
    mode is set explicitly (``os.fchmod``) so the umask cannot loosen or tighten it.

    Idempotent-ish: an existing own artifact is overwritten (re-grant).
    """
    if subject_uid is None:
        subject_uid = os.getuid()
    ref = now if now is not None else datetime.now(timezone.utc)
    ts_utc = ref.astimezone(timezone.utc).strftime(_ISO_FMT)

    os.makedirs(consent_dir(root), exist_ok=True)  # create only if absent; don't force dir perms
    path = consent_path(subject_uid, root)

    payload = {
        "subject_uid": int(subject_uid),
        "leader_uid": int(leader_uid),
        "granted": True,
        "revoked": False,
        "ts_utc": ts_utc,
        "expires_utc": expires_utc,
        "confirmation": (
            f"I (uid {subject_uid}) grant the head leader (uid {leader_uid}) permission "
            "to read my mesh inbox."
        ),
        "signed_by": str(subject_uid),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    # Write in-place + force the mode explicitly (umask-independent 0o640).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        os.fchmod(fd, 0o640)  # O_CREAT mode is umask-masked; force the exact bits.
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None  # taken over by fdopen
            fh.write(body)
    finally:
        if fd is not None:
            os.close(fd)
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_mesh.consent",
        description="Leader-read consent: grant/revoke/status in one command (run as yourself).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_grant = sub.add_parser("grant", help="grant the head leader leader-read consent")
    p_grant.add_argument("--leader-uid", type=int, required=True, help="uid of the head leader")
    p_grant.add_argument("--expires-utc", default=None, help="optional expiry date (UTC ISO-8601)")
    p_grant.add_argument(
        "--subject-uid", type=int, default=None, help="subject uid (default = current uid)"
    )

    p_revoke = sub.add_parser("revoke", help="revoke leader-read consent")
    p_revoke.add_argument(
        "--subject-uid", type=int, default=None, help="subject uid (default = current uid)"
    )

    p_status = sub.add_parser("status", help="show whether leader-read is allowed")
    p_status.add_argument(
        "--subject-uid", type=int, default=None, help="subject uid (default = current uid)"
    )
    p_status.add_argument(
        "--leader-uid", type=int, default=None, help="filter on this head-leader uid"
    )
    return parser


def main(argv=None) -> int:
    """Console entry: grant/revoke/status for leader-read consent. Respects ``$MESH_ROOT``."""
    args = _build_parser().parse_args(argv)
    subject = args.subject_uid if args.subject_uid is not None else os.getuid()

    if args.cmd == "grant":
        path = grant_consent(args.leader_uid, subject_uid=subject, expires_utc=args.expires_utc)
        allowed = leader_read_allowed(subject, leader_uid=args.leader_uid)
        print(f"consent written: {path}")
        print(f"leader_read_allowed({subject}): {allowed}")
        return 0

    if args.cmd == "revoke":
        removed = revoke_consent(subject)
        print(f"consent removed: {removed}")
        return 0

    if args.cmd == "status":
        allowed = leader_read_allowed(subject, leader_uid=args.leader_uid)
        print(f"leader_read_allowed({subject}): {allowed}")
        return 0

    return 2  # pragma: no cover — argparse enforces a valid subcommand


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
