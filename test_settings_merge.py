import json
import os
import tempfile
import unittest

from pm_mesh import settings_merge
from pm_mesh.enroll import EX_OK, EX_SETTINGS

CMD = "/usr/local/bin/mesh-inject"


def _entries(data):
    return [h for arr in data.get("hooks", {}).values() for h in arr]


def _cmds(h):
    """Every command string in a hook-array element, across the legacy bare form and the valid
    nested Claude-Code form (``{"hooks": [{"type": "command", "command": ...}]}``)."""
    out = []
    if isinstance(h.get("command"), str):
        out.append(h["command"])
    for sub in h.get("hooks", []) or []:
        if isinstance(sub, dict) and isinstance(sub.get("command"), str):
            out.append(sub["command"])
    return out


def _is_valid_cc_entry(h):
    """A Claude-Code hook-array element MUST carry a ``hooks: [{type, command}]`` list — this is
    exactly what the settings.json parser requires ("Expected array, but received undefined" when the
    key is missing → the whole file is rejected)."""
    hs = h.get("hooks")
    return (
        isinstance(hs, list)
        and len(hs) >= 1
        and all(
            isinstance(s, dict) and s.get("type") == "command" and isinstance(s.get("command"), str)
            for s in hs
        )
    )


class MergeHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def _write(self, obj):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def _load(self):
        return json.load(open(self.path, encoding="utf-8"))

    def _ours(self, data):
        return [h for h in _entries(data) if CMD in _cmds(h)]

    def test_missing_file_is_created(self):
        rc = settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        self.assertEqual(rc, EX_OK)
        self.assertTrue(self._ours(self._load()))

    def test_written_entry_is_valid_claude_code_schema(self):
        # THE regression for the live breakage: a bare {source,version,command} entry has no `hooks`
        # array → Claude Code rejects the WHOLE settings.json → all permissions (incl. bypass) fall
        # away → prompt on every tool-call. Every OUR entry must be the valid nested form.
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = self._load()
        ours = self._ours(data)
        self.assertTrue(ours)
        for h in ours:
            self.assertTrue(_is_valid_cc_entry(h), f"invalid CC hook entry: {h!r}")

    def test_preserves_unrelated_and_adds_exactly_one(self):
        self._write({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]},
                     "unrelated": 42})
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = self._load()
        self.assertEqual(data["unrelated"], 42)
        # exactly one distinct OUR command, and the foreign entry survived untouched.
        self.assertEqual(len({c for h in self._ours(data) for c in _cmds(h)}), 1)
        foreign = [h for h in _entries(data) if "x" in _cmds(h)]
        self.assertEqual(len(foreign), 1)

    def test_idempotent_upsert(self):
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        settings_merge.merge_hook(self.path, command=CMD, version="2.0")  # upgrade in place
        data = self._load()
        # one entry per hook event (upsert, not append); still schema-valid.
        self.assertEqual(len(self._ours(data)), len(("SessionStart", "UserPromptSubmit")))
        for h in self._ours(data):
            self.assertTrue(_is_valid_cc_entry(h))

    def test_empty_file_treated_as_empty_object(self):
        open(self.path, "w").close()  # zero bytes
        self.assertEqual(settings_merge.merge_hook(self.path, command=CMD, version="1.0"), EX_OK)

    def test_malformed_jsonc_no_write_and_defers(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"hooks": {}, // trailing comment JSONC\n}')
        before = open(self.path, encoding="utf-8").read()
        rc = settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        self.assertEqual(rc, EX_SETTINGS)
        self.assertEqual(open(self.path, encoding="utf-8").read(), before)  # never clobbered

    def test_matching_survives_cc_normalization(self):
        """Claude Code REWRITES settings.json and STRIPS unknown top-level keys (source/version), so a
        later dedup/remove that matched only on `source` would fail silently → duplicate on re-merge,
        un-removable entry. Matching must survive on the command (which normalization preserves)."""
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = self._load()
        # simulate CC normalization: drop the extra top-level keys, keep the nested valid hooks.
        for h in _entries(data):
            h.pop("source", None)
            h.pop("version", None)
        self._write(data)
        # re-merge must NOT duplicate (dedup by command survives the source strip).
        settings_merge.merge_hook(self.path, command=CMD, version="2.0")
        self.assertEqual(len(self._ours(self._load())), len(("SessionStart", "UserPromptSubmit")))
        # and it is still removable post-normalization (via the command, source gone).
        self.assertEqual(settings_merge.remove_hook(self.path, expected_command=CMD), "removed")


class RemoveHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def _load(self):
        return json.load(open(self.path, encoding="utf-8"))

    def test_remove_absent(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {}}, fh)
        self.assertEqual(settings_merge.remove_hook(self.path), "absent")

    def test_remove_by_source(self):
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        self.assertEqual(settings_merge.remove_hook(self.path), "removed")

    def test_remove_by_marker_after_normalization(self):
        # A marker-carrying command survives normalization; remove_hook(marker=...) finds it even
        # after source is stripped and even if the caller doesn't know the exact command.
        marked = CMD + " #mesh-poll-claude-v1"
        settings_merge.merge_hook(self.path, command=marked, version="1.0", source="mesh-poll-claude")
        data = self._load()
        for h in _entries(data):
            h.pop("source", None)
            h.pop("version", None)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertEqual(
            settings_merge.remove_hook(self.path, source="mesh-poll-claude", marker="#mesh-poll-claude-v1"),
            "removed",
        )
        self.assertEqual(_entries(self._load()), [])

    def test_reports_user_modified(self):
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = self._load()
        for h in _entries(data):
            if h.get("source") == "agentixmesh":
                h["hooks"][0]["command"] = CMD + " --changed-by-user"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        # Still source-matched, but nested command differs from canonical → report, don't skip.
        self.assertEqual(settings_merge.remove_hook(self.path, expected_command=CMD), "modified")

    def test_mixed_event_remove_leaves_file_unchanged(self):
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = self._load()
        for h in data.get("hooks", {}).get("SessionStart", []):
            if h.get("source") == "agentixmesh":
                h["hooks"][0]["command"] = CMD + " --user-modified"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        before = open(self.path, encoding="utf-8").read()
        self.assertEqual(settings_merge.remove_hook(self.path, expected_command=CMD), "modified")
        self.assertEqual(open(self.path, encoding="utf-8").read(), before)
