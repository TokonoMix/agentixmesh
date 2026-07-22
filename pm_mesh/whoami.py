"""``mesh-whoami`` — print THIS session's own mesh address, plainly.

A participant addresses others as ``uid:project`` but often does not know their OWN uid (it is the
numeric kernel user-id, not a memorable name). Guessing is error-prone — a mesh address typo silently
loses a message — and the skill used to hardcode ``1100`` as "the uid", which is exactly wrong for a
colleague on a different account (e.g. uid 1300, not 1100). This command removes the guess: run it,
read your address, share it.

The uid half is the kernel-verified identity (stable, unforgeable); the project half is just this
session's working-directory basename and changes when you ``cd`` elsewhere.
"""
from __future__ import annotations

from . import config


def address(cwd: str | None = None) -> str:
    """This session's own ``uid:project`` address (``cwd`` defaults to the process cwd)."""
    return config.current_address(cwd)


def render(cwd: str | None = None) -> str:
    """A short human block: the address plus what each half means and how others reach you."""
    addr = address(cwd)
    uid, project = config.parse_address(addr)
    return (
        f"your mesh address:  {addr}\n"
        f"  uid {uid}       = your kernel-verified identity (stable, unforgeable — not a guess)\n"
        f"  project '{project}' = this session's working-dir name (changes when you cd elsewhere)\n"
        f"\n"
        f"share it so others can reach you:  mesh-send {addr} \"...\""
    )


def main(argv=None) -> int:
    """Console entry: print the own address block. Always returns 0."""
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
