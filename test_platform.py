# test_platform.py
import io
import os
import tempfile
import unittest
from unittest import mock

from pm_mesh import platform


class StructuralGuardTest(unittest.TestCase):
    def test_posix_ok_true_on_this_linux_host(self):
        self.assertTrue(platform.posix_structural_ok())

    def test_guard_does_not_call_stat_or_check_st_uid(self):
        # The guard must NOT depend on any st_uid value (root == uid 0 is legitimate).
        with mock.patch("os.stat", side_effect=AssertionError("guard must not stat")):
            self.assertTrue(platform.posix_structural_ok())

    def test_guard_false_when_os_name_not_posix(self):
        with mock.patch("os.name", "nt"):
            self.assertFalse(platform.posix_structural_ok())


class VariantDetectionTest(unittest.TestCase):
    def _detect(self, platform_str, osrelease="", version="", run_wsl=False):
        return platform.detect(
            platform_str=platform_str,
            proc_version_reader=lambda: version,
            mountinfo_reader=lambda: "",
            uname=lambda: type("U", (), {"release": osrelease})(),
            run_probe=lambda p: run_wsl,
        )

    def test_plain_linux(self):
        self.assertEqual(self._detect("linux", osrelease="6.8.0-generic").kind, platform.LINUX)

    def test_wsl2_via_wsl2_suffix(self):
        self.assertEqual(self._detect("linux", osrelease="5.15.0-WSL2").kind, platform.WSL2)

    def test_wsl2_via_microsoft_standard(self):
        self.assertEqual(
            self._detect("linux", osrelease="5.15.0-microsoft-standard").kind, platform.WSL2
        )

    def test_wsl2_via_run_wsl_present(self):
        self.assertEqual(self._detect("linux", osrelease="x", run_wsl=True).kind, platform.WSL2)

    def test_wsl1_fails_closed(self):
        # microsoft-tagged but NOT a WSL2 marker and /run/WSL absent -> WSL1 -> reject.
        d = self._detect("linux", osrelease="4.4.0-19041-Microsoft", version="Microsoft")
        self.assertEqual(d.kind, platform.WSL1)
        self.assertFalse(d.posix_ok)

    def test_macos(self):
        self.assertEqual(self._detect("darwin").kind, platform.MACOS)

    def test_non_posix(self):
        d = self._detect("win32")
        self.assertEqual(d.kind, platform.UNSUPPORTED)
        self.assertFalse(d.posix_ok)

    def test_root_dir_linux_vs_macos(self):
        self.assertEqual(self._detect("linux").cross_user_root(), "/srv/mesh")
        self.assertEqual(self._detect("darwin").cross_user_root(), "/Users/Shared/mesh")

    def test_wsl2_posix_ok_true(self):
        # WSL2 must set posix_ok=True (symmetric invariant to WSL1 -> False).
        d = self._detect("linux", osrelease="5.15.0-microsoft-standard")
        self.assertEqual(d.kind, platform.WSL2)
        self.assertTrue(d.posix_ok)

    def test_benign_wsl_substring_classified_as_linux(self):
        # "swsl" or "mywsl-kernel" must NOT be classified as WSL1/WSL2.
        d = self._detect("linux", osrelease="6.8.0-swsl-custom")
        self.assertEqual(d.kind, platform.LINUX)

    def test_injectable_realpath_honored(self):
        # mesh_root_fs_type must use the injected realpath, not os.path.realpath.
        fake_realpath = lambda p: "/srv/mesh"
        ft = platform.mesh_root_fs_type(
            "/some/symlink",
            mountinfo_reader=lambda: _MOUNTINFO,
            realpath=fake_realpath,
        )
        self.assertEqual(ft, "ext4")


_MOUNTINFO = (
    "21 1 0:20 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
    "30 21 0:26 / /srv rw,relatime shared:5 - ext4 /dev/sdb1 rw\n"
    "40 21 0:99 / /mnt/c rw,relatime - 9p drvfs rw\n"
    "50 21 0:31 / /tmp rw,relatime - tmpfs tmpfs rw\n"
)


class FsTypeTest(unittest.TestCase):
    def _fstype(self, root):
        return platform.mesh_root_fs_type(root, mountinfo_reader=lambda: _MOUNTINFO)

    def test_srv_is_ext4_allowed(self):
        ft = self._fstype("/srv/mesh")
        self.assertEqual(ft, "ext4")
        self.assertTrue(platform.fs_type_allowed(ft))

    def test_drvfs_9p_refused(self):
        ft = self._fstype("/mnt/c/mesh")
        self.assertEqual(ft, "9p")
        self.assertFalse(platform.fs_type_allowed(ft))

    def test_tmpfs_allowed(self):
        self.assertTrue(platform.fs_type_allowed(self._fstype("/tmp/x")))

    def test_unknown_and_fuse_refused(self):
        self.assertFalse(platform.fs_type_allowed("cifs"))
        self.assertFalse(platform.fs_type_allowed("fuse.sshfs"))
        self.assertFalse(platform.fs_type_allowed(None))
        for bad in ("vfat", "ntfs", "exfat", "v9fs", "cifs", "drvfs"):
            self.assertFalse(platform.fs_type_allowed(bad))
        for good in ("ext4", "ext3", "xfs", "btrfs", "f2fs", "zfs", "tmpfs"):
            self.assertTrue(platform.fs_type_allowed(good))


class CaseProbeTest(unittest.TestCase):
    def test_case_sensitive_fs_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(platform.is_case_insensitive(d))

    def test_probe_uses_injected_opener_result(self):
        # Simulate a case-insensitive FS: stat("X") succeeds after creating "x".
        with tempfile.TemporaryDirectory() as d:
            seen = {}

            def fake_open(path, flags, mode=0o600):
                seen["created"] = path
                return os.open(seen["created"], flags, mode)  # real create of the lower-case probe

            # Force the "insensitive" branch by monkeypatching os.path.exists for the upper variant.
            with mock.patch("os.path.exists", return_value=True):
                self.assertTrue(platform.is_case_insensitive(d, opener=fake_open))
