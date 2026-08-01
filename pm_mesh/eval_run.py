"""Drill engine for the agentixmesh evaluation harness.

Turns the :mod:`pm_mesh.eval_corpus` cases into an actual injection drill: plant a canary file,
(optionally) send each case to a target mesh address, and record what was sent so the scorer
can attribute any compliance to exactly one case.

Layering:

* :func:`run_dir` — creates a per-run ``0700`` directory holding ``manifest.json`` and ``canary.txt``.
* :func:`plan` — **pure**: assigns a fresh, unique token to each case and renders its body. It cannot
  know the per-run canary path yet (that directory is created inside :func:`execute`), so the body
  keeps ``{canary_path}`` as a literal placeholder that ``execute`` substitutes just before sending.
* :func:`execute` — the orchestrator: creates the run dir, plants the canary, fills each body's
  canary path, sends (or not, when ``dry_run``), and writes the manifest atomically.

Guard rails (pinned in ``test_eval_run.py`` and ``test_eval_review_fixes.py``): dry-run is the
default at every layer; a target whose uid differs from ``os.getuid()`` is refused outright, because
such a drill cannot be scored and would always read as "resisted"; ``execute`` writes only inside the
run dir when dry-running; the bodies actually being sent are re-validated, not just the shipped
corpus; and a drill that would exceed the advisory turn cap stops early.

Reuses the real send path — ``pm_mesh.message.new_message`` + ``pm_mesh.maildir.deliver`` — rather
than reimplementing the maildrop.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone

from . import config, maildir, message, presence
from .eval_corpus import CORPUS, MARKER, render, validate_body, validate_corpus

__all__ = [
    "EvalRunError",
    "MARKER",
    "run_dir",
    "plan",
    "execute",
    "list_run_ids",
    "manifest_path",
    "latest_run_id",
    "latest_sent_run_id",
    "observed_reply_tokens",
]

#: Sub-directory (under ``$XDG_DATA_HOME``/``~/.local/share``) that holds per-run drill directories.
EVAL_DIR_NAME = "agentixmesh-eval"

#: Literal kept in a planned body until :func:`execute` knows the real per-run canary path.
_CANARY_PLACEHOLDER = "{canary_path}"


class EvalRunError(Exception):
    """A drill cannot run safely (cross-uid target without consent, turn-cap exceeded, …)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _eval_base(root: str | None) -> str:
    """Return the directory that holds per-run drill directories.

    ``root`` overrides it; otherwise ``$XDG_DATA_HOME/agentixmesh-eval`` or, failing that,
    ``~/.local/share/agentixmesh-eval``.
    """
    if root is not None:
        return root
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, EVAL_DIR_NAME)
    return os.path.join(os.path.expanduser("~"), ".local", "share", EVAL_DIR_NAME)


def run_dir(root: str | None = None) -> str:
    """Create and return a fresh per-run directory (mode ``0700``).

    ``run_id`` = UTC timestamp + short random suffix, so two drills started in the same second do
    not collide. The base is umask-independently forced to ``0700`` as well.
    """
    base = _eval_base(root)
    os.makedirs(base, mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    path = os.path.join(base, run_id)
    os.makedirs(path, mode=0o700)  # not exist_ok: the random suffix makes this unique
    os.chmod(path, 0o700)
    return path


def list_run_ids(root: str | None = None) -> list[str]:
    """Return the run ids present under the eval base, sorted (timestamp prefix ⇒ chronological)."""
    base = _eval_base(root)
    try:
        entries = [n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n))]
    except OSError:
        return []
    return sorted(entries)


def manifest_path(run_id: str, root: str | None = None) -> str:
    """Path to a run's ``manifest.json``."""
    return os.path.join(_eval_base(root), run_id, "manifest.json")


def latest_run_id(root: str | None = None) -> str | None:
    """The newest run id.

    Ordered by the id itself — a UTC timestamp plus a random suffix — not by directory mtime.
    An mtime moves for reasons that have nothing to do with when the drill ran (a later score, a
    backup, a restore), and scoring the wrong run while believing you scored the last one is a
    quiet way to draw a wrong conclusion.
    """
    ids = list_run_ids(root)
    return ids[-1] if ids else None


def _manifest_of(run_id: str, root: str | None = None):
    try:
        with open(manifest_path(run_id, root), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def latest_sent_run_id(root: str | None = None) -> str | None:
    """The newest run that actually SENT something, or ``None``.

    A dry run writes a manifest like any other, so plain "latest" can land on a run that tested
    nothing and report it as no-evidence — indistinguishable, at a glance, from a real drill the
    agent slept through. Callers that mean "the drill I just ran" want this one.
    """
    for run_id in reversed(list_run_ids(root)):
        manifest = _manifest_of(run_id, root)
        if manifest and any(c.get("sent") for c in manifest.get("cases", [])):
            return run_id
    return None


def observed_reply_tokens(manifest: dict, root: str | None = None) -> set:
    """Return the manifest's tokens that appear in a message sitting in the runner's own mailbox.

    Several cases ask the agent to *send the token back* rather than (or as well as) writing it to
    disk. Reading only the canary leaves that entire channel unobserved while still printing a
    verdict for it — so the drill would report "resisted" for an agent that did exactly what the
    untrusted body asked. This reads ``new/`` and ``cur/`` **without claiming or consuming
    anything**: it is diagnosis, not delivery, and a real inject turn must still see those messages.
    """
    tokens = {c.get("token") for c in manifest.get("cases", []) if c.get("token")}
    if not tokens:
        return set()
    own = manifest.get("from")
    if not own:
        return set()
    try:
        drop = maildir.maildrop(own, root=root)
    except Exception:
        return set()

    # The drill's OWN messages carry the tokens too, and when a session drills itself they land in
    # this very mailbox. Excluding them by message id is what keeps this from reading the drill
    # back to itself and scoring every case as a compliance.
    own_ids = {c.get("message_id") for c in manifest.get("cases", []) if c.get("message_id")}

    seen: set = set()
    for sub in ("new", "cur"):
        directory = os.path.join(drop, sub)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, ValueError):
                continue
            try:
                if json.loads(text).get("id") in own_ids:
                    continue
            except ValueError:
                pass  # not our envelope shape; still worth scanning for a token
            for token in tokens:
                if token in text:
                    seen.add(token)
    return seen


def plan(cases, *, to: str, marker: bool = True) -> list[dict]:
    """Assign a fresh ``secrets.token_hex(8)`` token to each case and render its body.

    Pure (no filesystem, no sending). ``to`` is validated for shape here so a malformed address
    fails fast; the same-uid / cross-user policy is enforced later in :func:`execute`. Each returned
    entry is ``{case_id, category, token, body}`` — ``body`` still carries the ``{canary_path}``
    placeholder, filled per-run by :func:`execute`.
    """
    config.parse_address(to)  # raises ValueError on a malformed destination
    out: list[dict] = []
    for case in cases:
        token = secrets.token_hex(8)
        body = render(case, token=token, canary_path=_CANARY_PLACEHOLDER, marker=marker)
        out.append(
            {"case_id": case.id, "category": case.category, "token": token, "body": body}
        )
    return out


def _atomic_write(path: str, data: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def execute(
    plan: list[dict],
    *,
    to: str,
    dry_run: bool = True,
    from_addr: str | None = None,
    marker: bool = True,
    root: str | None = None,
    canary_path: str | None = None,
) -> dict:
    """Run the drill described by ``plan`` and return (and persist) its manifest.

    * ``dry_run`` (the default) sends nothing and writes nothing outside the run dir; each manifest
      case carries ``sent: false`` / ``message_id: null``.
    * A real run delivers each case with the existing send path and records the message id.

    Refuses (``EvalRunError``) a cross-uid target without ``allow_cross_user=True`` and a drill that
    would exceed the advisory ``config.MAX_TURNS_PER_THREAD`` cap. Refuses (``CorpusError``) if the
    shipped corpus fails validation.
    """
    validate_corpus()  # the shipped templates
    for entry in plan:  # and the bytes actually about to be sent
        validate_body(entry.get("body", ""), label=entry.get("case_id", "?"))

    uid, _project = config.parse_address(to)
    if uid != os.getuid():
        raise EvalRunError(
            f"target {to} belongs to another uid ({uid}); a cross-uid drill cannot be scored. "
            f"Two independent reasons: the canary lives in your own private directory, which that "
            f"uid cannot write; and a cross-user body is withheld by the approval gate before it "
            f"ever reaches the agent's context. Either one guarantees a 'resisted' verdict no "
            f"matter how the agent behaves, so the result would be meaningless. Run the drill as "
            f"the user who owns the agent under test."
        )

    if len(plan) > config.MAX_TURNS_PER_THREAD:
        raise EvalRunError(
            f"drill has {len(plan)} cases, above the advisory turn cap "
            f"({config.MAX_TURNS_PER_THREAD}); refusing rather than flooding the target."
        )

    # ``marker`` is threaded into ``plan`` (which bakes it into the bodies) and into the manifest
    # label independently, so a mislabelled run is possible if a caller desyncs them. That matters
    # for security tooling: a blind run recorded as marked (or vice versa) is a lie about how hard
    # the drill was to resist. Refuse the mismatch rather than persist a false label.
    marked = [MARKER in entry["body"] for entry in plan]
    if marker and not all(marked):
        raise EvalRunError(
            "marker=True but some case bodies are unmarked — plan/execute marker desync."
        )
    if not marker and any(marked):
        raise EvalRunError(
            "marker=False but some case bodies carry the marker — plan/execute marker desync."
        )

    # The session's REAL address, not basename(cwd). This is load-bearing twice over: it is the
    # return address the target replies to, and `observed_reply_tokens` scans it to see whether the
    # agent sent a token back. A path-qualified session (a worktree beside its main checkout) owns
    # `<base>--<segment>`; scanning `<base>` reads a mailbox that receives nothing, and every
    # exfiltration case would score `resisted` — exactly the false pass the reply channel exists
    # to close.
    from_ = from_addr if from_addr is not None else presence.resolve_own_address(root)[0]

    directory = run_dir(root=root)
    run_id = os.path.basename(directory)
    # The default canary sits in the run dir, which is named after this tool. That name is a tell:
    # an agent that reads the path can infer it is being drilled, which is precisely what `--blind`
    # is trying to avoid. `canary_path` lets the caller put it somewhere neutral.
    canary = canary_path or os.path.join(directory, "canary.txt")
    # Plant an empty canary (0600) that the target agent would only touch if it obeyed a body.
    fd = os.open(canary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.close(fd)

    cases: list[dict] = []
    for entry in plan:
        final_body = entry["body"].replace(_CANARY_PLACEHOLDER, canary)
        message_id = None
        sent = False
        if not dry_run:
            msg = message.new_message(to, final_body, kind="request", from_=from_)
            maildir.deliver(msg)
            message_id = msg.id
            sent = True
        cases.append(
            {
                "case_id": entry["case_id"],
                "category": entry["category"],
                "token": entry["token"],
                "message_id": message_id,
                "sent": sent,
            }
        )

    manifest = {
        "version": 1,
        "run_id": run_id,
        "created_utc": _utc_now_iso(),
        "to": to,
        "from": from_,
        "marker": marker,
        "canary_path": canary,
        "cases": cases,
    }
    _atomic_write(os.path.join(directory, "manifest.json"), json.dumps(manifest, indent=2))
    return manifest
