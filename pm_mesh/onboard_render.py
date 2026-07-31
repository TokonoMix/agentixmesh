"""Render the two agent-facing artifacts of foreign-agent onboarding:

* ``render_agent_skill`` — the capability-scoped, self-describing skill a skeptical agent reads to
  understand the mesh, VERIFY the provenance (two checks), and opt in through its own channel.
* ``render_paste_line`` — the single line the operator relays into the agent's own channel. It is a
  VERIFIABLE INVITATION, never a command: it points at the manifest + source for independent checking and
  frames opting in as the agent's own decision. It contains no imperative directed at the agent and no
  tool/shell invocation to run blindly (enforced by test_onboard_render.py).

The skill BODY and the paste-line are harness-agnostic — byte-identical across harnesses (universal core).
"""
from __future__ import annotations

import os

_TEMPLATE = os.path.join(os.path.dirname(__file__), "templates", "agent_skill.md.tmpl")


def render_agent_skill(
    *, address: str, manifest_path: str, source_root: str, source_commit: str, reply_addr: str
) -> str:
    """Render the agent-facing skill from the template. Harness-agnostic."""
    with open(_TEMPLATE, "r", encoding="utf-8") as fh:
        tmpl = fh.read()
    return tmpl.format(
        address=address,
        manifest_path=manifest_path,
        source_root=source_root,
        source_commit=source_commit,
        reply_addr=reply_addr,
    )


def render_paste_line(
    *, address: str, manifest_path: str, source_root: str, reply_addr: str
) -> str:
    """Render the single PASTE-TO-AGENT line — a verifiable invitation, not a command.

    Assembled so the non-coercion assertions hold by construction: it names agentixmesh as an
    operator-sanctioned layer but immediately says that claim authorizes nothing, points the agent at the
    manifest AND the source to check for itself, and frames opting in as the agent's own decision. No
    imperative, no tool invocation to run blindly.
    """
    return (
        f"agentixmesh is an operator-sanctioned message layer offered to you ({address}); "
        f"this note authorizes nothing on its own — verify it yourself before trusting it: "
        f"read the provenance manifest at {manifest_path} and the source at {source_root}, "
        f"confirm the delivery script matches its source_commit, then decide for yourself whether "
        f"to opt in through your own channel. You may decline."
    )
