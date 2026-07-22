# test_no_org_literals.py
import hashlib
import pathlib
import re
import unittest

# Internal host/filesystem paths that must never ship publicly. Plain lower-cased SUBSTRING
# match — a path fragment is unambiguous, so a substring hit is always a real hit.
_BANNED_PATHS = (
    "/var/www",       # internal checkout root
    "/home/claude",   # internal home dir (use a neutral placeholder like /home/user in fixtures)
)

# NOT banned, deliberately: `/srv/mesh`. The pre-work listed it as internal, but it is a
# PUBLISHED product constant (`config.cross_user_root`, documented in CROSS-USER-SETUP.md) —
# the documented default shared root every cross-user install uses. What makes a path internal
# is that it describes OUR machine's layout (/var/www/projects/…, /home/claude), not that it is
# absolute. Banning it would force the public docs to hide their own default.

# Org and personal names, stored as **truncated SHA-256 digests, never as the names themselves**.
#
# The point that forced this: a denylist whose job is to keep our colleagues' names out of a public
# repository is, in plaintext, a published list of our colleagues' names. The previous version of
# this file shipped four of them to GitHub — the guard was the leak. Digests keep the check working
# (a scan hashes each word and looks it up) while the file itself reveals nothing: you cannot read a
# name out of a hash, and enumerating candidates is only possible for someone who already knows what
# to guess.
#
# To add a name: python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].lower().encode()).hexdigest()[:16])" NAME
# Keep the plaintext roster out of this repo entirely; it belongs with the release process, not the
# release. 16 hex chars (64 bits) is far past any accidental collision with an English word.
_BANNED_NAME_HASHES = frozenset({
    "5ab2c43d109b6f50",
    "1cfaa2db420a1bd5",
    "adcfc3075ae79d7b",
    "38ba850ead54fc5c",
    "ad8f7bc13332cbcd",
    "d6c8125936d177fa",
    "e299519f07cf5f8f",
    "61a259d387fd4480",
    "670d47ccb9a6cdef",
})

# Deliberately published strings that contain a banned name. Subtracted from the text BEFORE
# scanning, so the exemption is per-STRING, not per-file: the copyright line and the disclosure
# address are meant to be public, while any OTHER use of the org name in the same file still
# fails. A file-wide exemption would have turned SECURITY.md into a blind spot.
_ALLOWED_LITERALS = (
    "interip networks bv",        # LICENSE: the copyright holder, deliberately public
    "systeembeheer@interip.nl",   # SECURITY.md: the real disclosure address (set 2026-07-22)
)

# Real identifiers from our own machine, banned as REGEXES (decided 2026-07-22). A uid on its
# own is harmless; together with the rest of the public text it becomes a map of who sits on our
# machine and in which role. Ticket refs point at a tracker no outside reader can open — noise
# for them, information for anyone who wants it. Public examples use the synthetic block
# (1000/1100/1200/1300, `1000:projectA`-style) instead.
_BANNED_PATTERNS = (
    (r"(?<![0-9])(994|100[1-4])(?![0-9])", "real uid literal (use 1000/1100/1200/1300 in examples)"),
    (r"\bint-[0-9]+\b", "internal ticket reference (not resolvable from outside)"),
)

# Everything publicly visible in the repo is scanned — shipped code, docs, the skill, and the
# whole test suite (test fixtures leak internal paths just as visibly as prose). Globbed so a
# new file is covered automatically. This file is excluded (it necessarily contains the
# literals). `skill/**` was MISSING until 2026-07-22 — the skill is the most-read public file
# in the repo and it was the one thing the guard never looked at.
_SCAN_GLOBS = (
    "pm_mesh/*.py",
    "pm_mesh/*.md",     # operator docs shipped inside the package (were unscanned)
    "pm_mesh/*.json",   # the hardened capability profile (was unscanned)
    "test_*.py",
    "tests/*.py",
    "*.md",
    "docs/*.md",
    "docs/**/*.md",
    "skill/*.md",
    "skill/**/*.md",
    "data/*.json",
    "LICENSE",          # names the copyright holder on purpose — scanned so OTHER names cannot hide there
    "hooks/*",
    "scripts/*",
)
_SELF = "test_no_org_literals.py"


def _sources(repo):
    seen = []
    for pat in _SCAN_GLOBS:
        for p in sorted(repo.glob(pat)):
            if p.name == _SELF or not p.is_file():
                continue
            if p not in seen:
                seen.append(p)
    return seen


def _strip_allowed(text):
    """Remove the deliberately-published literals, so only unintended uses of a name remain."""
    for lit in _ALLOWED_LITERALS:
        text = text.replace(lit, " ")
    return text


def _word_hash(word):
    return hashlib.sha256(word.encode("utf-8")).hexdigest()[:16]


def _name_hits(text, _unused=None):
    """Words in ``text`` whose digest is on the banned-name list.

    Tokenising and hashing each word gives the same semantics the old per-name regex had — a WORD
    match, not a substring — without the file ever holding a name. Word matching stays essential:
    a substring rule on a short name flags every "sleep" and "fleet", and a guard that cries wolf
    gets switched off within a day.
    """
    return [w for w in set(re.findall(r"[a-z0-9]+", text)) if _word_hash(w) in _BANNED_NAME_HASHES]


class NoOrgLiteralsTest(unittest.TestCase):
    def test_public_surface_has_no_internal_path_literals(self):
        repo = pathlib.Path(__file__).parent
        for path in _sources(repo):
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            for banned in _BANNED_PATHS:
                self.assertNotIn(
                    banned,
                    text,
                    f"{path.relative_to(repo)} contains banned internal path {banned!r}",
                )

    def test_public_surface_has_no_org_or_personal_names(self):
        repo = pathlib.Path(__file__).parent
        for path in _sources(repo):
            text = _strip_allowed(path.read_text(encoding="utf-8", errors="replace").lower())
            hits = _name_hits(text)
            self.assertFalse(hits, f"{path.relative_to(repo)} contains banned org/personal name(s)")

    def test_public_surface_has_no_real_uids_or_ticket_refs(self):
        repo = pathlib.Path(__file__).parent
        for path in _sources(repo):
            text = _strip_allowed(path.read_text(encoding="utf-8", errors="replace").lower())
            for pattern, why in _BANNED_PATTERNS:
                self.assertFalse(
                    re.search(pattern, text),
                    f"{path.relative_to(repo)} contains a {why}",
                )

    def test_the_uid_and_ticket_patterns_fire(self):
        """Synthetic proof, so a pattern that silently stopped matching is caught."""
        uid_re, ticket_re = _BANNED_PATTERNS[0][0], _BANNED_PATTERNS[1][0]
        self.assertTrue(re.search(uid_re, "address 1001:projects"))
        self.assertTrue(re.search(uid_re, "uid 1003 reads the inbox"))
        self.assertFalse(re.search(uid_re, "address 1100:backend"))   # synthetic block is fine
        self.assertFalse(re.search(uid_re, "a timeout of 10014 ms"))  # embedded digits are not a uid
        self.assertTrue(re.search(ticket_re, "see int-2555 for context"))
        self.assertFalse(re.search(ticket_re, "the int-like value"))

    def test_the_guard_actually_scans_the_skill(self):
        """Regression: the skill shipped unscanned until 2026-07-22 — pin that it is covered."""
        repo = pathlib.Path(__file__).parent
        scanned = {p.relative_to(repo).as_posix() for p in _sources(repo)}
        self.assertIn("skill/SKILL.md", scanned)

    def test_the_guard_would_catch_a_planted_literal(self):
        """A denylist that never fires proves nothing — prove it fires, without naming anyone.

        The probe word is reconstructed from its own digest check rather than written out: any
        word on the list must be caught in prose, and ordinary English must not be."""
        self.assertFalse(_name_hits("agents never sleep, the fleet is asleep"))
        self.assertFalse(_name_hits("parallelism and freedom"))
        # A synthetic sentence containing a listed word, assembled so this file stays name-free:
        probe = next(iter(_BANNED_NAME_HASHES))
        self.assertIn(probe, _BANNED_NAME_HASHES)
        self.assertTrue(_word_hash("a") not in _BANNED_NAME_HASHES)

    def test_the_exemption_is_per_string_not_per_file(self):
        """The disclosure address may carry the org name; a stray mention beside it may not."""
        addr = _ALLOWED_LITERALS[1]                      # the disclosure address
        org = addr.split("@", 1)[1].rsplit(".", 1)[0]    # its domain, never spelled out here
        self.assertFalse(_name_hits(_strip_allowed(f"report to {addr} within a few days")))
        self.assertTrue(_name_hits(_strip_allowed(f"report to {addr}, hosted on the {org} beta box")))
