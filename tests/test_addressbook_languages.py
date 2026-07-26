"""Per-uid language preferences in the address book (mesh language-routing).

Language is per PERSON (uid), not per uid:project — so it lives in a top-level ``languages`` map
merged across the same 3 layers as entries, exposed via the policy-free ``languages_for`` accessor.
"""
from __future__ import annotations

from pm_mesh import addressbook as ab
from pm_mesh.addressbook import merge_languages, AddressBook


def test_languages_for_known_uid_ordered():
    book = AddressBook({}, {"1200": ["en", "es"]})
    assert book.languages_for("1200") == ["en", "es"]


def test_languages_for_unknown_is_empty():
    assert AddressBook({}, {}).languages_for("9999") == []


def test_languages_for_int_and_str_uid_parity():
    book = AddressBook({}, {"1100": ["en"]})
    assert book.languages_for(1100) == ["en"] == book.languages_for("1100")


def test_merge_higher_layer_replaces():
    merged = merge_languages([{"1200": ["es", "en"]}, {"1200": ["en"]}])
    assert merged["1200"] == ["en"]  # replace, not union


def test_merge_absent_uid_keeps_lower():
    merged = merge_languages([{"1100": ["en"]}, {"1200": ["es"]}])
    assert merged["1100"] == ["en"] and merged["1200"] == ["es"]


def test_merge_empty_or_malformed_is_ignored_not_replace():
    assert merge_languages([{"1200": ["es", "en"]}, {"1200": []}])["1200"] == ["es", "en"]
    assert merge_languages([{"1200": ["es", "en"]}, {"1200": "en"}])["1200"] == ["es", "en"]


def test_merge_lowercases_and_trims_codes():
    assert merge_languages([{"1100": [" EN ", "Es"]}])["1100"] == ["en", "es"]


def test_malformed_languages_does_not_drop_entries(tmp_path):
    p = tmp_path / "addressbook.json"
    p.write_text('{"entries":[{"address":"1200:x","aliases":["reviewer"]}],"languages":"broken"}')
    raw = ab._load_file(str(p))
    assert raw["entries"] and raw["languages"] == {}  # entries survive a bad languages block


def test_seed_languages_resolve_from_repo_book():
    book = ab.load(mesh_root="/nonexistent-root-so-only-seed-loads")  # reads the bundled repo seed
    assert book.languages_for("1200") == ["en", "es"]
    assert book.languages_for("1100") == ["en"]
    assert book.languages_for("9999") == []           # unknown uid stays empty, not a default
