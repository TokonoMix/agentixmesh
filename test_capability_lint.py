"""Capability-profile lint (f2-13): read-only check of a settings.json against a profile.

*Applying* it to a live settings.json is human-gated (parked); this test covers the template + the
lint (deliverables).
"""

import json
import os
import tempfile
import unittest

from pm_mesh import capability_lint


def _clean_settings():
    # A clean settings covers exactly the required denies and uses only safe allow rules
    # (no interpreter/wildcard → no bypass_allow warning).
    return {
        "permissions": {
            "allow": ["Read", "Grep", "Bash(ls:*)", "Bash(git status:*)"],
            "deny": list(capability_lint.PROFILES["mesh-reachable-pm"]["deny_required"]),
            "defaultMode": "default",
        }
    }


class LintSettingsTest(unittest.TestCase):
    def test_clean_settings_no_findings(self):
        self.assertEqual(capability_lint.lint_settings(_clean_settings()), [])

    def test_forbidden_allow_flagged(self):
        s = _clean_settings()
        s["permissions"]["allow"].append("Bash(sudo systemctl restart nginx)")
        findings = capability_lint.lint_settings(s)
        kinds = {f["kind"] for f in findings}
        self.assertIn("forbidden_allow", kinds)

    def test_missing_deny_flagged(self):
        s = _clean_settings()
        s["permissions"]["deny"].remove("WebFetch")
        findings = capability_lint.lint_settings(s)
        self.assertTrue(any(f["kind"] == "missing_deny" and "WebFetch" in f["detail"] for f in findings))

    def test_interpreter_allow_flagged_as_bypass(self):
        # An interpreter in allow reopens the entire deny list → bypass_allow (council f2-13).
        s = _clean_settings()
        s["permissions"]["allow"].append("Bash(python3 -c:*)")
        findings = capability_lint.lint_settings(s)
        self.assertTrue(any(f["kind"] == "bypass_allow" for f in findings))

    def test_wildcard_allow_flagged_as_bypass(self):
        s = _clean_settings()
        s["permissions"]["allow"].append("Bash(*)")
        findings = capability_lint.lint_settings(s)
        self.assertTrue(any(f["kind"] == "bypass_allow" for f in findings))

    def test_egress_and_git_in_required_denies(self):
        req = capability_lint.PROFILES["mesh-reachable-pm"]["deny_required"]
        for needed in ("Bash(ssh:*)", "Bash(git push:*)", "Bash(dd:*)", "Bash(crontab:*)"):
            self.assertIn(needed, req)

    def test_bypass_default_mode_flagged(self):
        s = _clean_settings()
        s["permissions"]["defaultMode"] = "bypassPermissions"
        findings = capability_lint.lint_settings(s)
        self.assertTrue(any(f["kind"] == "unsafe_default_mode" for f in findings))

    def test_empty_settings_flags_all_required_denies(self):
        findings = capability_lint.lint_settings({})
        missing = [f for f in findings if f["kind"] == "missing_deny"]
        self.assertEqual(len(missing), len(capability_lint.PROFILES["mesh-reachable-pm"]["deny_required"]))

    def test_unknown_profile_raises(self):
        with self.assertRaises(capability_lint.CapabilityLintError):
            capability_lint.lint_settings(_clean_settings(), profile_name="nope")


class LintFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_lint_file_clean(self):
        self._write(_clean_settings())
        self.assertEqual(capability_lint.lint_file(self.path), [])

    def test_lint_file_is_read_only(self):
        self._write(_clean_settings())
        before = os.stat(self.path).st_mtime_ns
        with open(self.path, encoding="utf-8") as fh:
            content_before = fh.read()
        capability_lint.lint_file(self.path)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), content_before, "lint must not modify the settings")
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_lint_file_bad_json_raises(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        with self.assertRaises(capability_lint.CapabilityLintError):
            capability_lint.lint_file(self.path)

    def test_cli_clean_exit0(self):
        self._write(_clean_settings())
        self.assertEqual(capability_lint.main([self.path]), 0)

    def test_cli_findings_exit1(self):
        self._write({})
        self.assertEqual(capability_lint.main([self.path]), 1)


class DocTest(unittest.TestCase):
    def test_profiles_doc_present_and_parks_apply(self):
        path = os.path.join(os.path.dirname(__file__), "pm_mesh", "CAPABILITY-PROFILES.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("mesh-reachable-pm", text)
        self.assertIn("PARKED", text)        # applying it is human-gated
        self.assertIn("Human-next-action", text)
        for tool in ("sudo", "WebFetch", "systemctl"):
            self.assertIn(tool, text)


if __name__ == "__main__":
    unittest.main()
