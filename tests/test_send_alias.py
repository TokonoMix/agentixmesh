"""mesh-send resolves an address-book alias before delivery (sender-side only)."""

from __future__ import annotations

from pm_mesh import send, maildir


def test_send_resolves_alias_to_canonical_address(monkeypatch, tmp_path):
    captured = {}

    def fake_deliver(msg):
        captured["to"] = msg.to
        return "/fake/path"

    monkeypatch.setattr(maildir, "deliver", fake_deliver)
    # current_address must be well-formed for message construction
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))

    rc = send.main(["reviewer", "hi"])
    assert rc == 0
    assert captured["to"] == "1200:reviews"   # 'reviewer' resolved via the book


def test_send_passes_bare_address_through(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(maildir, "deliver", lambda m: captured.setdefault("to", m.to))
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))

    rc = send.main(["1005:whatever", "hi"])
    assert rc == 0
    assert captured["to"] == "1005:whatever"


def test_send_rejects_unknown_non_address(monkeypatch, tmp_path):
    monkeypatch.setattr(maildir, "deliver", lambda m: (_ for _ in ()).throw(AssertionError("should not deliver")))
    monkeypatch.setenv("MESH_ROOT", str(tmp_path))

    rc = send.main(["totallyunknownname", "hi"])
    assert rc == 2   # not an address, not an alias → validation fails, no delivery
