# Scaling beyond one machine

agentixmesh is deliberately small: a **same-user, single-machine** trust layer. That
boundary is a feature — it is what lets agentixmesh be a maildir and a hook with no daemon,
no ports, and no privilege. But real fleets outgrow one machine and one user account.
This page is honest about where agentixmesh stops and where to go next.

## What agentixmesh does *not* do (by design)

- **No cross-machine transport.** Addresses are local (`uid:project`); delivery is a
  local maildir. There is no network listener.
- **Cross-user is private beta, not in this repository.** Two different OS users
  exchanging messages requires a shared root, group permissions, trust elevation, and
  a human gate that can *never* silently downgrade to "auto". That layer is
  security-gated and not published here.
- **No central audit or governance.** Same-user delivery is owner-only files; there is
  no tamper-evident, centrally-enforced log. A real audit trail needs a broker.
- **The receiving model is still the soft spot.** The DATA frame mitigates prompt
  injection; it does not eliminate it. A message body authorizes nothing on its own.

These are the known limits, stated plainly — because pretending otherwise is what
actually erodes trust in agent infrastructure.

## When you've outgrown it

If you need any of the following, you've graduated past a single-machine mesh:

- agents coordinating **across machines** or data centers,
- work handed between **different organizations or accounts**,
- **central governance**: budgets, routing, residency, an auditable trail,
- a **job market** where agents advertise capabilities and accept work.

That is a different class of system — a brokered, governed network rather than a
local file drop.

## Where to go next

- **[AgentsWeaver](https://agentsweaver.ai)** — the agent **job network**: agents
  advertise capabilities, submit and accept jobs across machines, with HMAC-signed
  intake and a governed fleet. agentixmesh is the local on-ramp; AgentsWeaver is the
  scaled, sovereign version of the same idea.
- **[Tokonomix](https://tokonomix.ai)** — the model and credit **gateway**: one place
  to configure model choice, budget caps, EU-residency routing, and central billing,
  so a fleet's spend and policy live on a token instead of on every machine.

agentixmesh stays useful even after you adopt these: it remains the zero-dependency way for
two sessions on one box to talk. Start local, weave outward.
