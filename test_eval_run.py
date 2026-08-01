"""Tests for ``pm_mesh.eval_run`` — the drill engine (canary dir, manifest, send).

Guard rails pinned here, not left to convention: dry-run is the default and sends nothing;
a cross-uid target is refused unless explicitly allowed; ``execute`` writes only inside its run
dir when dry-running; every case gets a unique token; the manifest round-trips through JSON.
"""

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from pm_mesh import eval_run, maildir
from pm_mesh.eval_corpus import CORPUS, CorpusError


class _Isolated(unittest.TestCase):
    """Base: redirect HOME/XDG_DATA_HOME/MESH_ROOT so the real mesh is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.mesh_root = os.path.join(base, "mesh")
        self.xdg = os.path.join(base, "xdg")
        os.makedirs(self.mesh_root, mode=0o700)
        os.makedirs(self.xdg, mode=0o700)
        patcher = mock.patch.dict(
            os.environ,
            {"HOME": base, "XDG_DATA_HOME": self.xdg, "MESH_ROOT": self.mesh_root},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.uid = os.getuid()
        self.addr = f"{self.uid}:eval_target"

    def _mesh_message_count(self):
        total = 0
        for dirpath, _dirs, files in os.walk(self.mesh_root):
            if os.path.basename(dirpath) == "new":
                total += len(files)
        return total


class PlanTest(_Isolated):
    def test_plan_shape_and_unique_tokens(self):
        p = eval_run.plan(CORPUS, to=self.addr)
        self.assertEqual(len(p), len(CORPUS))
        tokens = [e["token"] for e in p]
        self.assertEqual(len(tokens), len(set(tokens)), "tokens must be unique per case")
        for entry, case in zip(p, CORPUS):
            self.assertEqual(set(entry), {"case_id", "category", "token", "body"})
            self.assertEqual(entry["case_id"], case.id)
            self.assertIn(entry["token"], entry["body"])
            # plan cannot know the per-run canary path yet -> placeholder survives for execute.
            self.assertIn("{canary_path}", entry["body"])

    def test_plan_marker_toggle(self):
        on = eval_run.plan(CORPUS, to=self.addr, marker=True)
        off = eval_run.plan(CORPUS, to=self.addr, marker=False)
        self.assertIn(eval_run.MARKER, on[0]["body"])
        self.assertNotIn(eval_run.MARKER, off[0]["body"])

    def test_plan_rejects_malformed_address(self):
        with self.assertRaises(ValueError):
            eval_run.plan(CORPUS, to="not-an-address")


class ExecuteTest(_Isolated):
    def test_dry_run_sends_nothing_but_writes_manifest(self):
        p = eval_run.plan(CORPUS, to=self.addr)
        with mock.patch.object(maildir, "deliver") as deliver:
            manifest = eval_run.execute(p, to=self.addr, dry_run=True)
        deliver.assert_not_called()
        self.assertEqual(self._mesh_message_count(), 0)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(len(manifest["cases"]), len(CORPUS))
        for c in manifest["cases"]:
            self.assertFalse(c["sent"])
            self.assertIsNone(c["message_id"])
        # manifest + canary planted on disk.
        run_dir = os.path.dirname(manifest["canary_path"])
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "manifest.json")))
        self.assertTrue(os.path.isfile(manifest["canary_path"]))

    def test_dry_run_writes_only_inside_run_dir(self):
        p = eval_run.plan(CORPUS, to=self.addr)
        manifest = eval_run.execute(p, to=self.addr, dry_run=True)
        run_dir = os.path.dirname(manifest["canary_path"])
        # Nothing landed in the mesh root.
        self.assertEqual(self._mesh_message_count(), 0)
        # Everything that was written lives under the eval run dir.
        eval_root = os.path.join(self.xdg, "agentixmesh-eval")
        for dirpath, _dirs, files in os.walk(eval_root):
            for f in files:
                full = os.path.join(dirpath, f)
                self.assertTrue(
                    full.startswith(run_dir), f"{full} written outside the run dir {run_dir}"
                )

    def test_run_dir_is_0700(self):
        p = eval_run.plan(CORPUS, to=self.addr)
        manifest = eval_run.execute(p, to=self.addr, dry_run=True)
        run_dir = os.path.dirname(manifest["canary_path"])
        self.assertEqual(stat.S_IMODE(os.stat(run_dir).st_mode), 0o700)

    def test_real_run_records_one_message_id_per_case(self):
        p = eval_run.plan(CORPUS, to=self.addr)
        sent = []
        real_deliver = maildir.deliver

        def spy(msg, *a, **k):
            sent.append(msg)
            return real_deliver(msg, *a, **k)

        with mock.patch.object(maildir, "deliver", side_effect=spy):
            manifest = eval_run.execute(p, to=self.addr, dry_run=False)

        self.assertEqual(len(sent), len(CORPUS))
        ids = [c["message_id"] for c in manifest["cases"]]
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)), "each case gets its own message id")
        self.assertTrue(all(c["sent"] for c in manifest["cases"]))
        # The real per-run canary path was substituted into every sent body.
        canary = manifest["canary_path"]
        for msg in sent:
            self.assertIn(canary, msg.body)
            self.assertNotIn("{canary_path}", msg.body)
        # And the drop really happened.
        self.assertEqual(self._mesh_message_count(), len(CORPUS))

    def test_cross_uid_refused_by_default(self):
        other = f"{self.uid + 1}:colleague"
        p = eval_run.plan(CORPUS, to=other)
        with self.assertRaises(eval_run.EvalRunError):
            eval_run.execute(p, to=other, dry_run=True)

    def test_cross_uid_has_no_override(self):
        # Not even a dry run: the run would produce a manifest that later scores as a clean pass
        # for reasons that have nothing to do with the agent's behaviour.
        other = f"{self.uid + 1}:colleague"
        p = eval_run.plan(CORPUS, to=other)
        with self.assertRaises(eval_run.EvalRunError):
            eval_run.execute(p, to=other, dry_run=True)

    def test_every_token_has_the_same_length(self):
        # The no-cross-match guarantee in eval_score rests on this: equal-length tokens mean one
        # can never be a prefix or substring of another. A variable-length token would silently
        # reintroduce false compliances.
        p = eval_run.plan(CORPUS, to=self.addr)
        self.assertEqual(len({len(e["token"]) for e in p}), 1)

    def test_manifest_round_trips_through_json(self):
        p = eval_run.plan(CORPUS, to=self.addr)
        manifest = eval_run.execute(p, to=self.addr, dry_run=True)
        run_dir = os.path.dirname(manifest["canary_path"])
        with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as fh:
            disk = json.load(fh)
        self.assertEqual(disk, manifest)
        for key in ("version", "run_id", "created_utc", "to", "from", "marker", "canary_path", "cases"):
            self.assertIn(key, manifest)

    def test_refuses_when_corpus_invalid(self):
        p = eval_run.plan(CORPUS, to=self.addr)
        with mock.patch.object(eval_run, "validate_corpus", side_effect=CorpusError("boom")):
            with self.assertRaises(CorpusError):
                eval_run.execute(p, to=self.addr, dry_run=True)

    def test_refuses_to_exceed_turn_cap(self):
        # A drill larger than the advisory turn cap stops early with a clear error.
        from pm_mesh import config

        big = eval_run.plan(CORPUS, to=self.addr)
        big = (big * ((config.MAX_TURNS_PER_THREAD // len(big)) + 2))  # > MAX_TURNS_PER_THREAD entries
        self.assertGreater(len(big), config.MAX_TURNS_PER_THREAD)
        with self.assertRaises(eval_run.EvalRunError):
            eval_run.execute(big, to=self.addr, dry_run=True)


if __name__ == "__main__":
    unittest.main()
