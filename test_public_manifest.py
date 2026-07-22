"""Default-deny release guard: every tracked file must be on PUBLIC-MANIFEST.txt.

The sibling guard (``test_no_org_literals.py``) is a denylist and therefore fails OPEN: it
refuses a file containing a string someone thought to ban. It cannot see the leak that actually
costs us — a whole new file crossing dev→public that contains no banned literal at all (an
unripe module, an internal design note, a threat-model doc). This test inverts the default: a
tracked path that no manifest pattern matches is a failure, and the fix is a deliberate line in
the manifest rather than a string nobody happened to ban.

Deliberately reads ``git ls-files``: tracked files are exactly what a push publishes. An
untracked scratch file is not a leak until someone commits it, and gitignored build output
should not have to be enumerated here.
"""

import fnmatch
import pathlib
import subprocess
import unittest

_REPO = pathlib.Path(__file__).parent
_MANIFEST = _REPO / "PUBLIC-MANIFEST.txt"


def _patterns():
    lines = _MANIFEST.read_text(encoding="utf-8").splitlines()
    return [ln.split("#", 1)[0].strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _tracked():
    out = subprocess.run(["git", "-C", str(_REPO), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def _allowed(path, patterns):
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


class PublicManifestTest(unittest.TestCase):
    def test_every_tracked_file_is_on_the_manifest(self):
        patterns = _patterns()
        unlisted = [p for p in _tracked() if not _allowed(p, patterns)]
        self.assertEqual(
            unlisted, [],
            "not on PUBLIC-MANIFEST.txt (default is DENY — add a deliberate line if it "
            f"really belongs in the public repo): {unlisted}",
        )

    def test_the_manifest_has_no_dead_patterns(self):
        """A pattern matching nothing is stale — it hides that the thing it guarded is gone."""
        tracked = _tracked()
        dead = [pat for pat in _patterns() if not any(fnmatch.fnmatch(p, pat) for p in tracked)]
        self.assertEqual(dead, [], f"manifest patterns match no tracked file: {dead}")

    def test_the_guard_would_refuse_an_unlisted_file(self):
        """A default-deny that never denies proves nothing. Prove it denies — synthetically."""
        patterns = _patterns()
        self.assertFalse(_allowed("pm_mesh/superadmin.py".replace("pm_mesh/", "internal/"), patterns))
        self.assertFalse(_allowed("docs/2026-07-21-block-c-crossuid-release-design.md", patterns))
        self.assertFalse(_allowed("TAKEOVER-2026-07-21-trust-calibration-live.md", patterns))
        self.assertTrue(_allowed("skill/SKILL.md", patterns))

    def test_docs_are_curated_not_globbed(self):
        """docs/ must stay an explicit list: a glob there would auto-publish internal design docs."""
        patterns = _patterns()
        self.assertFalse(_allowed("docs/anything-new.md", patterns))


if __name__ == "__main__":
    unittest.main()
