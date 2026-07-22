"""Tests for the address-book resolver (pm_mesh.addressbook)."""

from __future__ import annotations

import json
import os

import pytest

from pm_mesh import addressbook as ab


def _book(entries):
    return ab.AddressBook(ab.merge_entries([entries]))


def test_resolve_alias_display_and_bare_address():
    book = _book([
        {"address": "1200:reviews", "display": "Reviewer (colleague account)",
         "dir": "/x", "aliases": ["reviewer", "peer", "bobs-reviewer"]},
    ])
    assert book.resolve("reviewer") == "1200:reviews"
    assert book.resolve("PEER") == "1200:reviews"           # case-insensitive
    assert book.resolve("  bobs-reviewer ") == "1200:reviews"  # trimmed
    assert book.resolve("Reviewer (colleague account)") == "1200:reviews"    # display name
    assert book.resolve("1200:reviews") == "1200:reviews"        # bare address


def test_unknown_alias_is_none_but_wellformed_address_passes_through():
    book = _book([{"address": "1100:projects", "aliases": ["alice"]}])
    assert book.resolve("nobody") is None
    # a valid-looking address not in the book still resolves to itself
    assert book.resolve("1005:whatever") == "1005:whatever"
    # a bare word that is not an alias is not an address
    assert book.resolve("whatever") is None


def test_dir_and_display_lookup():
    book = _book([
        {"address": "1100:agentixmesh-web", "display": "agentixmesh.ai site",
         "dir": "/home/user/agentixmesh-web",
         "aliases": ["agentixmesh", "agentixmesh.ai"]},
    ])
    assert book.resolve("agentixmesh.ai") == "1100:agentixmesh-web"
    assert book.dir_for("1100:agentixmesh-web").endswith("agentixmesh-web")
    assert book.display_for("1100:agentixmesh-web") == "agentixmesh.ai site"
    assert book.dir_for("1100:unknown") is None


def test_merge_later_layer_wins_and_aliases_union():
    base = [{"address": "1200:reviews", "display": "old", "aliases": ["peer"]}]
    shared = [{"address": "1200:reviews", "display": "Reviewer", "aliases": ["reviewer"]}]
    book = ab.AddressBook(ab.merge_entries([base, shared]))
    assert book.display_for("1200:reviews") == "Reviewer"       # higher layer wins
    assert book.resolve("peer") == "1200:reviews"              # base alias kept
    assert book.resolve("reviewer") == "1200:reviews"           # new alias added


def test_malformed_addresses_are_dropped():
    book = ab.AddressBook(ab.merge_entries([[
        {"address": "not-an-address", "aliases": ["x"]},
        {"address": "1100:ok", "aliases": ["good"]},
    ]]))
    assert book.resolve("x") is None
    assert book.resolve("good") == "1100:ok"


def test_bundled_seed_loads_and_resolves_examples():
    # the shipped data/addressbook.json must resolve its example aliases
    book = ab.load(mesh_root="/nonexistent-root-so-only-seed-loads")
    assert book.resolve("backend") == "1100:backend"
    assert book.resolve("reviews") == "1200:reviews"
    assert book.resolve("reviewer") == "1200:reviews"
    # and the dir mapping the bare address could not do:
    assert book.dir_for("1100:backend").endswith("backend")


def test_personal_layer_overrides(tmp_path, monkeypatch):
    personal_dir = tmp_path / ".config" / "pm-mesh"
    personal_dir.mkdir(parents=True)
    (personal_dir / "addressbook.json").write_text(json.dumps({
        "entries": [{"address": "1200:reviews", "aliases": ["neighbor"]}]
    }))
    monkeypatch.setenv("HOME", str(tmp_path))
    book = ab.load(mesh_root="/nonexistent")
    assert book.resolve("neighbor") == "1200:reviews"     # personal alias added
    assert book.resolve("reviews") == "1200:reviews"      # seed alias still there
