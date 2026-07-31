#!/usr/bin/env sh
# agentixmesh delivery BRIDGE for OpenClaw.
#
# OpenClaw has no stdout-capture hook like Claude Code and Codex. Its shipped external-injection
# path is a push command: `openclaw system event --text "<text>" --mode next-heartbeat` enqueues text
# the agent sees on its next heartbeat turn. So this adapter is a BRIDGE, not a hook: it polls the
# mesh and pushes any pending DATA frame into OpenClaw. Run it on a schedule (systemd timer / cron).
#
# Fail-closed throughout: exits 0 on any absence/error so it never disturbs anything.
#
# Delivery-durability caveat: mesh-inject marks a message seen when it renders it, so if the openclaw
# push below fails AFTER that, the message is not re-shown on the next poll. Acceptable for a
# monitoring/notify channel; do not use this bridge as the sole path for must-not-lose messages until
# a push-then-mark variant exists.

: "${MESH_CWD:=$HOME}"
: "${MESH_ROOT:=/srv/mesh}"
export MESH_CWD MESH_ROOT

command -v openclaw >/dev/null 2>&1 || exit 0   # no OpenClaw here → nothing to do

frame=$(python3 -m pm_mesh.inject 2>/dev/null) || exit 0
[ -z "$frame" ] && exit 0                        # nothing pending

# Pass the frame as a single quoted argv element — never let its content re-enter the shell.
# mesh-inject already sanitizes control/ANSI/zero-width chars in the frame body.
openclaw system event --text "$frame" --mode next-heartbeat >/dev/null 2>&1 || exit 0
