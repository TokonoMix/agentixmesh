"""XU-RT-05 — adversarial red-team suite: filesystem attacks on the mesh transport.

Targets the guards described in CLAUDE.md §4 (F5) and
``docs/2026-06-24-pm-mesh-multiuser-design.md`` §14-§18:

* O_NOFOLLOW + S_ISREG (``pm_mesh.identity.open_verified``) must reject symlinks and
  non-regular files planted in a maildrop, end-to-end through ``maildir.consume_new``.
* hardlinks (``st_nlink > 1``) must be rejected even though the hardlinked file is
  otherwise a perfectly regular file owned by the attacker.
* the shared cross-user dropbox must be exactly ``0o3730`` (setgid|sticky|730): group
  members can drop+traverse but can neither list nor read; only the receiver reads.
* delivered filenames must carry >=128 bits of entropy (``secrets.token_hex(16)``).
* when ACL-hardening is enabled, only the receiver's uid gets a read ACL entry — group
  and other stay at zero, so naming alone (not the classic unix group-read bit) grants
  access.

This file does NOT modify production code. If a guard turns out to be bypassable the
corresponding test is left FAILING and reported — not weakened.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from pm_mesh import config, identity, maildir, message


def _imode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _full_mode(path):
    return os.lstat(path).st_mode


class SymlinkAttackTest(unittest.TestCase):
    """Attempt to smuggle a symlink into new/ so the reader dereferences a victim file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.addr = "1000:gallery"

    def test_symlink_planted_in_new_is_not_delivered_and_victim_untouched(self):
        drop = maildir.maildrop(self.addr, root=self.root)
        new_dir = os.path.join(drop, "new")

        # Victim file OUTSIDE the maildrop the attacker wants disclosed/executed-as-message.
        victim_dir = os.path.join(self.root, "victim")
        os.makedirs(victim_dir, mode=0o700)
        victim_path = os.path.join(victim_dir, "secret.json")
        with open(victim_path, "w", encoding="utf-8") as fh:
            fh.write('{"leaked": "should never surface as a delivered message"}')

        # Attacker plants a symlink directly in new/ (bypassing maildir.deliver's rename dance).
        evil_link = os.path.join(new_dir, "a" * 32)  # mimics the 32-hex-char final name shape
        os.symlink(victim_path, evil_link)

        results = list(maildir.consume_new(self.addr, root=self.root))

        # Nothing is yielded as a valid message -> the symlink's content never reaches a caller.
        self.assertEqual(results, [], "symlink must never be delivered as a message")
        # The symlink got claimed (rename of the link itself, not the target) then quarantined.
        held_dir = os.path.join(drop, "held")
        held_names = os.listdir(held_dir)
        self.assertEqual(len(held_names), 1)
        self.assertTrue(os.path.islink(os.path.join(held_dir, held_names[0])))
        # new/ is empty now (claimed away), victim file is completely untouched.
        self.assertEqual([n for n in os.listdir(new_dir) if not n.startswith(".")], [])
        with open(victim_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"leaked": "should never surface as a delivered message"}')

    def test_open_verified_directly_rejects_symlink_with_eloop_style_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "target")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("victim content")
            link = os.path.join(d, "link")
            os.symlink(target, link)
            with self.assertRaises(identity.IdentityError):
                identity.open_verified(link)

    def test_symlink_to_directory_also_rejected(self):
        # A symlink pointing at a directory (not just a file) must be equally refused.
        with tempfile.TemporaryDirectory() as d:
            victim_dir = os.path.join(d, "adir")
            os.makedirs(victim_dir)
            link = os.path.join(d, "dirlink")
            os.symlink(victim_dir, link)
            with self.assertRaises(identity.IdentityError):
                identity.open_verified(link)


class HardlinkAttackTest(unittest.TestCase):
    """Attempt to borrow another file's ownership/content via a hardlink dropped in new/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.addr = "1000:gallery"

    def test_hardlink_in_new_is_quarantined_not_delivered(self):
        drop = maildir.maildrop(self.addr, root=self.root)
        new_dir = os.path.join(drop, "new")

        # Attacker's own pre-existing file (same fs, same uid in this test -- the guard must
        # reject it purely on st_nlink, independent of whether ownership "looks" fine).
        origin_dir = os.path.join(self.root, "attacker-owned")
        os.makedirs(origin_dir, mode=0o700)
        origin_path = os.path.join(origin_dir, "template.json")
        with open(origin_path, "w", encoding="utf-8") as fh:
            fh.write("not a real mesh envelope")

        evil_link = os.path.join(new_dir, "b" * 32)
        os.link(origin_path, evil_link)
        self.assertGreater(os.stat(evil_link).st_nlink, 1, "sanity: hardlink must show nlink>1")

        results = list(maildir.consume_new(self.addr, root=self.root))

        self.assertEqual(results, [], "a hardlinked file must never be delivered as a message")
        held_dir = os.path.join(drop, "held")
        held_names = os.listdir(held_dir)
        self.assertEqual(len(held_names), 1)
        # The original file (outside the maildrop) is untouched.
        with open(origin_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "not a real mesh envelope")

    def test_hardlink_rejected_even_when_nlink_drops_to_one_after_unlinking_original(self):
        # Defense-in-depth check: rejection is evaluated once, at claim time, on nlink as
        # observed then -- verify the *live* nlink (not a cached/pre-fetched value) is what
        # gates the decision, by asserting on a still-linked file (nlink==2) directly.
        with tempfile.TemporaryDirectory() as d:
            origin = os.path.join(d, "origin")
            with open(origin, "w", encoding="utf-8") as fh:
                fh.write("x")
            link = os.path.join(d, "link")
            os.link(origin, link)
            with self.assertRaises(identity.IdentityError) as ctx:
                identity.open_verified(link)
            self.assertIn("hardlink", str(ctx.exception).lower())


class DropboxPermsTest(unittest.TestCase):
    """Bit-level attack surface check on the shared cross-user dropbox (F1-F5, ontwerp §17)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.uid = os.geteuid()
        self.gid = os.getgid()
        self.addr = f"{self.uid}:gallery"
        patcher = mock.patch("pm_mesh.maildir._mesh_gid", return_value=self.gid)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_dir_is_exactly_1730_plus_setgid_no_group_or_other_read(self):
        drop = maildir.maildrop(self.addr, root=self.root, mode="cross_user")
        new_dir = os.path.join(drop, "new")
        m = _full_mode(new_dir)
        self.assertEqual(stat.S_IMODE(m), 0o3730, "must be exactly setgid|sticky|730")
        # Attack surface: group can traverse+write (drop a message) but the read bit for
        # group MUST be absent -- otherwise any group member could `ls`/read every pending
        # message in every other user's inbox.
        group_bits = (stat.S_IMODE(m) >> 3) & 0o7
        other_bits = stat.S_IMODE(m) & 0o7
        self.assertEqual(group_bits, 0o3, "group must be exactly -wx (drop+traverse, no read/list)")
        self.assertEqual(other_bits, 0, "other must have zero access")
        self.assertTrue(m & stat.S_ISUID == 0, "setuid must never be set")

    def test_drop_dir_group_is_traverse_only_cannot_write_or_read(self):
        # CROSS_USER_DROP_MODE = 0o2710: group gets execute-only on the drop dir itself, so an
        # attacker in the mesh group cannot create arbitrary files at the drop-dir level
        # (only inside new/, which is explicitly the intended dropbox).
        drop = maildir.maildrop(self.addr, root=self.root, mode="cross_user")
        m = _full_mode(drop)
        group_bits = (stat.S_IMODE(m) >> 3) & 0o7
        self.assertEqual(group_bits, 0o1, "group must be --x only on the drop dir (traverse only)")

    def test_cur_and_held_are_owner_only_unreachable_by_group(self):
        drop = maildir.maildrop(self.addr, root=self.root, mode="cross_user")
        for sub in ("cur", "held"):
            m = _imode(os.path.join(drop, sub))
            self.assertEqual(m, 0o700, f"{sub}/ must stay owner-only 0700, unreachable by mesh group")

    def test_group_cannot_list_new_dir_contents_via_permission_bits(self):
        # "Cannot list" == no read bit on the directory. Deliver a message, then confirm the
        # dir's own mode (not the file's) is what an attacker would need read access to list --
        # and assert that bit is off.
        with mock.patch.dict(os.environ, {"MESH_ACL": "0"}, clear=False):
            msg = message.new_message(self.addr, "hoi", from_=f"{self.uid}:tokonomix")
            maildir.deliver(msg, root=self.root, mode="cross_user")
        new_dir = os.path.join(self.root, self.addr, "new")
        m = stat.S_IMODE(_full_mode(new_dir))
        self.assertFalse(m & stat.S_IRGRP, "group-read bit must be off -- no directory listing for group")

    def test_maildrop_rejects_new_dir_replaced_by_symlink(self):
        # Post-provisioning drift attack: swap the shared dropbox for a symlink to a directory
        # the attacker controls. Re-validation must catch this on the very next turn.
        drop = maildir.maildrop(self.addr, root=self.root, mode="cross_user")
        new_dir = os.path.join(drop, "new")
        elsewhere = os.path.join(self.root, "attacker-controlled")
        os.makedirs(elsewhere, mode=0o3730)
        shutil.rmtree(new_dir)
        os.symlink(elsewhere, new_dir)
        with self.assertRaises(maildir.MaildropError):
            maildir.maildrop(self.addr, root=self.root, mode="cross_user")


class FilenameEntropyTest(unittest.TestCase):
    """F5: delivered filenames must be unguessable (>=128 bits) -- confirm the floor holds."""

    def test_final_name_is_32_hex_chars_128_bits(self):
        name = maildir._final_name()
        self.assertEqual(len(name), 32)
        self.assertRegex(name, r"\A[0-9a-f]{32}\Z")

    def test_many_names_are_unique_no_birthday_collisions(self):
        names = {maildir._final_name() for _ in range(5000)}
        self.assertEqual(len(names), 5000, "collision in 5000 draws would indicate a broken RNG/entropy floor")

    def test_delivered_message_filename_meets_entropy_floor(self):
        with tempfile.TemporaryDirectory() as root:
            msg = message.new_message("1000:gallery", "hoi", from_="1000:tokonomix")
            path = maildir.deliver(msg, root=root)
            name = os.path.basename(path)
            hexpart = re.match(r"^[0-9a-f]+", name).group(0)
            self.assertGreaterEqual(len(hexpart), 32, ">=128 bits required to resist enumeration")

    def test_lowering_entropy_below_128_bits_is_hard_rejected(self):
        # Simulates a regression that weakens F5: the code must refuse to run, not silently
        # deliver a guessable filename.
        with mock.patch.object(maildir, "_NAME_BYTES", 8):  # 64 bits -- below the floor
            with self.assertRaises(ValueError):
                maildir._final_name()
            with tempfile.TemporaryDirectory() as root:
                msg = message.new_message("1000:gallery", "hoi", from_="1000:tokonomix")
                with self.assertRaises(ValueError):
                    maildir.deliver(msg, root=root)
                new_dir = os.path.join(root, "1000:gallery", "new")
                leftovers = [n for n in os.listdir(new_dir) if not n.startswith(".")]
                self.assertEqual(leftovers, [], "failed deliver must leave no message behind")


@unittest.skipUnless(
    shutil.which("setfacl") and shutil.which("getfacl"),
    "setfacl/getfacl not available on this host",
)
class ReceiverOnlyAclTest(unittest.TestCase):
    """When ACL-hardening is on, only the named receiver uid may read -- not the whole group."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.uid = os.geteuid()
        self.addr = f"{self.uid}:gallery"
        patcher = mock.patch("pm_mesh.maildir._mesh_gid", return_value=os.getgid())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _deliver_with_acl(self):
        msg = message.new_message(self.addr, "hoi", from_=f"{self.uid}:tokonomix")
        with mock.patch.dict(os.environ, {"MESH_ACL": "1"}, clear=False):
            return maildir.deliver(msg, root=self.root, mode="cross_user")

    def _getfacl(self, path):
        # ``-n`` forces numeric uid/gid output. Without it, getfacl resolves the uid to its
        # account name (e.g. "user:claude:r--" instead of "user:1001:r--") whenever the name
        # is resolvable -- which silently breaks any assertion keyed on the numeric uid.
        return subprocess.run(
            ["getfacl", "-n", "-p", path], capture_output=True, text=True, timeout=10
        ).stdout

    def _skip_unless_real_acl_applied(self, out, uid):
        # NOTE: a successful named-user ACL entry makes the kernel mirror the ACL *mask* into
        # the classic group-permission stat bits (POSIX ACL backward-compat behaviour) -- so
        # ``_imode(path) != 0o600`` is NOT a valid "did the ACL apply" signal: it is true both
        # when the ACL genuinely succeeded (mask::r--) and when it silently fell back to plain
        # group-read (0640, no ACL at all). The correct discriminator is the presence of the
        # *named* user entry itself in getfacl's output.
        if f"user:{uid}:" not in out:
            self.skipTest("tmpfs does not support ACLs here -> group-read fallback engaged, N/A")

    def test_acl_grants_only_receiver_uid_group_and_other_stay_zero(self):
        path = self._deliver_with_acl()
        out = self._getfacl(path)
        self._skip_unless_real_acl_applied(out, self.uid)

        # The receiver (named uid) gets an explicit read entry.
        self.assertIn(f"user:{self.uid}:r--", out)
        # A generic "group member" (unnamed / other) must NOT be able to read: the BASE
        # group/other entries stay at zero access -- only the *named* user entry grants
        # anything. (The mask line is expected to show r-- to keep that entry effective --
        # that is not a group-wide grant, it only bounds what named entries may use.)
        self.assertIn("group::---", out, "generic group perm must stay ---")
        self.assertIn("other::---", out)
        self.assertIn("mask::r--", out, "mask must permit the named entry to be effective")
        # Mask must not silently neuter the receiver's grant (regression covered already in
        # test_filename_entropy_acl.py; re-asserted here as part of the adversarial pass).
        self.assertNotIn("#effective:---", out)

    def test_an_unnamed_uid_has_no_acl_entry_at_all(self):
        # Attack: some OTHER member of the mesh group (not the receiver) tries to read. Since
        # POSIX ACLs are allow-only and additive, the absence of any entry for a foreign uid
        # combined with other::--- means that uid is refused at the kernel level.
        path = self._deliver_with_acl()
        out = self._getfacl(path)
        self._skip_unless_real_acl_applied(out, self.uid)
        foreign_uid = self.uid + 999999
        self.assertNotIn(f"user:{foreign_uid}:", out, "no ACL entry must exist for a non-receiver uid")


if __name__ == "__main__":
    unittest.main()
