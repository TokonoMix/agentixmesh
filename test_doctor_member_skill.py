"""``mesh doctor`` must check the OTHER members' skill install, not only its own.

Found the hard way on 2026-07-22: all three colleague accounts had the delivery hook wired — they
received mesh frames — but **no pm-mesh skill installed at all**, not even an old version. So a
frame landing in their context met an agent with no operating knowledge: it did not know the frame
is inert DATA, did not know `mesh-send` syntax, did not know `mesh-whoami` existed. That had been
true for weeks and nothing reported it, because the existing ``skill-symlink`` check only ever
looked at ``$HOME`` — the one home that was always correct.

A self-check that only inspects the machine it runs on cannot find a fleet gap. This adds a
per-member check driven by the address book (the one enumerable list of who is on the mesh).

The honesty requirement: a normal user cannot traverse another user's home, so the check must
report **unknown** in that case, never "ok". A check that silently degrades to ok is worse than
no check — it is the same silence with a green tick on top.
"""

import os
import tempfile
import unittest
from unittest import mock

from pm_mesh import doctor


class MemberSkillCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _home_for(self, name, *, with_skill):
        home = os.path.join(self.tmp.name, name)
        skills = os.path.join(home, ".claude", "skills")
        os.makedirs(skills, exist_ok=True)
        if with_skill:
            target = os.path.join(self.tmp.name, "skillsrc")
            os.makedirs(target, exist_ok=True)
            os.symlink(target, os.path.join(skills, "pm-mesh"))
        return home

    def _run(self, members, homes):
        with mock.patch("pm_mesh.doctor._mesh_member_uids", return_value=members), \
             mock.patch("pm_mesh.doctor._home_of_uid", side_effect=lambda uid: homes.get(uid)):
            return doctor._member_skill_checks(self_uid=1000)

    def test_1_a_member_without_the_skill_is_reported_not_ok(self):
        homes = {1100: self._home_for("a", with_skill=False)}
        checks = self._run([1100], homes)
        self.assertEqual(len(checks), 1)
        self.assertIs(checks[0]["ok"], False)
        self.assertIn("1100", checks[0]["detail"])

    def test_2_a_member_with_the_skill_is_ok(self):
        homes = {1100: self._home_for("b", with_skill=True)}
        checks = self._run([1100], homes)
        self.assertIs(checks[0]["ok"], True)

    def test_3_an_unreadable_home_is_unknown_never_ok(self):
        """The honesty requirement: no permission must not read as a green tick."""
        checks = self._run([1100], {1100: None})
        self.assertIsNone(checks[0]["ok"])
        self.assertIn("unknown", checks[0]["detail"].lower())

    def test_3b_a_private_home_is_unknown_not_a_confident_failure(self):
        """The false alarm that showed up live: os.path.lexists returns False on a 0700 home,
        so an account that is perfectly fine was reported as having NO skill. Homes are private
        by default, so this is the common case — and a check that cries wolf gets ignored."""
        home = self._home_for("private", with_skill=True)
        os.chmod(home, 0o000)
        self.addCleanup(os.chmod, home, 0o700)
        real_stat = os.stat

        def denied(path, *a, **kw):
            if str(path).startswith(home):
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *a, **kw)

        with mock.patch("os.stat", side_effect=denied):
            checks = self._run([1100], {1100: home})
        self.assertIsNone(checks[0]["ok"], "a private home must be unknown, not a confident FAIL")
        self.assertIn("permission", checks[0]["detail"].lower())

    def test_4_self_is_skipped_the_existing_check_covers_it(self):
        checks = self._run([1000], {})
        self.assertEqual(checks, [])

    def test_5_the_check_never_raises(self):
        with mock.patch("pm_mesh.doctor._mesh_member_uids", side_effect=OSError("boom")):
            self.assertIsInstance(doctor._member_skill_checks(self_uid=1000), list)

    def test_6_diagnose_includes_the_member_checks(self):
        """Wired in, not merely available — the gap was that nobody ran it."""
        with mock.patch("pm_mesh.doctor._member_skill_checks", return_value=[
                doctor._check("member-skill", False, "uid 1100: missing")]):
            names = [c["name"] for c in doctor.diagnose(home=self.tmp.name)]
        self.assertIn("member-skill", names)


if __name__ == "__main__":
    unittest.main()
