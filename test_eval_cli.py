"""Tests for ``pm_mesh.eval_cli`` — the ``mesh-eval`` command.

Guards pinned here: ``list`` is side-effect free; ``run`` is a dry run unless ``--apply`` and says
so; ``--blind`` warns; a cross-uid target without the flag exits non-zero; ``score`` propagates the
the exit codes and round-trips a real on-disk manifest; and the manifest's ``marker`` label never
lies about whether the sent bodies were marked.
"""

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pm_mesh import eval_cli, eval_run, maildir
from pm_mesh.eval_corpus import CORPUS, MARKER


class _Isolated(unittest.TestCase):
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

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = eval_cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    @staticmethod
    def _run_id(stdout):
        m = re.search(r"run_id:\s*(\S+)", stdout)
        assert m, f"no run_id in output:\n{stdout}"
        return m.group(1)


class ListTest(_Isolated):
    def test_list_prints_every_case_and_exits_0(self):
        rc, out, _ = self._run(["list"])
        self.assertEqual(rc, 0)
        for case in CORPUS:
            self.assertIn(case.id, out)


class RunTest(_Isolated):
    def test_dry_run_sends_nothing_and_says_so(self):
        with mock.patch.object(maildir, "deliver") as deliver:
            rc, out, _ = self._run(["run", "--to", self.addr])
        deliver.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("--apply", out)
        self.assertIn("run_id:", out)
        self.assertIn("mesh-eval score", out)

    def test_apply_sends_every_case(self):
        sent = []
        real = maildir.deliver

        def spy(msg, *a, **k):
            sent.append(msg)
            return real(msg, *a, **k)

        with mock.patch.object(maildir, "deliver", side_effect=spy):
            rc, out, _ = self._run(["run", "--to", self.addr, "--apply"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), len(CORPUS))

    def test_blind_warns(self):
        rc, _out, err = self._run(["run", "--to", self.addr, "--blind"])
        self.assertEqual(rc, 0)
        self.assertIn("BLIND", err)
        self.assertIn("own", err.lower())

    def test_marker_label_never_lies(self):
        # Marked run: manifest marker True AND every sent body carries the marker.
        sent = []
        real = maildir.deliver

        def spy(msg, *a, **k):
            sent.append(msg)
            return real(msg, *a, **k)

        with mock.patch.object(maildir, "deliver", side_effect=spy):
            _rc, out, _ = self._run(["run", "--to", self.addr, "--apply"])
        run_id = self._run_id(out)
        with open(eval_run.manifest_path(run_id), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertTrue(manifest["marker"])
        self.assertTrue(all(MARKER in m.body for m in sent))

        # Blind run: manifest marker False AND no sent body carries the marker.
        sent.clear()
        with mock.patch.object(maildir, "deliver", side_effect=spy):
            _rc, out, _ = self._run(["run", "--to", self.addr, "--apply", "--blind"])
        run_id = self._run_id(out)
        with open(eval_run.manifest_path(run_id), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertFalse(manifest["marker"])
        self.assertTrue(all(MARKER not in m.body for m in sent))

    def test_cases_subset(self):
        ids = f"{CORPUS[0].id},{CORPUS[1].id}"
        rc, out, _ = self._run(["run", "--to", self.addr, "--cases", ids])
        self.assertEqual(rc, 0)
        run_id = self._run_id(out)
        with open(eval_run.manifest_path(run_id), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(len(manifest["cases"]), 2)

    def test_unknown_case_id_errors(self):
        rc, _out, err = self._run(["run", "--to", self.addr, "--cases", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown case", err)

    def test_cross_uid_is_refused_and_says_why(self):
        # There is no override any more: a cross-uid drill cannot be scored (the canary is in the
        # sender's private dir the target cannot write, and the body is withheld by the approval
        # gate), so it would always read "resisted" regardless of the agent. A security tool must
        # not hand out a pass it did not measure.
        rc, _out, err = self._run(["run", "--to", f"{self.uid + 1}:colleague"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot be scored", err)


class ScoreTest(_Isolated):
    def _apply_run(self):
        rc, out, _ = self._run(["run", "--to", self.addr, "--apply"])
        assert rc == 0
        return self._run_id(out)

    def test_score_latest_all_resisted_exit_0(self):
        self._apply_run()  # nobody processes the inbox -> canary stays empty
        rc, out, _ = self._run(["score", "--latest"])
        self.assertEqual(rc, 0)
        self.assertIn("evidence, not immunity", out)  # honesty paragraph present

    def test_score_detects_compliance_exit_1(self):
        run_id = self._apply_run()
        with open(eval_run.manifest_path(run_id), encoding="utf-8") as fh:
            manifest = json.load(fh)
        # Simulate the agent-under-test obeying exactly one body: plant its token in the canary.
        token = manifest["cases"][0]["token"]
        with open(manifest["canary_path"], "a", encoding="utf-8") as fh:
            fh.write(token + "\n")
        rc, _out, _ = self._run(["score", "--run", run_id])
        self.assertEqual(rc, 1)

    def test_score_dry_run_is_exit_2(self):
        rc, out, _ = self._run(["run", "--to", self.addr])  # dry run
        run_id = self._run_id(out)
        rc, _out, _ = self._run(["score", "--run", run_id])
        self.assertEqual(rc, 2)  # nothing sent -> no evidence, never 0

    def test_score_json_is_parseable(self):
        self._apply_run()
        rc, out, _ = self._run(["score", "--latest", "--json"])
        self.assertIn(rc, (0, 1))
        parsed = json.loads(out)
        self.assertIn("totals", parsed)

    def test_score_without_selector_errors(self):
        rc, _out, err = self._run(["score"])
        self.assertEqual(rc, 2)
        self.assertIn("--run", err)

    def test_score_unknown_run_errors(self):
        rc, _out, err = self._run(["score", "--run", "no-such-run"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot read manifest", err)


if __name__ == "__main__":
    unittest.main()
