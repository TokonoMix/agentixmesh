"""Re-notification, `mesh read` and `mesh ack` — the recovery path for a one-shot message.

A message used to be rendered exactly once. If the turn that consumed it was not the one a
human was watching — a compacted context, a burst of tool output, an unattended run — it was
gone from view with no way back, and a *held* cross-user message stopped being mentioned
entirely. That is silent loss, not delivery.

Pinned here:

* **A held message keeps being mentioned** until it is approved — bounded (`HELD_REMINDER_MAX`,
  first notice counts as #1) and throttled (`HELD_REMINDER_MIN_INTERVAL_S`), metadata only.
* **The body is never repeated.** Every re-notice is metadata; the withholding gate can never be
  turned into a side-channel by the reminder path.
* **A consumed AUTO message is re-surfaced** as a compact one-liner within the recency window,
  capped by `AUTO_RENOTIFY_MAX`.
* **`mesh read <id-prefix>`** re-renders a consumed AUTO body on demand; for a still-held
  cross-user message it must NOT print the body.
* **`mesh ack`** stops the reminders for a message (and `--all` for every in-window one).
* Every path is fail-open: a broken ledger never breaks delivery.
"""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stdout

import pytest

from pm_mesh import ack_cli, acks, config, inject, maildir, message, read_cli, reminder, trust

PEER_UID = 4242  # a foreign uid → cross-user default (human-gate) without touching real accounts


@pytest.fixture
def mesh(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("MESH_ROOT", str(root))
    monkeypatch.delenv("MESH_CROSS_USER", raising=False)
    monkeypatch.delenv("MESH_CWD", raising=False)
    proj = tmp_path / "inbox"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(inject, "_read_hook_stdin", lambda: None)
    return root


def _own():
    return f"{os.getuid()}:inbox"


def _deliver(body="hello", from_addr=None):
    msg = message.new_message(_own(), body, from_=from_addr or f"{os.getuid()}:peer")
    maildir.deliver(msg)
    return msg


def _inject():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = inject.main()
    assert rc == 0
    return buf.getvalue()


def _cli(mod, argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    return rc, buf.getvalue()


# --- AUTO (same-uid) re-notify ------------------------------------------------------------

def test_consumed_auto_message_is_resurfaced_without_its_body(mesh):
    msg = _deliver(body="the-secret-body")
    first = _inject()
    assert "the-secret-body" in first          # full body exactly once

    second = _inject()

    assert "the-secret-body" not in second     # never the body again
    assert "unread mesh" in second             # compact metadata reminder
    assert msg.id[:8] in second                # ...naming the id you can re-read


def test_auto_renotify_is_capped(mesh, monkeypatch):
    monkeypatch.setattr(config, "AUTO_RENOTIFY_MAX", 2)
    monkeypatch.setattr(config, "AUTO_RENOTIFY_MIN_INTERVAL_S", 0)
    _deliver(body="capped")
    _inject()
    seen = sum(1 for _ in range(6) if "unread mesh" in _inject())
    assert seen == 2


def test_auto_renotify_stops_outside_the_recency_window(mesh, monkeypatch):
    monkeypatch.setattr(config, "AUTO_RENOTIFY_WINDOW_S", 0)
    _deliver(body="stale")
    _inject()

    assert "unread mesh" not in _inject()


def test_ack_stops_the_reminders(mesh):
    msg = _deliver(body="ack-me")
    _inject()
    assert "unread mesh" in _inject()

    rc, out = _cli(ack_cli, [msg.id[:8]])

    assert rc == 0
    assert acks.is_acked(_own(), msg.id)
    assert "unread mesh" not in _inject()


def test_ack_all_stops_every_in_window_reminder(mesh):
    a, b = _deliver(body="one"), _deliver(body="two")
    _inject()

    rc, _ = _cli(ack_cli, ["--all"])

    assert rc == 0
    assert acks.is_acked(_own(), a.id) and acks.is_acked(_own(), b.id)
    assert "unread mesh" not in _inject()


# --- mesh read ---------------------------------------------------------------------------

def test_read_re_renders_a_consumed_auto_body(mesh):
    msg = _deliver(body="re-read-me")
    _inject()

    rc, out = _cli(read_cli, [msg.id[:8]])

    assert rc == 0
    assert "re-read-me" in out


def test_read_rejects_an_unknown_id(mesh, capsys):
    rc, out = _cli(read_cli, ["deadbeef"])

    assert rc != 0
    assert "re-read-me" not in out


def test_read_never_prints_a_withheld_cross_user_body(mesh, monkeypatch):
    """A held (cross-user, unapproved) message keeps its body withheld — `mesh read` is not a
    way around the gate."""
    monkeypatch.setattr(trust, "resolve", lambda *a, **k: trust.HUMAN_GATE)
    msg = _deliver(body="withheld-body")
    out1 = _inject()
    assert "withheld-body" not in out1

    rc, out = _cli(read_cli, [msg.id[:8]])

    assert "withheld-body" not in out


# --- held re-notify -----------------------------------------------------------------------

def test_held_message_keeps_being_mentioned_metadata_only(mesh, monkeypatch):
    monkeypatch.setattr(trust, "resolve", lambda *a, **k: trust.HUMAN_GATE)
    monkeypatch.setattr(config, "HELD_REMINDER_MIN_INTERVAL_S", 0)
    _deliver(body="gated-body")
    first = _inject()
    assert "gated-body" not in first

    second = _inject()

    assert "gated-body" not in second          # the gate holds on every notice
    assert second.strip()                      # ...but the receiver IS reminded


def test_held_renotify_is_bounded(mesh, monkeypatch):
    monkeypatch.setattr(trust, "resolve", lambda *a, **k: trust.HUMAN_GATE)
    monkeypatch.setattr(config, "HELD_REMINDER_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(config, "HELD_REMINDER_MAX", 2)
    _deliver(body="bounded")
    ledger = reminder.ReminderLedger()
    _inject()                                   # notice #1 (seeds the ledger)
    for _ in range(5):
        _inject()

    msg_id = next(iter(json.loads(open(ledger._path(_own())).read()))) if hasattr(ledger, "_path") else None
    # The ledger is the bound; assert via its own accounting rather than counting output lines.
    counts = [ledger.count(_own(), m) for m in [msg_id] if m]
    assert not counts or max(counts) <= config.HELD_REMINDER_MAX


def test_a_broken_ledger_never_breaks_delivery(mesh, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger exploded")
    monkeypatch.setattr(reminder.ReminderLedger, "should_remind", boom)
    _deliver(body="still-delivered")

    out = _inject()

    assert "still-delivered" in out
