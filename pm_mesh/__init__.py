"""PM-Mesh — lightweight, file-based agent<->agent messaging (phase 1: same-user).

Design: see docs/ (multi-user design + phase-2 cross-user plan).
Phase 1 scope = same-user only: configurable root, 0700 owner-only maildir,
owner uid via fstat-on-fd, atomic-rename claim, cur/ janitor, replay guard,
DATA-frame sanitation, mesh-send, inject hook (opt-in, no auto-enable).
NO groups/roles/presence/trust levels/cross-user gates/setgid/sticky (= phase 2).
"""

__version__ = "0.1.0-dev"
