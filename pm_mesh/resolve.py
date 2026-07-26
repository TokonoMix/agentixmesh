"""``python3 -m pm_mesh.resolve <name>`` — resolve an alias/name to uid:project.

Prints the canonical address on stdout (exit 0) or an error on stderr (exit 1).
With ``--list`` it prints the whole book. Sender-side convenience only — see
``pm_mesh.addressbook`` for the trust note.
"""

from __future__ import annotations

import sys

from . import addressbook


def _uid_of(address: str) -> str:
    return address.split(":", 1)[0]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    book = addressbook.load()

    if not args or args[0] in ("-h", "--help"):
        print("usage: mesh-resolve <alias-or-address> | --list", file=sys.stderr)
        return 0 if args else 1

    if args[0] == "--list":
        for e in sorted(book.entries(), key=lambda x: x.address):
            aliases = "[" + ", ".join(e.aliases) + "]"
            langs = ",".join(book.languages_for(_uid_of(e.address)))
            print(f"{e.address:40s} {e.display or '-':22s} {aliases:34s} {langs}")
        return 0

    addr = book.resolve(args[0])
    if addr is None:
        print(f"mesh-resolve: unknown name/alias: {args[0]!r} "
              f"(mesh-resolve --list shows the book)", file=sys.stderr)
        return 1
    print(addr)  # stdout — bare address, machine-readable (unchanged contract)
    langs = book.languages_for(_uid_of(addr))
    if langs:
        print(f"prefers: {', '.join(langs)} -> write in {langs[0]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
