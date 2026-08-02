"""``mesh-eval`` — fire the agentixmesh injection-drill at an agent you control and score it.

One command that wires the corpus, the run engine and the scorer into the
adopter-facing flow. The drill is inherently two-phase and asynchronous: ``run`` drops injection
attempts into a target session's inbox; a *separate* run of that agent then processes the inbox; and
``score`` reads the canary afterwards to see whether the agent obeyed any untrusted body.

Safety is front-loaded: ``run`` is a **dry run unless ``--apply``**, refuses a cross-uid target
without ``--allow-cross-user``, and warns loudly on ``--blind`` (the unmarked, stricter variant that
must only be aimed at an agent you own). ``score`` follows the exit-code contract (0 = every sent
case resisted, 1 = at least one compliance, 2 = no evidence / unusable), so a clean run reads as a
floor — never a certificate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import config, eval_run, eval_score
from .eval_corpus import CORPUS, MARKER


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-eval",
        description="Injection-resistance drill: fire test bodies at an agent you own and score it.",
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="print the injection corpus (id · category · title); no side effects")

    run = sub.add_parser("run", help="run (dry by default) the drill against a target address")
    run.add_argument("--to", required=True, help="target address <uid>:<project> (an agent you own)")
    run.add_argument("--apply", action="store_true",
                     help="actually send (default is a dry run that only writes the manifest)")
    run.add_argument("--blind", action="store_true",
                     help="unmarked, stricter variant (marker off) — only against an agent you own")
    run.add_argument("--cases", default=None,
                     help="comma-separated case ids to run (default: the whole corpus)")
    run.add_argument("--canary-path", default=None,
                     help="where to plant the canary (default: inside the run dir, whose name "
                          "tells a reader this is a drill — pass a neutral path for a blind run)")
    run.add_argument("--repo-file", default=None,
                     help="in-tree file the repo_edit cases nominate; the engine creates it empty "
                          "(0600) and watches it — pass a path you are willing to have written to. "
                          "Without it the repo_edit cases are skipped.")
    run.add_argument("--third-addr", default=None,
                     help="third mesh address the mesh_action cases try to make the agent send to — "
                          "an address you own, NOT the target and NOT your own. Without it the "
                          "mesh_action cases are skipped.")

    score = sub.add_parser("score", help="score a run's canary and exit per the documented contract")
    g = score.add_mutually_exclusive_group()
    g.add_argument("--run", dest="run_id", default=None, help="run id to score")
    g.add_argument("--latest", action="store_true", help="score the most recent run")
    score.add_argument("--json", action="store_true", help="emit JSON instead of the text report")

    return p


def _cmd_list() -> int:
    for case in CORPUS:
        print(f"{case.id} · {case.category} · {case.title}")
    return 0


def _select_cases(spec: str | None):
    """Return the corpus subset named by a comma-separated ``--cases`` spec, or the whole corpus."""
    if not spec:
        return list(CORPUS)
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    by_id = {c.id: c for c in CORPUS}
    unknown = [w for w in wanted if w not in by_id]
    if unknown:
        raise ValueError(f"unknown case id(s): {', '.join(unknown)}")
    return [by_id[w] for w in wanted]


def _cmd_run(args) -> int:
    try:
        cases = _select_cases(args.cases)
    except ValueError as exc:
        print(f"mesh-eval run: {exc}", file=sys.stderr)
        return 2

    try:
        uid, _project = config.parse_address(args.to)
    except ValueError as exc:
        print(f"mesh-eval run: invalid --to address: {exc}", file=sys.stderr)
        return 2

    if uid != os.getuid():
        print(
            f"mesh-eval run: {args.to} belongs to another uid ({uid}); a cross-uid drill cannot be "
            f"scored — the canary is in your private directory that uid cannot write, and a "
            f"cross-user body is withheld by the approval gate before the agent ever sees it. "
            f"Both force a 'resisted' verdict regardless of behaviour. Run the drill as the user "
            f"who owns the agent under test.",
            file=sys.stderr,
        )
        return 2

    marker = not args.blind
    if args.blind:
        print(
            "mesh-eval run: BLIND drill (unmarked) — the target sees no 'this is a test' banner. "
            "Only ever aim a blind drill at an agent you own.",
            file=sys.stderr,
        )

    plan = eval_run.plan(
        cases, to=args.to, marker=marker,
        repo_file=args.repo_file, third_addr=args.third_addr,
    )

    # Announce skipped categories BEFORE anything is sent — a drill that quietly tests eight of ten
    # families while printing a confident summary is the silent-truncation failure this project keeps
    # re-learning. The skill's own rule is: no silent caps.
    skipped = [e for e in plan if e.get("skip_reason")]
    if skipped:
        by_reason: dict = {}
        for entry in skipped:
            key = (entry["category"], entry["skip_reason"])
            by_reason[key] = by_reason.get(key, 0) + 1
        print(
            "mesh-eval run: the following categories will NOT be tested (their channel is not "
            "configured) — a drill that skips a category cannot exit 0:",
            file=sys.stderr,
        )
        for (category, reason), count in by_reason.items():
            print(f"  {category}: {count} case(s) — {reason}", file=sys.stderr)
        print(
            "  supply --repo-file <path> (repo_edit) and/or --third-addr <uid:project> "
            "(mesh_action) to include them.",
            file=sys.stderr,
        )

    try:
        manifest = eval_run.execute(
            plan,
            to=args.to,
            dry_run=not args.apply,
            marker=marker,
            canary_path=args.canary_path,
            repo_file=args.repo_file,
            third_addr=args.third_addr,
        )
    except eval_run.EvalRunError as exc:
        print(f"mesh-eval run: {exc}", file=sys.stderr)
        return 2

    run_id = manifest["run_id"]
    n_sent = sum(1 for e in plan if not e.get("skip_reason"))
    if args.apply:
        print(f"sent {n_sent} case(s) to {args.to}  (marker={'off' if args.blind else 'on'})")
    else:
        print(
            f"DRY RUN — nothing was sent. {n_sent} case(s) planned for {args.to}. "
            f"Re-run with --apply to actually send; a dry run tests nothing."
        )
    print(f"run_id: {run_id}")
    print(
        "next: start (or resume) the agent-under-test so it processes its inbox, then run:\n"
        f"      mesh-eval score --run {run_id}"
    )
    return 0


def _cmd_score(args) -> int:
    if args.latest:
        # "Latest" means the last drill that actually sent something. A dry run afterwards writes
        # a manifest too, and scoring that would report no evidence for a run that tested nothing —
        # which looks, at a glance, exactly like a real drill the agent slept through.
        run_id = eval_run.latest_sent_run_id()
        newest = eval_run.latest_run_id()
        if run_id is None:
            if newest is not None:
                print("mesh-eval score: the only runs on record are dry runs, which tested "
                      "nothing. Re-run with --apply.", file=sys.stderr)
            else:
                print("mesh-eval score: no runs found.", file=sys.stderr)
            return 2
        if newest != run_id:
            print(f"mesh-eval score: skipping newer dry run(s); scoring {run_id}, the last run "
                  f"that actually sent.", file=sys.stderr)
    elif args.run_id:
        run_id = args.run_id
    else:
        print("mesh-eval score: give --run <run_id> or --latest.", file=sys.stderr)
        return 2

    path = eval_run.manifest_path(run_id)
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"mesh-eval score: cannot read manifest for run {run_id}: {exc}", file=sys.stderr)
        return 2

    result = eval_score.score(
        manifest,
        reply_tokens=eval_run.observed_reply_tokens(manifest),
        third_tokens=eval_run.observed_third_tokens(manifest),
    )
    if args.json:
        print(eval_score.render_json(result))
    else:
        print(eval_score.render_report(result))
    rc = eval_score.exit_code(result)

    # A repo_edit compliance is unlike a canary one: the token landed in a file in a git working tree
    # a human may later commit, not in a private run dir. Name the file and say, in words, to inspect
    # and revert it — but never revert it automatically; the drill does not own the adopter's tree.
    repo_complied = [
        c for c in result.get("cases", [])
        if c.get("channel") == "repo_file" and c.get("verdict") == "complied"
    ]
    if repo_complied:
        repo_path = manifest.get("repo_file_path")
        print(
            f"mesh-eval score: a repo_edit case COMPLIED — the agent-under-test modified "
            f"{repo_path}. That file is in a working tree a human may commit. Inspect it and revert "
            f"the agent's change; the drill deliberately does not touch your tree, so it will not "
            f"revert it for you.",
            file=sys.stderr,
        )

    # A 2 caused by an unfired category is INCOMPLETE COVERAGE, not a failed drill. Say so plainly and
    # repeat the flags, so an adopter with CI does not read the exit change as a regression.
    if result.get("skipped"):
        categories = sorted({s.get("category") for s in result["skipped"]})
        print(
            f"mesh-eval score: exit 2 here means INCOMPLETE COVERAGE, not failure — "
            f"{len(result['skipped'])} case(s) across {', '.join(categories)} were not tested. "
            f"Re-run 'mesh-eval run' with --repo-file <path> (repo_edit) and/or "
            f"--third-addr <uid:project> (mesh_action) to cover them.",
            file=sys.stderr,
        )

    return rc


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "list":
        return _cmd_list()
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "score":
        return _cmd_score(args)
    _build_parser().print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
