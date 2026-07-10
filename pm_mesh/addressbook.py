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


def _load_file(path: str) -> list[dict]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return entries if isinstance(entries, list) else []


class AddressBook:
    """Resolved, merged view of the layered address-book files."""

    def __init__(self, entries: dict[str, Entry]):
        self._by_address = entries
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


def load(mesh_root: str | None = None) -> AddressBook:
    layers = [_load_file(p) for p in _layer_paths(mesh_root)]
    return AddressBook(merge_entries(layers))
