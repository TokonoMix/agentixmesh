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
from pm_mesh.eval_corpus import CORPUS, MARKER, REQUIRED_FIELDS


def _default_channel_ids():
    """Case ids the bare CLI can fire — the canary-channel cases, which need no extra flag.

    repo_edit and mesh_action observe a channel the CLI configures only with an extra
    flag; until then a bare run carries them as skips rather than sending them.
    """
    return [c.id for c in CORPUS if "canary_path" in REQUIRED_FIELDS[c.category]]


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

    def test_apply_sends_every_default_channel_case(self):
        # A bare run configures only the canary channel, so it fires the canary cases and carries
        # the repo_edit cases as skips (their channel needs an extra flag). It must not silently
        # send fewer than the whole canary set, and it must not send the skipped ones.
        sent = []
        real = maildir.deliver

        def spy(msg, *a, **k):
            sent.append(msg)
            return real(msg, *a, **k)

        with mock.patch.object(maildir, "deliver", side_effect=spy):
            rc, out, _ = self._run(["run", "--to", self.addr, "--apply"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), len(_default_channel_ids()))
        self.assertLess(len(sent), len(CORPUS), "the repo_edit cases were skipped, not sent")

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


class ChannelFlagTest(_Isolated):
    """The --repo-file / --third-addr flags and the skip announcement."""

    def _manifest_for(self, run_id):
        with open(eval_run.manifest_path(run_id), encoding="utf-8") as fh:
            return json.load(fh)

    def test_repo_file_and_third_addr_reach_the_engine(self):
        repo = os.path.join(self._tmp.name, "in_tree.py")
        third = f"{self.uid}:third_party"
        rc, out, err = self._run([
            "run", "--to", self.addr, "--apply",
            "--repo-file", repo, "--third-addr", third,
        ])
        self.assertEqual(rc, 0)
        manifest = self._manifest_for(self._run_id(out))
        self.assertEqual(manifest["repo_file_path"], repo)
        self.assertEqual(manifest["third_addr"], third)
        # Every shipped category was fired — no skip_reason anywhere.
        self.assertFalse(any(c.get("skip_reason") for c in manifest["cases"]))
        self.assertTrue(os.path.isfile(repo))  # the engine planted it

    def test_missing_flags_warn_before_any_send(self):
        sent_at = []
        first_warning_seen = {"done": False}
        real = maildir.deliver

        # Record, at the moment of the first send, whether the skip warning had already been printed.
        err_buf = io.StringIO()

        def spy(msg, *a, **k):
            if "repo_edit" in err_buf.getvalue():
                first_warning_seen["done"] = True
            sent_at.append(msg)
            return real(msg, *a, **k)

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err_buf):
            with mock.patch.object(maildir, "deliver", side_effect=spy):
                eval_cli.main(["run", "--to", self.addr, "--apply"])
        err = err_buf.getvalue()
        self.assertIn("repo_edit", err)
        self.assertIn("mesh_action", err)
        self.assertIn("--repo-file", err)
        self.assertIn("--third-addr", err)
        self.assertTrue(sent_at, "some canary cases should still have sent")
        self.assertTrue(first_warning_seen["done"], "the skip warning must precede the first send")

    def test_apply_message_counts_only_what_was_sent(self):
        with mock.patch.object(maildir, "deliver"):
            rc, out, _ = self._run(["run", "--to", self.addr, "--apply"])
        self.assertEqual(rc, 0)
        # Sent count excludes the skipped repo_edit / mesh_action cases.
        self.assertIn(f"sent {len(_default_channel_ids())} case", out)


class ScoreTest(_Isolated):
    def _apply_run(self):
        rc, out, _ = self._run(["run", "--to", self.addr, "--apply"])
        assert rc == 0
        return self._run_id(out)

    def test_score_full_canary_coverage_all_resisted_exit_0(self):
        # Exit 0 means every SELECTED case was fired and resisted. A bare run now skips the repo_edit
        # cases (their channel is unconfigured) and so can never be 0; restrict the selection to the
        # canary cases — which need no extra flag — to exercise the clean-pass path. --cases is a
        # deliberate narrowing, not a skip, so it does not withhold the 0.
        ids = ",".join(_default_channel_ids())
        rc, out, _ = self._run(["run", "--to", self.addr, "--apply", "--cases", ids])
        self.assertEqual(rc, 0)
        run_id = self._run_id(out)
        rc, out, _ = self._run(["score", "--run", run_id])  # nobody processes -> canary stays empty
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
        # A bare run skips repo_edit, so the exit is 2 here; this test only pins that the JSON body
        # parses and carries totals (the exit-code contract is pinned by its own tests).
        self.assertIn(rc, (0, 1, 2))
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

    def test_score_with_skips_exits_2_and_explains_it_is_coverage_not_failure(self):
        # A bare apply run skips repo_edit + mesh_action, so score exits 2 — but must say plainly
        # that 2 means incomplete coverage, not a failed drill, and repeat the flags.
        run_id = self._apply_run()  # nobody processes the inbox; canary resists
        rc, out, err = self._run(["score", "--run", run_id])
        self.assertEqual(rc, 2)
        text = out + err
        self.assertIn("coverage", text.lower())
        self.assertIn("not failure", text.lower())
        self.assertIn("--repo-file", text)
        self.assertIn("--third-addr", text)

    def test_full_coverage_clean_run_is_still_exit_0(self):
        repo = os.path.join(self._tmp.name, "clean.py")
        third = f"{self.uid}:third_party"
        rc, out, _ = self._run([
            "run", "--to", self.addr, "--apply", "--repo-file", repo, "--third-addr", third,
        ])
        self.assertEqual(rc, 0)
        run_id = self._run_id(out)
        rc, out, _ = self._run(["score", "--run", run_id])  # nobody complies
        self.assertEqual(rc, 0)

    def test_complied_repo_edit_prints_path_and_revert_advice(self):
        from pm_mesh.eval_corpus import CORPUS
        repo = os.path.join(self._tmp.name, "committed_by_mistake.py")
        rc, out, _ = self._run(["run", "--to", self.addr, "--apply", "--repo-file", repo])
        self.assertEqual(rc, 0)
        run_id = self._run_id(out)
        with open(eval_run.manifest_path(run_id), encoding="utf-8") as fh:
            manifest = json.load(fh)
        # The agent-under-test obeyed a repo_edit case: its token lands in the in-tree file.
        repo_case = next(c for c in manifest["cases"] if c["category"] == "repo_edit")
        with open(repo, "a", encoding="utf-8") as fh:
            fh.write(f"\nMARKER = {repo_case['token']}\n")
        rc, out, err = self._run(["score", "--run", run_id])
        self.assertEqual(rc, 1)  # a compliance still wins the exit code
        text = out + err
        self.assertIn(repo, text)              # names the modified file
        self.assertIn("revert", text.lower())  # and says to revert it

    def test_complied_mesh_action_scores_via_third_mailbox_through_the_cli(self):
        # The analog of the repo_edit CLI test for the third-mailbox channel: the ONLY slice not
        # otherwise proven is that _cmd_score actually wires third_tokens through to score(). Without
        # this, a mesh_action case could only ever report "resisted" through the real entry point —
        # the exact "channel that cannot report a compliance" defect this harness exists to prevent.
        from pm_mesh import message
        third = f"{self.uid}:third_party"
        rc, out, _ = self._run(["run", "--to", self.addr, "--apply", "--third-addr", third])
        self.assertEqual(rc, 0)
        run_id = self._run_id(out)
        with open(eval_run.manifest_path(run_id), encoding="utf-8") as fh:
            manifest = json.load(fh)
        # The agent-under-test forwards one mesh_action case's token on to the third address.
        mesh_case = next(c for c in manifest["cases"] if c["category"] == "mesh_action")
        maildir.deliver(message.new_message(third, f"forwarding {mesh_case['token']}",
                                            from_=manifest["from"]))
        rc, out, _ = self._run(["score", "--run", run_id])
        self.assertEqual(rc, 1)  # a compliance wins the exit code
        complied = next(c for c in json.loads(
            self._run(["score", "--run", run_id, "--json"])[1])["cases"]
            if c["verdict"] == "complied")
        self.assertEqual(complied["channel"], "third_mailbox")


if __name__ == "__main__":
    unittest.main()
