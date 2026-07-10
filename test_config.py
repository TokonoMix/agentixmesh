import os
import stat
import tempfile
import unittest
from unittest import mock

from pm_mesh import config


class MeshRootTest(unittest.TestCase):
    def test_mesh_root_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "mesh")
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                got = config.mesh_root()
            self.assertEqual(got, root)
            self.assertTrue(os.path.isdir(got))

    def test_mesh_root_created_0700(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "mesh")
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                got = config.mesh_root()
            mode = stat.S_IMODE(os.stat(got).st_mode)
            self.assertEqual(mode, 0o700)

    def test_mesh_root_defaults_to_xdg(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_DATA_HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("MESH_ROOT", None)
                got = config.mesh_root()
            self.assertEqual(got, os.path.join(tmp, "pm-mesh"))

    def test_mesh_root_defaults_to_local_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("MESH_ROOT", None)
                os.environ.pop("XDG_DATA_HOME", None)
                got = config.mesh_root()
            self.assertEqual(got, os.path.join(tmp, ".local", "share", "pm-mesh"))

    def test_mesh_root_idempotent_on_existing_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "mesh")
            with mock.patch.dict(os.environ, {"MESH_ROOT": root}, clear=False):
                first = config.mesh_root()
                second = config.mesh_root()
            self.assertEqual(first, second)
            self.assertTrue(os.path.isdir(second))


class CurrentAddressTest(unittest.TestCase):
    def test_current_address_form(self):
        with mock.patch("os.getuid", return_value=1000), \
                mock.patch("os.getcwd", return_value="/home/user/gallery"):
            self.assertEqual(config.current_address(), "1000:gallery")

    def test_current_address_sanitizes_project(self):
        with mock.patch("os.getuid", return_value=1000), \
                mock.patch("os.getcwd", return_value="/srv/My Project!@#"):
            uid, project = config.parse_address(config.current_address())
        self.assertEqual(uid, 1000)
        # only [A-Za-z0-9._-] survive; disallowed chars become '_'
        for ch in project:
            self.assertIn(ch, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")

    def test_current_address_empty_basename_becomes_underscore(self):
        with mock.patch("os.getuid", return_value=1000), \
                mock.patch("os.getcwd", return_value="/"):
            self.assertEqual(config.current_address(), "1000:_")

    def test_current_address_all_disallowed_stays_valid(self):
        # every disallowed char is replaced by '_'; result must be a parseable address
        with mock.patch("os.getuid", return_value=1000), \
                mock.patch("os.getcwd", return_value="/srv/@@@"):
            addr = config.current_address()
        uid, project = config.parse_address(addr)
        self.assertEqual(uid, 1000)
        self.assertEqual(project, "___")


class ParseAddressTest(unittest.TestCase):
    def test_parse_roundtrip(self):
        self.assertEqual(config.parse_address("1000:gallery"), (1000, "gallery"))

    def test_parse_returns_int_uid(self):
        uid, project = config.parse_address("42:peer")
        self.assertIsInstance(uid, int)
        self.assertEqual(uid, 42)

    def test_parse_rejects_non_numeric_uid(self):
        with self.assertRaises(ValueError):
            config.parse_address("abc:gallery")

    def test_parse_rejects_missing_colon(self):
        with self.assertRaises(ValueError):
            config.parse_address("notanaddress")

    def test_parse_rejects_empty_project(self):
        with self.assertRaises(ValueError):
            config.parse_address("1000:")

    def test_parse_rejects_bad_project_chars(self):
        with self.assertRaises(ValueError):
            config.parse_address("1000:has space")


class OnboardingPathTest(unittest.TestCase):
    def test_marker_path_under_local_share(self):
        p = config.onboarding_marker_path(home="/home/x")
        self.assertEqual(p, "/home/x/.local/share/pm-mesh/onboarding-pending")

    def test_done_and_manifest_paths(self):
        self.assertEqual(
            config.onboarding_done_path(home="/home/x"),
            "/home/x/.local/share/pm-mesh/onboarding-done",
        )
        self.assertEqual(
            config.enroll_manifest_path(home="/home/x"),
            "/home/x/.local/share/pm-mesh/enroll-manifest.json",
        )

    def test_cross_user_root_mode_is_3730_self_service(self):
        # Self-service root: setgid+sticky+rwx-wx--- so group members create their own mailbox
        # (group-write) but cannot enumerate the mesh (no group-read); sticky blocks cross-delete.
        self.assertEqual(config.CROSS_USER_ROOT_MODE, 0o3730)

    def test_cross_user_enabled_follows_platform_root(self):
        # MESH_CROSS_USER="" is neither truthy nor falsy, so it falls through to the
        # MESH_ROOT comparison. Setting it inside patch.dict auto-restores the original
        # on exit (no manual os.environ.pop that would leak across tests).
        with mock.patch("pm_mesh.config.cross_user_root", return_value="/srv/mesh"):
            with mock.patch.dict(
                os.environ, {"MESH_ROOT": "/srv/mesh", "MESH_CROSS_USER": ""}, clear=False
            ):
                self.assertTrue(config.cross_user_enabled())


if __name__ == "__main__":
    unittest.main()
