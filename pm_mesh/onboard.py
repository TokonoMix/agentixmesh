"""``mesh-onboard`` — steward/participant onboarding wizard (capability-grants plan §6c).

Role model (docs/2026-07-03-autonomous-capability-grants-plan.md §2A/§6b/§6c):

* **steward** — the central human ultimately responsible, designated at first install.
* **participant account** — a colleague with their own OS uid.
* **project-agent** — a Claude Code session acting for an account/project.

The wizard writes only files that existing, reviewed mechanisms already use — it introduces
**no new rights**:

1. ``onboard steward`` walks the steward through accounts/projects/permission-pairs and writes
   (a) address-book entries into ``$MESH_ROOT/addressbook.json`` (merged with what is already there,
   via the existing :mod:`pm_mesh.addressbook` merge logic — never overwritten), and (b) an
   **intent-only** permission matrix into ``$MESH_ROOT/permissions.json``. For every cross-user
   ``info`` pair it *prints* the exact ``mesh-trust`` command the RECEIVING participant must run
   themselves — the wizard never runs it, and never touches anyone else's trust policy.
2. ``onboard participant`` reads that matrix, shows only the pairs proposed *to the caller's own
   uid* (``os.getuid()``), asks per-sender confirmation, and — only for the closed-vocabulary
   ``info`` level, and only after an explicit yes — applies the *receiver's own* trust policy via
   the same path as ``mesh-trust`` (:mod:`pm_mesh.trust_cli`). ``do``/``write``/``change`` are
   recorded as INTENTION only; there is (yet) no enforcement path for them, so confirming one never
   causes any write — phase 2B does not exist yet (spec §2B).

Permission vocabulary is a **closed set**: ``info | do | write | change``, plus a free-text
``custom`` field that is an AUDIT note ONLY — it is never read by any decision path (consensus
finding #1/#2, spec §6b). Only ``info`` is enforceable today (== trust-level ``notify-only`` for
cross-user, ``auto`` within one uid already); anything above ``info`` is stored with a fixed
``"intent-only above info"`` note and a printed warning that it stays human-gated.

**Hard boundary (spec §6c point 6):** cross-user enforcement keys on **uid**, never on a
project-label, and the steward's matrix is only ever a *proposal* across account boundaries — the
receiving account's own 0600 trust policy is what actually governs its inbox. A compromised
steward session must never be able to force another account's receive policy; that is exactly why
``onboard participant`` requires the RECEIVER to run the apply-step themselves, in their own
session, under their own uid.

Core/IO split: ``plan_steward`` and ``plan_participant`` are pure functions — (answers-dict,
existing-state) -> (files-to-write / commands-to-print). They raise :class:`OnboardError` on any
invalid input (fail-closed) and perform no I/O. ``main`` is the thin CLI layer: it loads existing
state, calls the pure core, and only then writes files / prints / delegates to
:mod:`pm_mesh.trust_cli`. ``--answers <file>`` feeds the same answers-shape non-interactively (for
tests and automation); without it, ``main`` collects the identical shape via ``input()``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field

from . import addressbook, config, trust, trust_cli

#: Closed permission vocabulary (spec §6c point 5). No other string is ever accepted.
PERMISSION_LEVELS = ("info", "do", "write", "change")

#: Fixed audit note stored for every pair above 'info' — never influences enforcement.
INTENT_ONLY_NOTE = "intent-only above info"

#: The only permission level with an enforcement path today (== trust-level 'notify-only'
#: cross-user). Kept as a constant so the mapping lives in exactly one place.
_ENFORCEABLE_LEVEL = "info"
_ENFORCEABLE_TRUST_LEVEL = trust.NOTIFY_ONLY


class OnboardError(Exception):
    """Fail-closed validation error. The CLI turns this into a non-zero exit; nothing is written."""


def _require_int(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise OnboardError(f"{what} must be an integer uid, got {value!r}")
    return value


@dataclass
class StewardPlan:
    """Pure output of ``plan_steward`` — nothing here has touched disk yet."""

    addressbook_entries: list
    permissions_doc: dict
    notify_commands: list
    warnings: list


@dataclass
class ParticipantProposal:
    from_uid: int
    level: str
    custom: str
    note: str


@dataclass
class ParticipantPlan:
    """Pure output of ``plan_participant``. ``grants`` is (from_uid, trust_level) pairs to apply —
    ONLY ever populated for the closed-vocabulary 'info' level, on explicit confirmation."""

    proposals: list = field(default_factory=list)
    grants: list = field(default_factory=list)


def _entries_to_raw(entries: dict) -> list:
    return [
        {"address": e.address, "display": e.display, "dir": e.dir, "aliases": list(e.aliases)}
        for e in entries.values()
    ]


def plan_steward(
    answers: dict,
    *,
    existing_addressbook_entries: list | None = None,
    existing_permissions: dict | None = None,
    caller_uid: int,
    mesh_root: str,
) -> StewardPlan:
    """Pure core of ``onboard steward``. Raises :class:`OnboardError` (fail-closed) on any invalid
    input; never writes anything itself."""
    existing_addressbook_entries = existing_addressbook_entries or []

    steward = answers.get("steward") or {}
    steward_uid = _require_int(steward.get("uid"), "steward.uid")

    # Rewrite guard (requirement 4): only the recorded steward may update an existing matrix; the
    # first run (no file yet) defines it. A participant may read the matrix but never rewrite it.
    if existing_permissions:
        existing_steward = existing_permissions.get("steward_uid")
        if existing_steward is not None and existing_steward != caller_uid:
            raise OnboardError(
                f"permissions.json is already owned by steward uid {existing_steward}; "
                f"uid {caller_uid} may not rewrite the matrix (only the steward may)"
            )

    new_entries = []
    for acc in answers.get("accounts") or []:
        uid = _require_int(acc.get("uid"), "account.uid")
        display = acc.get("display") or ""
        for proj in acc.get("projects") or []:
            name = proj.get("name")
            if not isinstance(name, str) or not name.strip():
                raise OnboardError(f"account {uid}: every project needs a non-empty 'name'")
            aliases = proj.get("aliases") or []
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise OnboardError(f"account {uid}: project {name!r} aliases must be a list of strings")
            new_entries.append(
                {
                    "address": f"{uid}:{name}",
                    "display": display,
                    "dir": proj.get("dir") or "",
                    "aliases": aliases,
                }
            )

    # Reuse the existing, reviewed address-book merge logic — later layer wins scalars, aliases
    # union. This is what makes the write a MERGE, never an overwrite.
    merged = addressbook.merge_entries([existing_addressbook_entries, new_entries])
    merged_raw = _entries_to_raw(merged)

    existing_pairs: dict = {}
    for p in (existing_permissions or {}).get("pairs") or []:
        try:
            key = (int(p["from_uid"]), int(p["to_uid"]))
        except (KeyError, TypeError, ValueError):
            continue
        existing_pairs[key] = p

    notify_commands = []
    warnings = []
    new_pairs: dict = {}
    for pair in answers.get("pairs") or []:
        from_uid = _require_int(pair.get("from_uid"), "pair.from_uid")
        to_uid = _require_int(pair.get("to_uid"), "pair.to_uid")
        level = pair.get("level")
        if level not in PERMISSION_LEVELS:
            raise OnboardError(
                f"unknown permission level {level!r} for pair {from_uid}->{to_uid}; "
                f"must be one of {PERMISSION_LEVELS}"
            )
        custom = pair.get("custom") or ""
        if not isinstance(custom, str):
            raise OnboardError(f"pair {from_uid}->{to_uid}: 'custom' must be a string")
        note = "" if level == _ENFORCEABLE_LEVEL else INTENT_ONLY_NOTE
        new_pairs[(from_uid, to_uid)] = {
            "from_uid": from_uid,
            "to_uid": to_uid,
            "level": level,
            "custom": custom,
            "note": note,
        }
        if note:
            warnings.append(
                f"{from_uid} -> {to_uid}: '{level}' is {INTENT_ONLY_NOTE} — stays human-gated until "
                f"phase 2B exists (the custom note is documentation only; it never changes enforcement)"
            )
        if level == _ENFORCEABLE_LEVEL and from_uid != to_uid:
            notify_commands.append(
                f"MESH_ROOT={mesh_root} mesh-trust grant {from_uid} {_ENFORCEABLE_TRUST_LEVEL}"
                f"   # run this AS uid {to_uid} — the receiver opts in, never the wizard"
            )

    merged_pairs = {**existing_pairs, **new_pairs}
    pairs_out = sorted(merged_pairs.values(), key=lambda p: (p["from_uid"], p["to_uid"]))

    permissions_doc = {"version": 1, "steward_uid": steward_uid, "pairs": pairs_out}
    return StewardPlan(merged_raw, permissions_doc, notify_commands, warnings)


def plan_participant(permissions_doc: dict, my_uid: int, confirmations: dict) -> ParticipantPlan:
    """Pure core of ``onboard participant``. ``confirmations`` maps ``str(from_uid) -> bool``.

    Fail-closed: a pair with an unrecognized level, or a non-integer uid, is silently dropped —
    never acted on. Only the 'info' level is ever translated into a trust grant, and only on an
    explicit confirmed=True; 'do'/'write'/'change' never produce a grant regardless of
    confirmation (no enforcement path exists for them yet)."""
    proposals = []
    grants = []
    for pair in (permissions_doc or {}).get("pairs") or []:
        try:
            to_uid = int(pair.get("to_uid"))
            from_uid = int(pair.get("from_uid"))
        except (TypeError, ValueError):
            continue
        if to_uid != my_uid:
            continue
        level = pair.get("level")
        if level not in PERMISSION_LEVELS:
            continue  # fail-closed: an unrecognized level in the matrix is never acted on

        proposals.append(
            ParticipantProposal(
                from_uid=from_uid, level=level, custom=pair.get("custom") or "", note=pair.get("note") or ""
            )
        )
        if not confirmations.get(str(from_uid), False):
            continue
        if level == _ENFORCEABLE_LEVEL:
            grants.append((from_uid, _ENFORCEABLE_TRUST_LEVEL))
        # do/write/change: intent-only, no enforcement path exists to apply — stays human-gated.

    # Defense-in-depth assertion (spec §6c: "assert it in the wizard too") — trust.py already
    # engine-floors cross-user 'auto', but the wizard must never even attempt to request it.
    for _, trust_level in grants:
        assert trust_level != trust.AUTO, "onboard: refusing to ever request 'auto' trust cross-user"

    return ParticipantPlan(proposals, grants)


# ---------------------------------------------------------------------------------------------
# Thin IO layer — file paths, atomic writes, interactive prompts, CLI wiring.
# ---------------------------------------------------------------------------------------------


def _addressbook_path(mesh_root: str) -> str:
    return os.path.join(mesh_root, "addressbook.json")


def _permissions_path(mesh_root: str) -> str:
    return os.path.join(mesh_root, "permissions.json")


def _load_json(path: str | None) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _atomic_write(path: str, data: dict, mode: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{question}{suffix}: ").strip()
    return raw or (default or "")


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{question} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def collect_steward_answers_interactively() -> dict:
    print("agentixmesh onboarding — steward setup")
    print("(closed permission vocabulary: info | do | write | change; 'custom' is a free audit note only)")
    steward_uid = int(_prompt("Your (steward) OS uid", default=str(os.getuid())))
    steward_display = _prompt("Steward display name", default="")

    accounts = []
    while _prompt_yes_no("Add a participant account?", default=not accounts):
        uid = int(_prompt("  account uid"))
        display = _prompt("  display name", default="")
        projects = []
        while _prompt_yes_no("  Add a project for this account?", default=not projects):
            name = _prompt("    project name")
            proj_dir = _prompt("    project directory (optional)", default="")
            aliases_raw = _prompt("    aliases, comma-separated (optional)", default="")
            aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
            projects.append({"name": name, "dir": proj_dir, "aliases": aliases})
        accounts.append({"uid": uid, "display": display, "projects": projects})

    pairs = []
    uids = sorted({steward_uid, *(a["uid"] for a in accounts)})
    for from_uid in uids:
        for to_uid in uids:
            if from_uid == to_uid:
                continue
            if _prompt_yes_no(f"Define a permission for {from_uid} -> {to_uid}?", default=False):
                level = _prompt(f"  level ({'/'.join(PERMISSION_LEVELS)})")
                custom = _prompt("  custom audit note (optional)", default="")
                pairs.append({"from_uid": from_uid, "to_uid": to_uid, "level": level, "custom": custom})

    return {"steward": {"uid": steward_uid, "display": steward_display}, "accounts": accounts, "pairs": pairs}


def collect_participant_confirmations_interactively(proposals: list) -> dict:
    confirmations = {}
    for p in proposals:
        note = f" ({p.note})" if p.note else ""
        question = f"Accept proposed permission from uid {p.from_uid}: '{p.level}'{note}?"
        confirmations[str(p.from_uid)] = _prompt_yes_no(question, default=False)
    return confirmations


def _cmd_steward(args) -> int:
    mesh_root = config.mesh_root()

    if args.answers:
        answers = _load_json(args.answers)
        if answers is None:
            print(f"mesh-onboard: cannot read answers file {args.answers!r}", file=sys.stderr)
            return 1
    else:
        answers = collect_steward_answers_interactively()

    existing_addressbook = (_load_json(_addressbook_path(mesh_root)) or {}).get("entries") or []
    existing_permissions = _load_json(_permissions_path(mesh_root))

    try:
        plan = plan_steward(
            answers,
            existing_addressbook_entries=existing_addressbook,
            existing_permissions=existing_permissions,
            caller_uid=os.getuid(),
            mesh_root=mesh_root,
        )
    except OnboardError as exc:
        print(f"mesh-onboard: refused: {exc}", file=sys.stderr)
        return 1

    _atomic_write(_addressbook_path(mesh_root), {"entries": plan.addressbook_entries}, 0o644)
    _atomic_write(_permissions_path(mesh_root), plan.permissions_doc, 0o644)

    print(f"wrote {_addressbook_path(mesh_root)}")
    print(f"wrote {_permissions_path(mesh_root)}")
    for w in plan.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if plan.notify_commands:
        print(
            "\nCross-user 'info' pairs need the RECEIVER to opt in themselves "
            "(the wizard never does this for another uid):"
        )
        for cmd in plan.notify_commands:
            print(f"  {cmd}")
    return 0


def _cmd_participant(args) -> int:
    mesh_root = config.mesh_root()
    my_uid = os.getuid()
    permissions_path = _permissions_path(mesh_root)
    permissions_doc = _load_json(permissions_path)
    if permissions_doc is None:
        print(
            f"mesh-onboard: no permissions matrix found at {permissions_path} yet "
            f"(ask your steward to run 'onboard steward' first)",
            file=sys.stderr,
        )
        return 0

    preview = plan_participant(permissions_doc, my_uid, {})
    if not preview.proposals:
        print(f"mesh-onboard: no proposals for uid {my_uid} in the matrix.")
        return 0

    if args.answers:
        raw = _load_json(args.answers) or {}
        confirmations = raw.get("confirmations") or {}
    else:
        confirmations = collect_participant_confirmations_interactively(preview.proposals)

    plan = plan_participant(permissions_doc, my_uid, confirmations)
    for p in plan.proposals:
        note = f" ({p.note})" if p.note else ""
        confirmed = confirmations.get(str(p.from_uid), False)
        print(f"from uid {p.from_uid}: proposed '{p.level}'{note} -> {'accepted' if confirmed else 'declined'}")

    for from_uid, trust_level in plan.grants:
        rc = trust_cli.main(["grant", str(from_uid), trust_level])
        if rc != 0:
            print(f"mesh-onboard: failed to apply trust grant for uid {from_uid}", file=sys.stderr)
            return rc
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mesh-onboard", description="agentixmesh steward/participant onboarding wizard.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("steward", help="steward Q&A: accounts, projects, and the intent permission matrix")
    s.add_argument("--answers", help="JSON file answering all prompts non-interactively (tests/automation)")

    ptc = sub.add_parser("participant", help="review + accept the steward's proposed permissions for your own uid")
    ptc.add_argument("--answers", help="JSON file with {'confirmations': {'<from_uid>': true/false}}")

    args = p.parse_args(argv)
    if args.cmd == "steward":
        return _cmd_steward(args)
    if args.cmd == "participant":
        return _cmd_participant(args)
    return 2  # pragma: no cover — argparse(required=True) already rejects an unknown/missing cmd


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
