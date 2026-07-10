"""Trust-policy engine (phase 2, f2-03) — uid-keyed, ``uid:project`` restrict-only, cross-user default
human-gate. **Security-critical, fail-closed.**

This is the core of phase-2 safety (design §7 + review correction **F1** + conditions **1 & 2**):

* **Condition 1** — a cross-user (different uid) or unknown sender **always** lands on at least
  ``human-gate``, **NEVER** silently on ``auto``. Auto for a foreign uid can only happen if the
  receiver explicitly sets that themself at **uid level** (a deliberate choice).
* **F1 / condition 2** — trust elevation keys **exclusively on ``uid``**. A ``uid:project``
  override may **only** make a uid level **STRICTER**, never elevate towards ``auto`` — because a
  sender freely chooses their ``project`` label, so elevation on ``uid:project`` would be
  cross-user exploitable. An override that would elevate is ignored (+ logged).

> **INTERFACE CONTRACT (load-bearing, for f2-04/f2-05):** the ``sender_uid`` passed to ``resolve``
> MUST be the **kernel-verified** ``owner_uid`` (``identity.read_verified`` / ``fstat``-on-fd),
> **never** a sender derived from the message JSON. The entire model collapses if the caller
> trusts the self-declared sender.

The policy file is **owned by the receiver, mode 0600**: a sender cannot make themself trusted.
A file with group/other bits or a different owner is **completely ignored** (fail-closed to the
safe default), not half-trusted.
"""

from __future__ import annotations

import json
import os
import stat
import sys

#: Trust levels, ordered from **least** (left) to **most** restrictive (right). The order is
#: security-load-bearing: ``auto`` is strictly the loosest; ``notify-only`` (metadata flows through
#: WITHOUT a hold) is looser than ``human-gate`` (held until agreement) — so a ``uid:project`` that
#: wants ``notify-only`` under a ``human-gate`` uid is a downgrade and is refused.
AUTO = "auto"
NOTIFY_ONLY = "notify-only"
HUMAN_GATE = "human-gate"
LEADER_GATE = "leader-gate"
BLOCK = "block"

#: Increasing restrictiveness. ``leader-gate`` >= ``human-gate`` (both gates; the exact refinement
#: is f2-09) — deliberately not pinned down tighter than the security invariant requires.
LEVELS = (AUTO, NOTIFY_ONLY, HUMAN_GATE, LEADER_GATE, BLOCK)
_RANK = {level: i for i, level in enumerate(LEVELS)}

#: Condition 1: everything cross-user/unknown falls back to this, never lower (towards auto).
CROSS_USER_DEFAULT = HUMAN_GATE


class TrustError(Exception):
    """The policy file is unusable/unsafe (wrong owner, too-loose mode, no file)."""


def _log(msg: str) -> None:
    print(f"pm-mesh trust: {msg}", file=sys.stderr)


def _level_for(policy, section: str, key: str):
    """Get a validated level from ``policy[section][key]``; ``None`` if absent/unknown.

    An unknown level string is **ignored** (-> ``None`` -> safe default), never used blindly.
    """
    if not isinstance(policy, dict):
        return None
    sec = policy.get(section)
    if not isinstance(sec, dict):
        return None
    val = sec.get(key)
    if val is None:
        return None
    if val not in _RANK:
        _log(f"unknown level {val!r} for {section}.{key} -> ignored (safe default)")
        return None
    return val


def _clamp_cross_user(level: str) -> str:
    """Engine-hard condition 1 (§18): a **cross-user** sender may NEVER become ``auto``.

    ``auto`` means the agent **acts autonomously** on content from another principal — precisely
    the prompt-injection propagation that the cross-user human-gate must block. A policy that
    (accidentally or deliberately) sets ``auto`` for a foreign uid is here **floored to
    ``CROSS_USER_DEFAULT`` (``human-gate``)** — not as discipline but engine-enforced. Levels from
    ``notify-only`` upward (which NEVER act autonomously — only inert display) remain the
    receiver's explicit choice; only auto-acting is made impossible cross-user."""
    if level == AUTO:
        _log("cross-user 'auto' refused -> floored to human-gate (condition 1, engine-enforced)")
        return CROSS_USER_DEFAULT
    return level


def resolve(policy, sender_uid: int, sender_project: str, my_uid: int,
            *, sender_verified: bool = False) -> str:
    """Determine the trust level for a sender. **Pure function, no I/O.**

    Rules (hard):
    1. ``sender_uid == my_uid`` (own session) -> ``auto`` — **but only** when the caller asserts, via
       ``sender_verified=True``, that ``sender_uid`` is the **kernel-verified** owner_uid
       (``identity.read_verified`` / ``fstat``), not a value derived from the message JSON. This is
       **defense-in-depth**: the only path to ``auto`` must not rest on a comment-only contract.
       **Fail-safe**: without that assertion a same-uid match falls through to the cross-user path
       (-> ``human-gate``), so a caller that ever passes an unverified uid degrades safely instead of
       acting autonomously.
    2. otherwise: the uid level from the policy, or ``CROSS_USER_DEFAULT`` (``human-gate``) if the
       uid isn't explicitly in the policy — never lower.
    3. a ``uid:project`` override is **only** honored if it's **equal to or stricter** than the
       uid level (``rank[proj] >= rank[uid]``); otherwise ignored + logged (F1 restrict-only).
    4. **cross-user clamp (engine-hard):** the final level for a foreign uid can NEVER be
       ``auto`` — a policy that attempts that is floored to ``human-gate`` (``_clamp_cross_user``).
       Cross-user auto-acting is therefore impossible, regardless of the policy.
    """
    if sender_uid == my_uid and sender_verified:
        return AUTO

    uid_level = _level_for(policy, "by_uid", str(sender_uid))
    if uid_level is None:
        uid_level = CROSS_USER_DEFAULT

    proj_key = f"{sender_uid}:{sender_project}"
    proj_level = _level_for(policy, "by_uid_project", proj_key)
    if proj_level is None:
        level = uid_level
    elif _RANK[proj_level] >= _RANK[uid_level]:
        level = proj_level
    else:
        _log(
            f"uid:project override {proj_key}={proj_level!r} would elevate above uid level "
            f"{uid_level!r} -> ignored (F1: uid:project is restrict-only)"
        )
        level = uid_level

    return _clamp_cross_user(level)


def policy_path() -> str:
    """Path to the receiver's policy file (outside the shared mesh root -> not sender-reachable).

    Order: ``$MESH_POLICY`` -> ``$XDG_CONFIG_HOME/pm-mesh/policy.json`` -> ``~/.config/pm-mesh/policy.json``.
    """
    p = os.environ.get("MESH_POLICY")
    if p:
        return p
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "pm-mesh", "policy.json")


def load_policy(path: str) -> dict:
    """Read + validate the policy file; ``{}`` if it doesn't (yet) exist, raise ``TrustError`` if unsafe.

    Fail-closed + **TOCTOU-free** (same philosophy as ``identity.read_verified``): open with
    ``O_NOFOLLOW`` (a symlink -> ``OSError`` -> rejected) and validate with ``os.fstat`` on the
    **fd** — not on the path — so the checked object is identical to the read object (no
    file-swap race between check and read). Requirements: **regular file**, **owned by the euid**,
    NO **group/other bits** (mode ``0600``/``0400``). A sender who makes the file group/other
    writable to give themself ``auto`` is thus completely ignored. Corrupt JSON -> ``ValueError``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}  # no policy => empty policy => cross-user default human-gate
    except OSError as exc:
        # O_NOFOLLOW on a symlink -> ELOOP; any other open error -> fail-closed.
        raise TrustError(f"cannot safely open policy {path!r}: {exc}") from exc

    fh = None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TrustError(f"policy {path!r} is not a regular file")
        if st.st_uid != os.geteuid():
            raise TrustError(
                f"policy {path!r} is not owned by the receiver (st_uid={st.st_uid}, euid={os.geteuid()})"
            )
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise TrustError(
                f"policy {path!r} has group/other bits {oct(stat.S_IMODE(st.st_mode))} (expected 0600)"
            )
        fh = os.fdopen(fd, encoding="utf-8")  # takes ownership of the fd
        data = json.load(fh)
    finally:
        if fh is not None:
            fh.close()
        else:
            os.close(fd)

    if not isinstance(data, dict):
        raise TrustError(f"policy {path!r} is not a JSON object")
    return data


def load_policy_or_default(path: str) -> dict:
    """Like ``load_policy`` but **never raising**: any error -> ``{}`` (safe default) + stderr log.

    This is what the gate layer (f2-04) uses: an unusable/unsafe policy must not crash an inject
    turn, but also must not elevate anything — so it falls back to the cross-user human-gate default.
    """
    try:
        return load_policy(path)
    except (TrustError, ValueError, OSError) as exc:
        _log(f"policy {path!r} unusable ({exc}) -> safe default (cross-user human-gate)")
        return {}
