"""mesh-whoami also shows YOUR OWN languages, so you know how others should address you.

Reads the bundled repo seed (uid 1200 -> ["en", "es"]) via a real MESH_ROOT override so a
malformed/absent shared or personal book cannot break this. See pm_mesh/whoami.py.
"""
from __future__ import annotations

from pm_mesh import whoami, config


def test_whoami_render_includes_own_languages(monkeypatch):
    monkeypatch.setenv("MESH_ROOT", "/nonexistent-root-so-only-seed-loads")
    monkeypatch.setattr(config, "current_address", lambda cwd=None: "1200:reviews")
    block = whoami.render()
    assert "en" in block and "es" in block      # own languages shown
    assert "languages" in block.lower()


def test_whoami_render_omits_line_when_no_language_data(monkeypatch):
    monkeypatch.setenv("MESH_ROOT", "/nonexistent-root-so-only-seed-loads")
    monkeypatch.setattr(config, "current_address", lambda cwd=None: "9999:nowhere")
    block = whoami.render()
    assert "languages (how others" not in block  # no phantom empty line


def test_whoami_render_never_breaks_on_a_bad_book(monkeypatch, tmp_path):
    """The one command whose promise is 'never guess' must not traceback on a corrupt book."""
    bad_root = tmp_path / "meshroot"
    bad_root.mkdir()
    (bad_root / "addressbook.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("MESH_ROOT", str(bad_root))
    monkeypatch.setattr(config, "current_address", lambda cwd=None: "1200:reviews")
    block = whoami.render()  # must not raise
    assert "your mesh address" in block
