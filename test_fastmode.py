"""Fast-mode (snel-modus) per-address state — prompt-free persistence.

Verifies: per-address isolation (no shared ~/.claude clobber), the ladder, fail-safe defaults, a
malformed address can't escape the dir, and the `mesh-poll fastmode` CLI round-trips. See
pm_mesh/fastmode.py.
"""

import os

import pytest

from pm_mesh import config, fastmode, poll


def test_save_load_roundtrip(tmp_path):
    home = str(tmp_path)
    rec = fastmode.save("1100:backend", mode="on", step=2, now=1000.0, note="busy", home=home)
    assert rec["mode"] == "on" and rec["step"] == 2 and rec["interval_s"] == 900
    got = fastmode.load("1100:backend", home=home)
    assert got["step"] == 2 and got["interval_s"] == 900 and got["note"] == "busy"


def test_interval_ladder():
    assert fastmode.interval_for_step(1) == 300
    assert fastmode.interval_for_step(4) == 3600
    assert fastmode.interval_for_step(9) == 3600   # clamp to the floor
    assert fastmode.interval_for_step(0) == 0        # off


def test_per_address_isolation(tmp_path):
    home = str(tmp_path)
    fastmode.save("1100:backend", mode="on", step=1, now=1.0, home=home)
    fastmode.save("1100:frontend", mode="on", step=4, now=1.0, home=home)
    # A single shared state file would let these clobber each other; per-address must not.
    assert fastmode.load("1100:backend", home=home)["step"] == 1
    assert fastmode.load("1100:frontend", home=home)["step"] == 4


def test_missing_and_corrupt_failsafe(tmp_path):
    home = str(tmp_path)
    assert fastmode.load("1100:x", home=home)["mode"] == "off"     # missing -> off
    p = fastmode.state_path("1100:x", home=home)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    assert fastmode.load("1100:x", home=home)["mode"] == "off"     # corrupt -> off (never raises)


def test_malformed_address_cannot_escape(tmp_path):
    home = str(tmp_path)
    with pytest.raises(ValueError):
        fastmode.state_path("../../etc/passwd", home=home)
    assert fastmode.load("../../etc/passwd", home=home)["mode"] == "off"  # load stays fail-safe


def test_off_stamps_deactivated(tmp_path):
    home = str(tmp_path)
    rec = fastmode.save("1100:x", mode="off", step=0, now=5.0, home=home)
    assert rec["mode"] == "off" and rec["step"] == 0 and rec["interval_s"] == 0
    assert rec["deactivated_at_utc"] is not None and rec["activated_at_utc"] is None


def test_cli_set_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(fastmode, "_data_dir", lambda home=None: str(tmp_path / "fm"))
    monkeypatch.setattr(config, "current_address", lambda: "1100:backend")
    assert poll.main(["fastmode", "set", "--step", "3"]) == 0
    got = fastmode.load("1100:backend")
    assert got["step"] == 3 and got["interval_s"] == 1800 and got["mode"] == "on"
    assert poll.main(["fastmode", "off"]) == 0
    assert fastmode.load("1100:backend")["mode"] == "off"


def test_cli_unknown_command_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        poll.main(["on"])
    assert exc.value.code == 2
