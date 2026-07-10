"""Capability-profile lint (phase 2, f2-13, F3/condition 4) — **read-only**: checks a ``settings.json``
against a profile and reports deviations. Changes NOTHING.

*Applying* a profile to a live PM ``settings.json`` is a **human action** (see
``CAPABILITY-PROFILES.md``); this script helps that human by showing which irreversible/outward-
reaching actions would still be executable.
"""

from __future__ import annotations

import argparse
import json
import sys

#: Profiles: per role the required deny rules, the substrings forbidden in ``allow``, and the
#: forbidden ``defaultMode`` values. Mirrors ``CAPABILITY-PROFILES.md`` (human-readable).
PROFILES = {
    "mesh-reachable-pm": {
        "description": "PM session that receives mesh messages; irreversible/external actions not executable.",
        "deny_required": [
            # privilege / OS administration
            "Bash(sudo:*)", "Bash(gpasswd:*)", "Bash(usermod:*)",
            # destructive filesystem (besides wipe also dd/shred/mkfs)
            "Bash(rm -rf:*)", "Bash(dd:*)", "Bash(shred:*)", "Bash(mkfs:*)",
            # service/prod/power (visibly irreversible)
            "Bash(systemctl:*)", "Bash(service:*)", "Bash(reboot:*)", "Bash(shutdown:*)",
            # network egress (exfil / external trigger) — besides curl/wget
            "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)",
            "Bash(nc:*)", "Bash(netcat:*)", "Bash(socat:*)", "Bash(telnet:*)", "Bash(ftp:*)",
            "WebFetch",
            # git-destructive / outbound (council f2-13)
            "Bash(git push:*)", "Bash(git reset:*)", "Bash(git clean:*)",
            # persistence / scheduled tasks
            "Bash(crontab:*)", "Bash(at:*)",
            # package managers (supply-chain / persistence)
            "Bash(apt:*)", "Bash(pip install:*)", "Bash(npm install:*)",
        ],
        "allow_forbidden_substrings": [
            "sudo", "rm -rf", "systemctl", "service ", "deploy", "curl", "wget",
            "gpasswd", "usermod", "ssh", "scp", "rsync", "socat", "netcat",
            "git push", "git reset", "git clean", "crontab", "reboot", "shutdown",
            "shred", "mkfs", "apt ", "pip install", "npm install",
        ],
        # Interpreters/wildcards that can BYPASS a deny prefix (council f2-13: prefix-deny is
        # bypassable via `bash -c "curl…"`, chaining, base64|sh). In allow → MEDIUM warning.
        "bypass_allow_substrings": [
            "python", "node", "perl", "ruby", "bash -c", "sh -c", "eval", "xargs",
            "Bash(*)", "Bash:*", "env ",
        ],
        "forbidden_default_modes": ["bypassPermissions"],
    }
}


class CapabilityLintError(Exception):
    """The settings could not be read/parsed, or the profile doesn't exist."""


def lint_settings(settings, profile_name: str = "mesh-reachable-pm") -> list:
    """Check a settings dict against a profile; return a list of findings (empty = clean).

    Each finding = ``{"severity", "kind", "detail"}``. Pure function, no I/O.
    """
    if profile_name not in PROFILES:
        raise CapabilityLintError(f"unknown profile: {profile_name!r}")
    profile = PROFILES[profile_name]
    findings = []

    perms = settings.get("permissions", {}) if isinstance(settings, dict) else {}
    allow = perms.get("allow") or []
    deny = perms.get("deny") or []
    default_mode = perms.get("defaultMode")

    # 1. forbidden allow rules (an irreversible action explicitly permitted).
    for entry in allow:
        for sub in profile["allow_forbidden_substrings"]:
            if isinstance(entry, str) and sub in entry:
                findings.append(
                    {
                        "severity": "high",
                        "kind": "forbidden_allow",
                        "detail": f"allow contains {entry!r} (forbidden pattern {sub!r})",
                    }
                )

    # 2. missing required deny rules.
    deny_set = {d for d in deny if isinstance(d, str)}
    for req in profile["deny_required"]:
        if req not in deny_set:
            findings.append(
                {
                    "severity": "high",
                    "kind": "missing_deny",
                    "detail": f"required deny missing: {req!r}",
                }
            )

    # 3. bypass vectors in allow: interpreters/wildcards that bypass a deny prefix (council f2-13).
    for entry in allow:
        if not isinstance(entry, str):
            continue
        for sub in profile.get("bypass_allow_substrings", []):
            if sub in entry:
                findings.append(
                    {
                        "severity": "medium",
                        "kind": "bypass_allow",
                        "detail": (
                            f"allow {entry!r} contains {sub!r} — an interpreter/wildcard can bypass "
                            f"the deny prefix (e.g. `bash -c \"curl …\"`); prefix-deny is not airtight, "
                            f"prefer deny-by-default + a curated allow-list"
                        ),
                    }
                )

    # 4. unsafe defaultMode (bypasses every deny).
    if default_mode in profile["forbidden_default_modes"]:
        findings.append(
            {
                "severity": "high",
                "kind": "unsafe_default_mode",
                "detail": f"defaultMode {default_mode!r} bypasses all deny rules",
            }
        )

    return findings


def lint_file(path: str, profile_name: str = "mesh-reachable-pm") -> list:
    """Read ``path`` **read-only** and lint it. Raises ``CapabilityLintError`` on read/parse error."""
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except OSError as exc:
        raise CapabilityLintError(f"cannot read {path!r}: {exc}") from exc
    except ValueError as exc:
        raise CapabilityLintError(f"{path!r} is not valid JSON: {exc}") from exc
    return lint_settings(settings, profile_name)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mesh-caplint",
        description="Check (read-only) a settings.json against a capability profile.",
    )
    p.add_argument("settings", help="path to the settings.json to check")
    p.add_argument("profile", nargs="?", default="mesh-reachable-pm", help="profile name")
    return p


def main(argv=None) -> int:
    """Console entry; exit 0 = clean, 1 = deviations, 2 = read/profile error. Changes nothing."""
    args = _build_parser().parse_args(argv)
    try:
        findings = lint_file(args.settings, args.profile)
    except CapabilityLintError as exc:
        print(f"mesh-caplint: {exc}", file=sys.stderr)
        return 2
    if not findings:
        print(f"mesh-caplint: {args.settings} satisfies profile {args.profile!r} ✓")
        return 0
    print(f"mesh-caplint: {len(findings)} deviation(s) from profile {args.profile!r}:")
    for f in findings:
        print(f"  [{f['severity']}] {f['kind']}: {f['detail']}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
