import json
import os
import tempfile
import unittest

from pm_mesh import settings_merge
from pm_mesh.enroll import EX_OK, EX_SETTINGS

CMD = "/usr/local/bin/mesh-inject"


class MergeHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def _write(self, obj):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_missing_file_is_created(self):
        rc = settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        self.assertEqual(rc, EX_OK)
        data = json.load(open(self.path, encoding="utf-8"))
        entries = [h for arr in data["hooks"].values() for h in arr]
        sources = {h.get("source") for h in entries}
        self.assertIn("agentixmesh", sources)

    def test_preserves_unrelated_and_adds_exactly_one(self):
        self._write({"hooks": {"SessionStart": [{"source": "other", "command": "x"}]},
                     "unrelated": 42})
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(data["unrelated"], 42)
        ours = [h for arr in data["hooks"].values() for h in arr if h.get("source") == "agentixmesh"]
        self.assertEqual(len({h["command"] for h in ours}), 1)
        others = [h for arr in data["hooks"].values() for h in arr if h.get("source") == "other"]
        self.assertEqual(len(others), 1)

    def test_idempotent_upsert_by_source(self):
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        settings_merge.merge_hook(self.path, command=CMD, version="2.0")  # upgrade in place
        data = json.load(open(self.path, encoding="utf-8"))
        ours = [h for arr in data["hooks"].values() for h in arr if h.get("source") == "agentixmesh"]
        # One entry per hook event, version upgraded (upsert by source, not append).
        self.assertTrue(all(h["version"] == "2.0" for h in ours))

    def test_empty_file_treated_as_empty_object(self):
        open(self.path, "w").close()  # zero bytes
        rc = settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        self.assertEqual(rc, EX_OK)

    def test_malformed_jsonc_no_write_and_defers(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"hooks": {}, // trailing comment JSONC\n}')
        before = open(self.path, encoding="utf-8").read()
        rc = settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        self.assertEqual(rc, EX_SETTINGS)
        self.assertEqual(open(self.path, encoding="utf-8").read(), before)  # never clobbered


class RemoveHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")

    def test_remove_absent(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {}}, fh)
        self.assertEqual(settings_merge.remove_hook(self.path), "absent")

    def test_remove_by_source(self):
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        self.assertEqual(settings_merge.remove_hook(self.path), "removed")

    def test_reports_user_modified(self):
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = json.load(open(self.path, encoding="utf-8"))
        for arr in data["hooks"].values():
            for h in arr:
                if h.get("source") == "agentixmesh":
                    h["command"] = "/usr/local/bin/mesh-inject --changed-by-user"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        # Still source-matched, but command differs from the canonical one -> report, don't silently skip.
        self.assertEqual(settings_merge.remove_hook(self.path, expected_command=CMD), "modified")

    def test_mixed_event_remove_leaves_file_unchanged(self):
        """One event has user-modified command, other has canonical -> 'modified', file untouched."""
        settings_merge.merge_hook(self.path, command=CMD, version="1.0")
        data = json.load(open(self.path, encoding="utf-8"))
        # Modify the command in SessionStart only; leave UserPromptSubmit canonical.
        hooks = data.get("hooks", {})
        for h in hooks.get("SessionStart", []):
            if h.get("source") == "agentixmesh":
                h["command"] = "/usr/local/bin/mesh-inject --user-modified"
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        before = open(self.path, encoding="utf-8").read()
        result = settings_merge.remove_hook(self.path, expected_command=CMD)
        self.assertEqual(result, "modified")
        self.assertEqual(open(self.path, encoding="utf-8").read(), before)
