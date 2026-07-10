# test_no_org_literals.py
import pathlib
import unittest

# Org proper nouns AND internal host/filesystem paths that must never ship publicly.
# Lower-cased substring match (the scan lower-cases each file first).
_BANNED = (
    "interip",
    "ferhat",
    "dev01",
    "mesut",
    "aionized",
    "/var/www",       # internal checkout root
    "/home/claude",   # internal home dir (use a neutral placeholder like /home/user in fixtures)
)

# Everything publicly visible in the repo is scanned — shipped code, docs, and the whole
# test suite (test fixtures leak internal paths just as visibly as prose). Globbed so a new
# file is covered automatically. This file is excluded (it necessarily contains the literals).
_SCAN_GLOBS = ("pm_mesh/*.py", "test_*.py", "tests/*.py", "*.md", "docs/*.md")
_SELF = "test_no_org_literals.py"


def _sources(repo):
    seen = []
    for pat in _SCAN_GLOBS:
        for p in sorted(repo.glob(pat)):
            if p.name == _SELF:
                continue
            seen.append(p)
    return seen


class NoOrgLiteralsTest(unittest.TestCase):
    def test_public_surface_has_no_org_or_internal_path_literals(self):
        repo = pathlib.Path(__file__).parent
        for path in _sources(repo):
            text = path.read_text(encoding="utf-8").lower()
            for banned in _BANNED:
                self.assertNotIn(
                    banned,
                    text,
                    f"{path.relative_to(repo)} contains banned literal {banned!r}",
                )
