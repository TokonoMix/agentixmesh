"""Address book — friendly names/aliases → canonical ``uid:project`` addresses.

Motivation: humans (and the agents acting for them) refer to a peer many ways —
"reviewer", "peer", "bob's reviewer" all mean the same mailbox. Without one book
that is confusing and error-prone (a typo silently loses a message). This module
resolves any known alias, display name, or a bare address to the canonical
``uid:project`` string, and maps an address to its on-disk project directory.

**Trust boundary — read this.** The address book is *sender-side convenience
only*. It changes how a name is turned into an address before sending; it does
NOT touch the receive-side identity, which stays kernel-verified (fstat on the
open fd). An alias can never forge *who a message is from* — at worst a wrong
alias sends to the wrong (real, kernel-owned) mailbox, exactly like a mistyped
address today. So the book is untrusted metadata: convenient, not authoritative.

Layered load (later layers extend/override earlier ones), all optional:
  1. bundled seed   : ``<repo>/data/addressbook.json``
  2. shared team book: ``$MESH_ROOT/addressbook.json`` (cross-user consistency)
  3. personal book   : ``~/.config/pm-mesh/addressbook.json`` (your own aliases win)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import config

_ADDRESS_RE = re.compile(r"^\d+:[A-Za-z0-9._-]+$")


@dataclass
class Entry:
    address: str
    display: str = ""
    dir: str = ""
    aliases: list[str] = field(default_factory=list)


def _norm(name: str) -> str:
    return name.strip().lower()


def _seed_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "addressbook.json")


def _layer_paths(mesh_root: str | None) -> list[str]:
    root = mesh_root if mesh_root is not None else _safe_mesh_root()
    home = os.path.expanduser("~")
    return [
        _seed_path(),
        os.path.join(root, "addressbook.json") if root else "",
        os.path.join(home, ".config", "pm-mesh", "addressbook.json"),
    ]


def _safe_mesh_root() -> str:
    try:
        return config.mesh_root()
    except Exception:
        return os.environ.get("MESH_ROOT", "")


def _load_file(path: str) -> dict:
    """Return the raw layer ``{"entries": [...], "languages": {...}}``. Both keys defaulted so a
    missing/typed-wrong value degrades to empty rather than raising — and a bad ``languages`` block
    never discards the file's ``entries`` (they are read independently)."""
    empty = {"entries": [], "languages": {}}
    if not path or not os.path.isfile(path):
        return empty
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    entries = data.get("entries", [])
    languages = data.get("languages", {})
    return {"entries": entries if isinstance(entries, list) else [],
            "languages": languages if isinstance(languages, dict) else {}}


class AddressBook:
    """Resolved, merged view of the layered address-book files."""

    def __init__(self, entries: dict[str, Entry], languages: dict[str, list[str]] | None = None):
        self._by_address = entries
        self._languages = languages or {}
        self._alias_index: dict[str, str] = {}
        for addr, e in entries.items():
            self._alias_index[_norm(addr)] = addr
            if e.display:
                self._alias_index.setdefault(_norm(e.display), addr)
            for a in e.aliases:
                self._alias_index[_norm(a)] = addr  # later layers already won during merge

    # ---- lookups -------------------------------------------------------
    def resolve(self, name: str) -> str | None:
        """Return the canonical ``uid:project`` for a bare address, alias or
        display name; ``None`` if unknown. A well-formed address passes through
        even if it is not in the book (the book is convenience, not a gate)."""
        if name is None:
            return None
        raw = name.strip()
        if _ADDRESS_RE.match(raw):
            return raw
        return self._alias_index.get(_norm(raw))

    def dir_for(self, address: str) -> str | None:
        e = self._by_address.get(address)
        return e.dir or None if e else None

    def display_for(self, address: str) -> str | None:
        e = self._by_address.get(address)
        return e.display or None if e else None

    def entries(self) -> list[Entry]:
        return list(self._by_address.values())

    def languages_for(self, uid: int | str) -> list[str]:
        """Ordered language codes for a person (uid), or ``[]`` if unknown. Policy-free: the
        'unknown -> English' default is the caller's, so ``[]`` stays distinct from ``['en']``."""
        return list(self._languages.get(str(uid), []))


def merge_entries(layers: list[list[dict]]) -> dict[str, Entry]:
    """Merge raw entry-lists from low to high priority. Same address across
    layers merges field-wise (higher layer wins for scalars; aliases union)."""
    out: dict[str, Entry] = {}
    for layer in layers:
        for raw in layer:
            addr = (raw.get("address") or "").strip()
            if not _ADDRESS_RE.match(addr):
                continue
            e = out.get(addr) or Entry(address=addr)
            if raw.get("display"):
                e.display = raw["display"]
            if raw.get("dir"):
                e.dir = raw["dir"]
            for a in raw.get("aliases", []) or []:
                if isinstance(a, str) and _norm(a) not in {_norm(x) for x in e.aliases}:
                    e.aliases.append(a)
            out[addr] = e
    return out


def merge_languages(layers: list[dict]) -> dict[str, list[str]]:
    """Merge per-uid ordered language lists low->high. A higher layer's NON-EMPTY list REPLACES the
    uid's list (order is meaningful — no union). An empty/malformed value is ignored (kept from the
    lower layer), never a replace-with-empty. Keys canonicalised to ``str``; codes lowercased/trimmed."""
    out: dict[str, list[str]] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for uid, langs in layer.items():
            if not isinstance(langs, list):
                continue
            clean = [c.strip().lower() for c in langs if isinstance(c, str) and c.strip()]
            if clean:
                out[str(uid)] = clean
    return out


def load(mesh_root: str | None = None) -> AddressBook:
    raw = [_load_file(p) for p in _layer_paths(mesh_root)]
    entries = merge_entries([r["entries"] for r in raw])
    languages = merge_languages([r["languages"] for r in raw])
    return AddressBook(entries, languages)


def upsert_entry(address, *, display="", dir="", aliases=None, note="",
                 book_path):
    """Add or extend one entry in the book file at ``book_path``, in place.

    Merge-never-overwrite: an upsert can only ADD information. A new address is
    appended; an existing one gets its EMPTY scalar fields filled and its alias
    list unioned (case-insensitive, existing order first) — a non-empty
    ``display``/``dir``/``$note`` a steward already wrote is never replaced. This
    mirrors the read-side layering (a later layer extends, it never elevates) and
    keeps the book sender-side convenience only: nothing here touches receive-side
    identity, which stays kernel-verified.

    Returns ``{"created": bool, "updated_fields": [...]}``. Raises ``ValueError``
    on a malformed address or an unparseable existing file, leaving the file
    byte-for-byte untouched (a human inspects a corrupt book; we never clobber it).
    """
    address = (address or "").strip()
    if not _ADDRESS_RE.match(address):
        raise ValueError(f"malformed address: {address!r} (want <uid>:<project>)")
    aliases = list(aliases or [])

    if os.path.isfile(book_path):
        try:
            with open(book_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            raise ValueError(f"cannot parse existing book {book_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"existing book {book_path} is not a JSON object")
    else:
        data = {}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
        data["entries"] = entries

    existing = None
    for e in entries:
        if isinstance(e, dict) and (e.get("address") or "").strip() == address:
            existing = e
            break

    updated_fields = []
    if existing is None:
        entry = {"address": address}
        if display:
            entry["display"] = display
        if dir:
            entry["dir"] = dir
        if note:
            entry["$note"] = note
        entry["aliases"] = _dedup_aliases([], aliases)
        entries.append(entry)
        created = True
    else:
        created = False
        if display and not (existing.get("display") or "").strip():
            existing["display"] = display
            updated_fields.append("display")
        if dir and not (existing.get("dir") or "").strip():
            existing["dir"] = dir
            updated_fields.append("dir")
        if note and not (existing.get("$note") or "").strip():
            existing["$note"] = note
            updated_fields.append("$note")
        merged = _dedup_aliases(existing.get("aliases") or [], aliases)
        if merged != (existing.get("aliases") or []):
            existing["aliases"] = merged
            updated_fields.append("aliases")

    tmp = book_path + ".tmp"
    os.makedirs(os.path.dirname(book_path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, book_path)
    return {"created": created, "updated_fields": updated_fields}


def _dedup_aliases(existing, incoming):
    """Union, case-insensitive, existing order first."""
    out = list(existing)
    seen = {_norm(a) for a in out if isinstance(a, str)}
    for a in incoming:
        if isinstance(a, str) and _norm(a) not in seen:
            out.append(a)
            seen.add(_norm(a))
    return out
