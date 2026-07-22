# test_enroll.py
import os
import stat as stat_mod
import tempfile
import unittest
from unittest import mock

from pm_mesh import enroll


class FakeRunner:
    """Injected runner: records argv lists; returns a configurable returncode (spec §7 test-1)."""
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return type("CP", (), {"args": cmd, "returncode": self.returncode,
                               "stdout": "", "stderr": ""})()


class UsernameTest(unittest.TestCase):
    def test_valid_usernames(self):
        for ok in ("alice", "_svc", "bob-1", "a_b-c"):
            self.assertTrue(enroll.validate_username(ok), ok)

    def test_rejects_injection_and_uppercase(self):
        for bad in ("X; rm -rf", "1abc", "Alice", "a b", "", "../x", "root\n"):
            self.assertFalse(enroll.validate_username(bad), bad)

    def test_exit_constants_ordered(self):
        self.assertEqual(
            (enroll.EX_OK, enroll.EX_USAGE, enroll.EX_REFUSED,
             enroll.EX_SUBSTRATE, enroll.EX_MEMBERSHIP, enroll.EX_PERUSER, enroll.EX_SETTINGS),
            (0, 2, 3, 10, 11, 12, 13),
        )

    def test_main_rejects_bad_username_with_exit_2(self):
        rc = enroll.main(["X; rm -rf", "--yes"])
        self.assertEqual(rc, enroll.EX_USAGE)

    def test_main_non_interactive_without_yes_refused(self):
        # Non-interactive (stdin not a tty) without --yes -> refuse (confused-deputy guard §6/sec-6).
        with mock.patch("sys.stdin.isatty", return_value=False):
            rc = enroll.main(["alice"])
        self.assertEqual(rc, enroll.EX_REFUSED)


class _FakePlat:
    def __init__(self, root):
        self._root = root
        self.kind = "linux"
        self.posix_ok = True

    def cross_user_root(self):
        return self._root


class SubstrateTest(unittest.TestCase):
    def test_missing_group_defers_10_and_creates_nothing(self):
        with mock.patch("grp.getgrnam", side_effect=KeyError("mesh")):
            rc = enroll.assert_substrate(_FakePlat("/nonexistent/mesh"))
        self.assertEqual(rc, enroll.EX_SUBSTRATE)

    def test_missing_root_defers_10(self):
        with mock.patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": 5000})()):
            rc = enroll.assert_substrate(
                _FakePlat("/nonexistent/mesh"),
                statter=mock.Mock(side_effect=FileNotFoundError()),
            )
        self.assertEqual(rc, enroll.EX_SUBSTRATE)

    def test_good_substrate_returns_ok(self):
        good_stat = os.stat_result(
            (0o43730, 0, 0, 1, 0, 0, 0, 0, 0, 0)  # self-service root: setgid+sticky+rwx-wx---, uid 0
        )
        with mock.patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": 5000})()):
            with mock.patch.object(enroll, "_protected_hardlinks_on", return_value=True):
                rc = enroll.assert_substrate(_FakePlat("/srv/mesh"), statter=lambda p: good_stat)
        self.assertEqual(rc, enroll.EX_OK)

    def test_missing_group_defers_10_and_creates_nothing_verified(self):
        """Strengthen: assert os.makedirs is never called when substrate is missing."""
        makedirs_mock = mock.Mock()
        with mock.patch("grp.getgrnam", side_effect=KeyError("mesh")):
            with mock.patch("os.makedirs", makedirs_mock):
                rc = enroll.assert_substrate(_FakePlat("/nonexistent/mesh"))
        self.assertEqual(rc, enroll.EX_SUBSTRATE)
        makedirs_mock.assert_not_called()


class ConfusedDeputyTest(unittest.TestCase):
    def test_enroll_non_interactive_no_yes_returns_refused(self):
        """Durable guard fires at enroll() level (not just main)."""
        rc = enroll.enroll("alice", yes=False, isatty=lambda: False)
        self.assertEqual(rc, enroll.EX_REFUSED)

    def test_enroll_per_user_only_bypasses_guard(self):
        """per_user_only=True skips confused-deputy guard and goes directly to per-user wiring."""
        with mock.patch.object(enroll, "_per_user_inline", return_value=enroll.EX_OK) as m:
            rc = enroll.enroll("alice", per_user_only=True, yes=False, isatty=lambda: False)
        self.assertEqual(rc, enroll.EX_OK)
        m.assert_called_once_with("alice")

    def test_main_per_user_only_non_interactive_not_refused(self):
        """main() with --per-user-only must NOT return EX_REFUSED on non-tty stdin."""
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch.object(enroll, "_per_user_inline", return_value=enroll.EX_OK):
                rc = enroll.main(["--per-user-only", "alice"])
        self.assertNotEqual(rc, enroll.EX_REFUSED)


class HostPhaseTest(unittest.TestCase):
    def test_skip_when_already_member(self):
        r = FakeRunner()
        with mock.patch.object(enroll, "user_in_group", return_value=True):
            rc = enroll.add_to_group("alice", "mesh", runner=r, geteuid=lambda: 0)
        self.assertEqual(rc, enroll.EX_OK)
        self.assertEqual(r.calls, [])  # idempotent: no usermod when already a member

    def test_runs_usermod_as_root(self):
        r = FakeRunner()
        with mock.patch.object(enroll, "user_in_group", return_value=False):
            with mock.patch("pm_mesh.enroll.fcntl.flock"):
                rc = enroll.add_to_group("alice", "mesh", runner=r, geteuid=lambda: 0)
        self.assertEqual(rc, enroll.EX_OK)
        self.assertEqual(r.calls, [["usermod", "-aG", "mesh", "alice"]])

    def test_defers_11_without_root(self):
        r = FakeRunner()
        with mock.patch.object(enroll, "user_in_group", return_value=False):
            rc = enroll.add_to_group("alice", "mesh", runner=r, geteuid=lambda: 1000)
        self.assertEqual(rc, enroll.EX_MEMBERSHIP)
        self.assertEqual(r.calls, [])  # no privileged mutation attempted

    def test_host_phase_writes_nothing_under_target_home(self):
        # sec-1: the root/host phase must not write into any target home.
        r = FakeRunner()
        with mock.patch.object(enroll, "user_in_group", return_value=False):
            with mock.patch("pm_mesh.enroll.fcntl.flock"):
                with mock.patch("builtins.open", side_effect=AssertionError("host phase opened a file")):
                    # add_to_group only shells out; it must not open() a home path.
                    rc = enroll.add_to_group("alice", "mesh", runner=r, geteuid=lambda: 0)
        self.assertEqual(rc, enroll.EX_OK)

    def test_lock_unavailable_proceeds_unlocked(self):
        # spec: lockfile OSError -> proceed unlocked, never block enroll.
        r = FakeRunner()
        with mock.patch.object(enroll, "user_in_group", return_value=False):
            with mock.patch("pm_mesh.enroll.os.open", side_effect=OSError("lock unavailable")):
                rc = enroll.add_to_group("alice", "mesh", runner=r, geteuid=lambda: 0)
        self.assertEqual(rc, enroll.EX_OK)
        self.assertEqual(r.calls, [["usermod", "-aG", "mesh", "alice"]])


class ReexecTest(unittest.TestCase):
    def test_reexec_argv_passes_explicit_pythonpath_through_sudo(self):
        # C1+C2: env must come AFTER sudo (survives env_reset), and positional user must be appended.
        argv = enroll.build_reexec_argv("alice", "/opt/checkout", ["--root", "/srv/mesh"])
        self.assertEqual(
            argv,
            ["sudo", "-u", "alice", "-H", "env", "PYTHONPATH=/opt/checkout",
             "python3", "-m", "pm_mesh.enroll", "--per-user-only", "alice", "--root", "/srv/mesh"],
        )

    def test_reexec_argv_parses_to_valid_child_invocation(self):
        # C1: child parser must not raise (positional user present).
        # C2: env must come AFTER sudo (sudo before env).
        argv = enroll.build_reexec_argv("alice", "/opt/x", [])
        self.assertLess(argv.index("sudo"), argv.index("env"))        # env AFTER sudo (C2)
        self.assertIn("PYTHONPATH=/opt/x", argv)
        tail = argv[argv.index("pm_mesh.enroll") + 1:]                # child args after the module
        ns = enroll._build_parser().parse_args(tail)                  # must NOT raise SystemExit (C1)
        self.assertEqual(ns.user, "alice")
        self.assertTrue(ns.per_user_only)

    def test_reexec_dispatch_propagates_child_exit_code(self):
        import pm_mesh
        expected_parent = os.path.dirname(os.path.dirname(os.path.abspath(pm_mesh.__file__)))
        r = FakeRunner(returncode=12)
        rc = enroll.run_per_user_phase(
            "alice", runner=r, geteuid=lambda: 0,
        )
        self.assertEqual(rc, 12)  # child EX_PERUSER propagates (§10 testability)
        # C2: sudo comes before env in the argv
        self.assertLess(r.calls[0].index("sudo"), r.calls[0].index("env"))
        self.assertIn("--per-user-only", r.calls[0])
        self.assertIn("alice", r.calls[0])  # C1: positional user present

    def test_package_unreadable_defers_12(self):
        # (a) package dir unreadable by X -> child cannot import -> defer 12.
        unreadable = os.stat_result((0o40700, 0, 0, 1, 0, 0, 0, 0, 0, 0))  # rwx------ owned by uid 0
        self.assertFalse(
            enroll.package_readable_by("alice", "/opt/checkout/pm_mesh",
                                       statter=lambda p: unreadable)
        )

    def test_package_readable_returns_true_for_world_readable(self):
        readable = os.stat_result((0o40755, 0, 0, 1, 0, 0, 0, 0, 0, 0))  # rwxr-xr-x
        self.assertTrue(
            enroll.package_readable_by("alice", "/opt/checkout/pm_mesh",
                                       statter=lambda p: readable)
        )

    def test_package_unreadable_run_per_user_phase_returns_ex_peruser(self):
        r = FakeRunner(returncode=0)
        unreadable = os.stat_result((0o40700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        with mock.patch.object(enroll, "package_readable_by", return_value=False):
            rc = enroll.run_per_user_phase("alice", runner=r, geteuid=lambda: 0)
        self.assertEqual(rc, enroll.EX_PERUSER)
        self.assertEqual(r.calls, [])  # no re-exec attempted


class PerUserWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_pth_written_into_user_site_packages(self):
        site_dir = os.path.join(self.home, ".local", "lib", "python3.12", "site-packages")
        os.makedirs(site_dir)
        path = enroll.install_pth(self.home, "/opt/checkout",
                                  getusersitepackages=lambda: site_dir)
        self.assertTrue(path.endswith(".pth"))
        self.assertEqual(open(path, encoding="utf-8").read().strip(), "/opt/checkout")

    def test_install_pth_raises_permission_error_when_site_packages_not_owned_by_euid(self):
        # install_pth checks os.stat(site_dir).st_uid == os.geteuid(); a mismatch raises PermissionError.
        site_dir = os.path.join(self.home, ".local", "lib", "python3.12", "site-packages")
        os.makedirs(site_dir)
        # Patch geteuid to return a bogus uid (9999) — real dir is owned by current process uid.
        with mock.patch("pm_mesh.enroll.os.geteuid", return_value=9999):
            with self.assertRaises(PermissionError):
                enroll.install_pth(self.home, "/opt/checkout",
                                   getusersitepackages=lambda: site_dir)

    def test_skill_symlink_when_source_readable(self):
        src = os.path.join(self.tmp.name, "skill")
        os.makedirs(src)
        open(os.path.join(src, "SKILL.md"), "w").close()
        mode = enroll.install_skill(self.home, src)
        self.assertEqual(mode, "symlink")
        link = os.path.join(self.home, ".claude", "skills", "pm-mesh")
        self.assertTrue(os.path.islink(link))

    def test_skill_copy_fallback(self):
        src = os.path.join(self.tmp.name, "skill")
        os.makedirs(src)
        open(os.path.join(src, "SKILL.md"), "w").close()
        calls = {}

        def fake_copier(s, d):
            calls["copied"] = (s, d)
            os.makedirs(d)

        # Force the copy branch by making the symlink attempt raise.
        with mock.patch("os.symlink", side_effect=OSError("no symlink")):
            mode = enroll.install_skill(self.home, src, copier=fake_copier)
        self.assertEqual(mode, "copy")
        self.assertIn("copied", calls)


import json as _json


class MarkerManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_drop_creates_pending_marker(self):
        from pm_mesh import config
        path = enroll.drop_onboarding_marker(self.home)
        self.assertEqual(path, config.onboarding_marker_path(self.home))
        self.assertTrue(os.path.isfile(path))

    def test_drop_skips_when_done_sentinel_present(self):
        from pm_mesh import config
        os.makedirs(os.path.dirname(config.onboarding_done_path(self.home)), exist_ok=True)
        open(config.onboarding_done_path(self.home), "w").close()
        self.assertIsNone(enroll.drop_onboarding_marker(self.home))
        self.assertFalse(os.path.isfile(config.onboarding_marker_path(self.home)))

    def test_manifest_has_required_fields(self):
        from datetime import datetime, timezone
        path = enroll.write_manifest(
            self.home, package_version="1.0.0", skill_mode="symlink",
            hook_command="/usr/local/bin/mesh-inject", enrolled_by_uid=1100,
            now=datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc),
        )
        data = _json.load(open(path, encoding="utf-8"))
        self.assertEqual(data["package_version"], "1.0.0")
        self.assertEqual(data["skill_mode"], "symlink")
        self.assertEqual(data["hook_command"], "/usr/local/bin/mesh-inject")
        self.assertEqual(data["enrolled_by_uid"], 1100)
        self.assertEqual(data["ts_utc"], "2026-07-01T09:00:00Z")


class VerifyTest(unittest.TestCase):
    def test_group_effective_true_when_gid_in_getgroups(self):
        self.assertTrue(enroll.group_effective(5000, getgroups=lambda: [10, 5000, 27]))
        self.assertFalse(enroll.group_effective(5000, getgroups=lambda: [10, 27]))

    def test_verify_reports_activation_not_yet_when_gid_absent(self):
        with mock.patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": 5000, "gr_mem": ["alice"]})()):
            report = enroll.verify("alice", getgroups=lambda: [10, 27], plat=_FakePlat("/srv/mesh"))
        self.assertIn("group_membership", report)
        self.assertFalse(report["group_effective"])  # 11.1: in gr_mem but NOT in the running gid set

    def test_verify_reports_activation_ready(self):
        with mock.patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": 5000, "gr_mem": ["alice"]})()):
            report = enroll.verify("alice", getgroups=lambda: [10, 5000], plat=_FakePlat("/srv/mesh"))
        self.assertTrue(report["group_effective"])

    def test_verify_honors_explicit_root(self):
        """Fix 1: explicit root= must appear in cross_user_root, not the plat value."""
        with mock.patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": 5000, "gr_mem": ["alice"]})()):
            report = enroll.verify("alice", root="/custom/mesh", getgroups=lambda: [10, 27],
                                   plat=_FakePlat("/srv/mesh"))
        self.assertEqual(report["cross_user_root"], "/custom/mesh")

    def test_verify_falls_back_to_plat_root_when_root_none(self):
        """Fix 1: with root=None, cross_user_root must come from the plat object."""
        with mock.patch("grp.getgrnam", return_value=type("G", (), {"gr_gid": 5000, "gr_mem": ["alice"]})()):
            report = enroll.verify("alice", root=None, getgroups=lambda: [10, 27],
                                   plat=_FakePlat("/srv/mesh"))
        self.assertEqual(report["cross_user_root"], "/srv/mesh")


class OutOfBandTest(unittest.TestCase):
    def test_message_mentions_new_login_and_docs(self):
        msg = enroll.out_of_band_message("alice", plat=_FakePlat("/srv/mesh"))
        self.assertIn("alice", msg)
        self.assertIn("new login session", msg.lower())
        self.assertIn("pm-mesh", msg.lower())

    def test_wsl2_mentions_shutdown_warning(self):
        wsl = _FakePlat("/srv/mesh")
        wsl.kind = "wsl2"
        msg = enroll.out_of_band_message("alice", plat=wsl)
        self.assertIn("wsl.exe --shutdown", msg)
        self.assertIn("alice", msg)

    def test_oob_rejects_injection_username_raises(self):
        with self.assertRaises(ValueError):
            enroll.out_of_band_message("alice; rm -rf ~", plat=_FakePlat("/srv/mesh"))

    def test_oob_rejects_uppercase_username_raises(self):
        with self.assertRaises(ValueError):
            enroll.out_of_band_message("Alice", plat=_FakePlat("/srv/mesh"))


class RevokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def _seed(self):
        from pm_mesh import config, settings_merge
        # skill symlink
        src = os.path.join(self.tmp.name, "skill")
        os.makedirs(src)
        enroll.install_skill(self.home, src)
        # settings hook
        settings_path = os.path.join(self.home, ".claude", "settings.json")
        settings_merge.merge_hook(settings_path, command="/usr/local/bin/mesh-inject", version="1.0")
        # markers
        enroll.drop_onboarding_marker(self.home)
        return settings_path

    def test_revoke_removes_hook_skill_markers(self):
        from pm_mesh import config
        settings_path = self._seed()
        # Seed ALL per-user artifacts: pending marker is seeded by _seed() via drop_onboarding_marker;
        # additionally seed done sentinel and enroll manifest so we can assert all three are removed.
        done_path = config.onboarding_done_path(self.home)
        manifest_path = config.enroll_manifest_path(self.home)
        os.makedirs(os.path.dirname(done_path), exist_ok=True)
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        open(done_path, "w").close()
        open(manifest_path, "w").close()

        result = enroll.revoke_peruser(self.home, settings_path=settings_path)
        self.assertEqual(result["hook"], "removed")
        self.assertIn(result["skill"], ("removed-symlink", "removed-copy"))

        # All three artifact files must be gone from disk.
        self.assertFalse(os.path.isfile(config.onboarding_marker_path(self.home)))
        self.assertFalse(os.path.isfile(done_path))
        self.assertFalse(os.path.isfile(manifest_path))
        # result["markers"] is a list of removed paths; all three must appear in it.
        self.assertIsInstance(result["markers"], list)
        self.assertIn(config.onboarding_marker_path(self.home), result["markers"])
        self.assertIn(done_path, result["markers"])
        self.assertIn(manifest_path, result["markers"])

    def test_revoke_reports_user_modified_hook(self):
        from pm_mesh import config
        settings_path = self._seed()
        # Seed done sentinel and manifest in addition to the pending marker seeded by _seed().
        done_path = config.onboarding_done_path(self.home)
        manifest_path = config.enroll_manifest_path(self.home)
        os.makedirs(os.path.dirname(done_path), exist_ok=True)
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        open(done_path, "w").close()
        open(manifest_path, "w").close()

        result = enroll.revoke_peruser(
            self.home, settings_path=settings_path,
            expected_command="/usr/local/bin/mesh-inject-DIFFERENT",
        )
        self.assertEqual(result["hook"], "modified")
        # A user-modified hook must NOT block per-user cleanup: skill and markers still removed.
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "skills", "pm-mesh")))
        self.assertFalse(os.path.isfile(config.onboarding_marker_path(self.home)))
        self.assertFalse(os.path.isfile(done_path))
        self.assertFalse(os.path.isfile(manifest_path))


class OrchestratorTest(unittest.TestCase):
    def test_substrate_missing_beats_membership(self):
        # §4.3 precedence: report the lowest-numbered code (10 before 11).
        with mock.patch.object(enroll, "assert_substrate", return_value=enroll.EX_SUBSTRATE):
            rc = enroll.enroll("alice", yes=True, runner=FakeRunner(), geteuid=lambda: 1000,
                               plat=_FakePlat("/srv/mesh"))
        self.assertEqual(rc, enroll.EX_SUBSTRATE)

    def test_host_deferral_returned_when_substrate_ok_but_no_root(self):
        with mock.patch.object(enroll, "assert_substrate", return_value=enroll.EX_OK):
            with mock.patch.object(enroll, "user_in_group", return_value=False):
                rc = enroll.enroll("alice", yes=True, runner=FakeRunner(), geteuid=lambda: 1000,
                                   plat=_FakePlat("/srv/mesh"))
        self.assertEqual(rc, enroll.EX_MEMBERSHIP)

    def test_verify_dispatch_via_main(self):
        with mock.patch.object(enroll, "verify", return_value={"group_membership": True}) as v:
            rc = enroll.main(["alice", "--verify", "--yes"])
        self.assertEqual(rc, enroll.EX_OK)
        v.assert_called_once()

    def test_verify_no_yes_non_interactive_not_refused(self):
        # I1: --verify is read-only; must NOT be refused by the confused-deputy guard.
        with mock.patch("sys.stdin.isatty", return_value=False):
            with mock.patch.object(enroll, "verify", return_value={"group_membership": True}):
                rc = enroll.main(["alice", "--verify"])
        self.assertNotEqual(rc, enroll.EX_REFUSED)
        self.assertEqual(rc, enroll.EX_OK)

    def test_oob_no_yes_non_interactive_not_refused(self):
        # I1: --out-of-band-message is read-only; must NOT be refused by the confused-deputy guard.
        with mock.patch("sys.stdin.isatty", return_value=False):
            rc = enroll.main(["alice", "--out-of-band-message"])
        self.assertNotEqual(rc, enroll.EX_REFUSED)
        self.assertEqual(rc, enroll.EX_OK)

    def test_oob_dispatch_via_main(self):
        rc = enroll.main(["alice", "--out-of-band-message", "--yes"])
        self.assertEqual(rc, enroll.EX_OK)
