"""``mesh team-init`` — inspect the shared root and, with ``--apply``, do ONLY the unprivileged half.

The load-bearing property is the split: **this tool never escalates privilege and never executes a
root step.** It prints every ``sudo`` step for a human to run; the only thing ``--apply`` itself does
is create the caller's *own* dropbox (which only its receiver may create) via
``maildir.maildrop(..., mode="cross_user")``. There is deliberately no shell-out here — a tool that
runs commands as root while unattended is exactly what CLAUDE.md rule #11 forbids.

Exit codes: ``0`` = nothing missing; ``1`` = something a human must still do; ``2`` = the shared root
itself is unusable (absent / wrong owner / wrong mode) — a distinct code because it is the one state
where nothing else can be trusted, so ``--apply`` refuses to half-provision on top of it.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import maildir, team_init

_ROOT_STEPS = ("shared_root_present", "shared_root_mode")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh team-init",
        description="Inspect (and optionally do the unprivileged half of) shared-root provisioning.",
    )
    p.add_argument("--apply", action="store_true",
                   help="perform only the unprivileged, caller-owned steps (create your own dropbox)")
    p.add_argument("--plan", action="store_true",
                   help="show the provisioning plan (default when neither --apply nor --json)")
    p.add_argument("--json", action="store_true", help="emit the plan as JSON")
    return p


def _root_usable(steps: list[dict]) -> bool:
    """True iff the shared root exists and is correctly owned/moded — the substrate for everything."""
    by = {s["name"]: s for s in steps}
    return all(by.get(name, {}).get("ok") is True for name in _ROOT_STEPS)


def _exit_code(steps: list[dict]) -> int:
    if not _root_usable(steps):
        return 2
    if any(s["ok"] is not True for s in steps):
        return 1
    return 0


def _mark(ok) -> str:
    return "OK  " if ok is True else ("FAIL" if ok is False else "????")


def _sudo_block(steps: list[dict]) -> list[str]:
    """The copy-paste block of privileged remedies for a human — never executed by this tool."""
    lines = []
    pending = [s for s in steps if s["ok"] is not True and s["privileged"]]
    if pending:
        lines.append("")
        lines.append("run these as a human with sudo (this tool never runs them for you):")
        for s in pending:
            lines.append(f"  # {s['name']}: {s['detail']}")
            lines.append(f"  {s['remedy']}")
    return lines


def _verification() -> str:
    return "verify afterwards with: mesh-doctor   and   mesh-whoami"


def _render_plan(steps: list[dict]) -> str:
    lines = ["mesh team-init — shared-root provisioning plan", ""]
    for s in steps:
        priv = "privileged" if s["privileged"] else "unprivileged"
        lines.append(f"[{_mark(s['ok'])}] {s['name']}  ({priv})")
        lines.append(f"       {s['detail']}")
        if s["ok"] is not True:
            lines.append(f"       remedy: {s['remedy']}")
    lines.extend(_sudo_block(steps))
    lines.append("")
    lines.append(_verification())
    return "\n".join(lines)


def _do_apply(steps: list[dict]) -> int:
    """Create the caller's own dropbox — the only mutation this tool ever performs."""
    if not _root_usable(steps):
        out = [
            "mesh team-init --apply: the shared root is not usable yet — refusing to provision the "
            "dropbox on a broken substrate (nothing written).",
        ]
        out.extend(_sudo_block(steps))
        out.append("")
        out.append(_verification())
        print("\n".join(out))
        return 2

    # Both the address and the root must be the ones the plan just inspected. Deriving either a
    # second way is how a provisioning tool reports on one mailbox and creates another: the address
    # from the cwd basename misses a path-qualified session, and maildrop's own root default is the
    # *active* root, which is not necessarily the shared one this command was inspecting.
    root = team_init.resolve_root()
    addr = team_init.own_address(root=root)
    # mode=None derives it from config — the same source the plan's expectations came from, so
    # apply can never write modes the plan was not checking for.
    # A dropbox is created by its receiver and by nobody else — we run as that receiver here.
    maildir.maildrop(addr, root=root, mode=None)  # creates new/cur/held; re-validates internally

    fresh = team_init.plan()
    out = [f"mesh team-init --apply: created your own dropbox {addr} (new/cur/held)."]
    remaining = _sudo_block(fresh)
    if remaining:
        out.append("")
        out.append("still needs a human (privileged, not run by this tool):")
        out.extend(remaining[1:])  # drop the duplicate header line from _sudo_block
    out.append("")
    out.append(_verification())
    print("\n".join(out))
    return _exit_code(fresh)


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    steps = team_init.plan()

    if args.apply:
        return _do_apply(steps)

    if args.json:
        print(json.dumps({"steps": steps, "summary": team_init.summary(steps)}, indent=2))
        return _exit_code(steps)

    print(_render_plan(steps))
    return _exit_code(steps)


if __name__ == "__main__":  # pragma: no cover
    from .group_reexec import reexec_under_mesh_group_if_needed
    reexec_under_mesh_group_if_needed("pm_mesh.team_init_cli")
    raise SystemExit(main(sys.argv[1:]))
