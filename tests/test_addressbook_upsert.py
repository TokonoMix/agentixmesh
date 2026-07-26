"""Write-side of the address book: upsert_entry + the addressbook_add CLI.

Enroll/onboarding flows can write the uid:project entry automatically, with
merge-never-overwrite semantics — an upsert can only ADD information (fill empty
fields, union aliases), never replace what a steward already wrote. The book stays
sender-side convenience: nothing here touches receive-side identity.
"""

import json

import pytest

from pm_mesh import addressbook


def _write_book(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _seed(tmp_path):
    book = tmp_path / "addressbook.json"
    _write_book(book, {
        "$comment": "team book",
        "languages": {"1100": ["en"]},
        "entries": [
            {"address": "1200:reviews", "display": "Reviewer", "dir": "/home/bob/reviews",
             "$note": "existing note", "aliases": ["reviewer", "peer"]},
        ],
    })
    return book


def test_upsert_creates_new_entry_and_preserves_book(tmp_path):
    book = _seed(tmp_path)
    res = addressbook.upsert_entry(
        "1200:staging", display="Reviewer (staging clone)",
        dir="/home/bob/dev/staging", aliases=["staging-reviewer"],
        note="auto-added by enroll", book_path=str(book))
    assert res["created"] is True
    data = json.loads(book.read_text(encoding="utf-8"))
    # existing content untouched
    assert data["$comment"] == "team book"
    assert data["languages"] == {"1100": ["en"]}
    assert data["entries"][0]["display"] == "Reviewer"
    new = [e for e in data["entries"] if e["address"] == "1200:staging"]
    assert len(new) == 1
    assert new[0]["display"] == "Reviewer (staging clone)"
    assert new[0]["dir"] == "/home/bob/dev/staging"
    assert new[0]["aliases"] == ["staging-reviewer"]
    assert new[0]["$note"] == "auto-added by enroll"


def test_upsert_never_overwrites_existing_fields(tmp_path):
    book = _seed(tmp_path)
    res = addressbook.upsert_entry(
        "1200:reviews", display="SOMETHING ELSE", dir="/other/dir",
        aliases=["REVIEWER", "colleague"], note="new note", book_path=str(book))
    assert res["created"] is False
    e = json.loads(book.read_text(encoding="utf-8"))["entries"][0]
    # non-empty scalars kept, $note kept
    assert e["display"] == "Reviewer"
    assert e["dir"] == "/home/bob/reviews"
    assert e["$note"] == "existing note"
    # aliases union, case-insensitive dedup, existing order first
    assert e["aliases"] == ["reviewer", "peer", "colleague"]


def test_upsert_fills_only_empty_fields(tmp_path):
    book = tmp_path / "addressbook.json"
    _write_book(book, {"entries": [{"address": "1300:backend", "display": "", "aliases": []}]})
    res = addressbook.upsert_entry("1300:backend", display="Backend", dir="/home/carol/backend",
                                   book_path=str(book))
    assert res["created"] is False
    assert sorted(res["updated_fields"]) == ["dir", "display"]
    e = json.loads(book.read_text(encoding="utf-8"))["entries"][0]
    assert e["display"] == "Backend"
    assert e["dir"] == "/home/carol/backend"


def test_upsert_rejects_malformed_address_and_leaves_book_untouched(tmp_path):
    book = _seed(tmp_path)
    before = book.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        addressbook.upsert_entry("not-an-address", display="x", book_path=str(book))
    assert book.read_text(encoding="utf-8") == before


def test_upsert_creates_book_when_missing(tmp_path):
    book = tmp_path / "sub" / "addressbook.json"
    res = addressbook.upsert_entry("1300:tools", display="Tools clone",
                                   book_path=str(book))
    assert res["created"] is True
    data = json.loads(book.read_text(encoding="utf-8"))
    assert data["entries"][0]["address"] == "1300:tools"


def test_upsert_survives_corrupt_book_without_destroying_it(tmp_path):
    book = tmp_path / "addressbook.json"
    book.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        addressbook.upsert_entry("1300:x", display="X", book_path=str(book))
    # corrupt file left in place for a human to inspect — never clobbered
    assert book.read_text(encoding="utf-8") == "{not json"


def test_cli_adds_entry_and_exits_zero(tmp_path, capsys):
    from pm_mesh import addressbook_add
    book = _seed(tmp_path)
    rc = addressbook_add.main([
        "1200:staging", "--display", "Reviewer (staging clone)",
        "--dir", "/home/bob/dev/staging", "--alias", "staging-reviewer",
        "--note", "via enroll", "--book", str(book)])
    assert rc == 0
    data = json.loads(book.read_text(encoding="utf-8"))
    assert any(e["address"] == "1200:staging" for e in data["entries"])
    assert "created" in capsys.readouterr().out


def test_cli_rejects_bad_address_with_exit_2(tmp_path, capsys):
    from pm_mesh import addressbook_add
    book = _seed(tmp_path)
    rc = addressbook_add.main(["nope", "--book", str(book)])
    assert rc == 2
    assert "address" in capsys.readouterr().err.lower()
