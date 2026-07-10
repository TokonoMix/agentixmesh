"""XU-RT-04 (red-team) — replay + thread-cap + dedup integrity.

Adversarial tests that ATTEMPT to break the replay/dedup invariants (ontwerp §18, security-review
f2-16 #1) and MUST fail to do so against current code:

1. Straight replay: resubmit the exact same ``id``+``ts_utc`` → must be deduped (not shown twice).
2. Dedup-escape attempt: attacker forges ``msg.to`` on the replayed copy to a DIFFERENT address, but
   the file is physically placed in the SAME victim maildrop → dedup must key on the kernel-verified
   receive address (``config.current_address()``), not on the attacker-controlled ``msg.to`` field —
   so it must still be blocked.
3. Age-floor bypass attempt: a far-future ``ts_utc`` (negative age) must NOT be treated as fresh —
   it must not be shown (``replay.MAX_FUTURE_SKEW_S`` clamps this).
4. Thread turn-cap: flooding one thread past ``config.MAX_TURNS_PER_THREAD`` must trigger the
   advisory (it is fail-open/log-only by design — not enforcement — so all messages still render;
   the assertion is that the cap detection fires, not that delivery is suppressed).

These drive the FULL pipeline (``maildir.deliver`` → ``inject.main``), not just unit calls into
``replay.SeenStore``, to prove the invariant holds end-to-end through the hook entrypoint that a real
attacker would actually be constrained by. Style follows ``test_turn_cap.py`` (as_address harness).

STRICT: read-only w.r.t. production code — this file only adds tests.
"""

import io
import os
import secrets
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from pm_mesh import config, inject, maildir, message

UID = 1000
ADDR = f"{UID}:redteam"
ATTACKER_ADDR = "9999:elsewhere"


@contextmanager
def as_address(root):
    with mock.patch("os.getuid", return_value=UID), \
         mock.patch("os.getcwd", return_value="/home/user/redteam"), \
         mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
        yield


def _run_inject(root):
    out, err = io.StringIO(), io.StringIO()
    with as_address(root):
        with redirect_stdout(out), redirect_stderr(err):
            rc = inject.main()
    return rc, out.getvalue(), err.getvalue()


def _make(to, body, ts=None, mid=None, thread=None):
    msg = message.new_message(to=to, body=body, thread=thread, from_=f"{UID}:peer")
    if ts is not None:
        msg.ts_utc = ts
    if mid is not None:
        msg.id = mid
    return msg


def _drop_raw(root, address, msg):
    """Physically place ``msg`` in ``address``'s ``new/`` — bypasses ``maildir.deliver``'s use of
    ``msg.to`` to pick the directory, so a body-forged ``to`` can land in a mailbox it does not
    claim. Models an attacker who already has drop-capability into the victim's real inbox (the
    threat ``is_fresh(..., address=...)`` is designed against) rather than one who can only reach
    their own mailbox via the public ``deliver()`` API."""
    drop = maildir.maildrop(address, root=root)
    new_dir = os.path.join(drop, "new")
    data = message.to_json(msg).encode("utf-8")
    name = secrets.token_hex(16)
    path = os.path.join(new_dir, name)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


class ReplayAttemptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_attack_1_exact_replay_is_blocked(self):
        m = _make(ADDR, "secret instruction body xu-rt-04-a1")
        maildir.deliver(m, root=self.root)
        rc1, out1, _ = _run_inject(self.root)
        self.assertEqual(rc1, 0)
        self.assertIn("secret instruction body xu-rt-04-a1", out1)  # shown once

        # Attacker resubmits the exact same id+ts_utc (a captured/replayed copy).
        maildir.deliver(m, root=self.root)
        rc2, out2, _ = _run_inject(self.root)
        self.assertEqual(rc2, 0)
        self.assertNotIn("secret instruction body xu-rt-04-a1", out2)  # NOT shown again — deduped

    def test_attack_2_forged_to_does_not_escape_dedup(self):
        # Legit delivery + consumption first, so the id is marked seen under the VICTIM's verified
        # receive address.
        m1 = _make(ADDR, "original body xu-rt-04-a2", mid="dup-id-xu-rt-04")
        maildir.deliver(m1, root=self.root)
        rc1, out1, _ = _run_inject(self.root)
        self.assertEqual(rc1, 0)
        self.assertIn("original body xu-rt-04-a2", out1)

        # Attacker crafts a second copy with the SAME id/ts but a DIFFERENT `to`, and drops it
        # physically into the victim's own new/ (the field dedup must NOT trust).
        m2 = _make(ATTACKER_ADDR, "REPLAYED PAYLOAD — should be blocked",
                    ts=m1.ts_utc, mid="dup-id-xu-rt-04")
        _drop_raw(self.root, ADDR, m2)

        rc2, out2, _ = _run_inject(self.root)
        self.assertEqual(rc2, 0)
        # If dedup ever keyed on msg.to instead of the verified receive address, this payload would
        # show up here a second time under a different `to` — it must not.
        self.assertNotIn("REPLAYED PAYLOAD", out2)

    def test_attack_3_far_future_ts_does_not_bypass_age_floor(self):
        future = datetime.now(timezone.utc) + timedelta(days=10)
        m = _make(ADDR, "from the future — should be rejected",
                  ts=future.strftime("%Y-%m-%dT%H:%M:%SZ"))
        maildir.deliver(m, root=self.root)
        rc, out, _ = _run_inject(self.root)
        self.assertEqual(rc, 0)
        # A future ts drives age negative; without the MAX_FUTURE_SKEW_S clamp `now - ts < max_age_s`
        # is trivially true forever, i.e. always "fresh". It must instead be treated as not-fresh and
        # therefore not rendered.
        self.assertNotIn("from the future", out)

        # And it must not linger forever either: a second run must not surface it now (it was
        # consumed/mark_shown'd, so the janitor won't keep re-offering it as an orphan).
        rc2, out2, _ = _run_inject(self.root)
        self.assertEqual(rc2, 0)
        self.assertNotIn("from the future", out2)


class ThreadCapAttackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_attack_4_ping_loop_flood_trips_advisory_cap(self):
        thread = "flood-thread"
        n = config.MAX_TURNS_PER_THREAD + 5
        for i in range(n):
            msg = _make(ADDR, f"flood turn {i}", thread=thread)
            maildir.deliver(msg, root=self.root)
        rc, out, err = _run_inject(self.root)
        self.assertEqual(rc, 0)
        # Advisory-only design (ontwerp §8): flooding does not get silently absorbed — the cap
        # detection must fire on stderr once the thread crosses MAX_TURNS_PER_THREAD.
        self.assertIn(thread, err)
        self.assertIn(str(config.MAX_TURNS_PER_THREAD), err)
        self.assertIn("ping-loop", err)

    def test_attack_4b_flood_across_many_runs_still_trips_cap(self):
        # Attacker paces the flood across separate inject turns (below MAX_MESSAGES_PER_TURN each
        # time) to try to stay under the radar; the per-thread counter is persistent so this must
        # not evade detection either.
        thread = "paced-flood"
        for _ in range(config.MAX_TURNS_PER_THREAD):
            maildir.deliver(_make(ADDR, "turn", thread=thread), root=self.root)
        rc, out, err = _run_inject(self.root)
        self.assertEqual(err, "")  # exactly at the cap: no advisory yet

        maildir.deliver(_make(ADDR, "one more turn", thread=thread), root=self.root)
        rc2, out2, err2 = _run_inject(self.root)
        self.assertIn(thread, err2)
        self.assertIn("ping-loop", err2)


if __name__ == "__main__":
    unittest.main()
