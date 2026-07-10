"""Tests for the replay/dedup guard: already-processed or too-old messages must not act again."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from pm_mesh import message, replay

ADDR = "1000:proj"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(dt):
    return dt.timestamp()


class SeenStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.store = replay.SeenStore(root=self.root)
        self.base = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)

    def _msg(self, ts=None, mid=None):
        m = message.new_message(to=ADDR, body="hoi")
        if ts is not None:
            m.ts_utc = _iso(ts)
        if mid is not None:
            m.id = mid
        return m

    def test_fresh_message_is_fresh(self):
        m = self._msg(ts=self.base)
        self.assertTrue(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_after_mark_seen_not_fresh(self):
        m = self._msg(ts=self.base)
        self.store.mark_seen(m.to, m.id)
        self.assertFalse(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_old_ts_not_fresh(self):
        old = self.base - timedelta(seconds=100000)  # > 1 day
        m = self._msg(ts=old)
        self.assertFalse(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_mark_seen_idempotent(self):
        m = self._msg(ts=self.base)
        self.store.mark_seen(m.to, m.id)
        self.store.mark_seen(m.to, m.id)  # no crash
        self.assertFalse(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_seen_persists_across_store_instances(self):
        m = self._msg(ts=self.base)
        self.store.mark_seen(m.to, m.id)
        other = replay.SeenStore(root=self.root)
        self.assertFalse(other.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_is_seen_is_dedup_only_no_age_gate(self):
        # is_seen keys PURELY on seen-state, WITHOUT an age floor (separate from is_fresh) — the
        # gate-release path must be able to show a deliberately-approved, old message (design §2 inv. 7).
        old = self.base - timedelta(seconds=100000)  # > 1 day
        m = self._msg(ts=old, mid="rel-1")
        self.assertFalse(self.store.is_seen(m.to, m.id))  # not yet seen
        # is_fresh DOES refuse it on age — proves is_seen is a separate, age-less check:
        self.assertFalse(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))
        self.store.mark_seen(m.to, m.id)
        self.assertTrue(self.store.is_seen(m.to, m.id))  # True after mark_seen, regardless of age

    def test_distinct_ids_independent(self):
        m1 = self._msg(ts=self.base, mid="id-aaa")
        m2 = self._msg(ts=self.base, mid="id-bbb")
        self.store.mark_seen(m1.to, m1.id)
        self.assertFalse(self.store.is_fresh(m1, max_age_s=86400, now=_epoch(self.base)))
        self.assertTrue(self.store.is_fresh(m2, max_age_s=86400, now=_epoch(self.base)))

    def test_missing_store_treated_as_empty(self):
        # No seen-state ever written → is_fresh works without a crash.
        m = self._msg(ts=self.base)
        self.assertTrue(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_boundary_exactly_max_age_not_fresh(self):
        edge = self.base - timedelta(seconds=86400)
        m = self._msg(ts=edge)
        self.assertFalse(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_dedup_keys_on_verified_address_not_attacker_to(self):
        # FIX 1 (P2): the seen-store keys on the VERIFIED receive address, not on the
        # attacker-supplied `to`. Two messages with the same id but DIFFERENT `to`,
        # both delivered to the SAME receive address → the second must be deduplicated.
        m1 = self._msg(ts=self.base, mid="id-dup")
        m1.to = "1000:proj"
        m2 = self._msg(ts=self.base, mid="id-dup")
        m2.to = "9999:elders"  # attacker varies `to` to try to bypass dedup
        recv = "1000:proj"  # the maildir where both are physically found
        self.assertTrue(self.store.is_fresh(m1, address=recv, now=_epoch(self.base)))
        self.store.mark_seen(recv, m1.id)
        # same id, different `to`, but the same receive address → NOT fresh (deduplicated)
        self.assertFalse(self.store.is_fresh(m2, address=recv, now=_epoch(self.base)))

    def test_dedup_normal_repeated_id_still_dedups(self):
        # Regression: a normal repeated id (msg.to == receive address) still dedups.
        m = self._msg(ts=self.base, mid="id-normal")
        self.store.mark_seen(m.to, m.id)
        self.assertFalse(self.store.is_fresh(m, address=m.to, now=_epoch(self.base)))

    def test_far_future_ts_not_fresh(self):
        # FIX 2 (P3): a ts_utc dated far in the future (negative age) must not be able to
        # bypass the age floor — > MAX_FUTURE_SKEW_S in the future → NOT fresh.
        future = self.base + timedelta(hours=1)
        m = self._msg(ts=future)
        self.assertFalse(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))

    def test_small_future_skew_still_fresh(self):
        # A small legitimate clock skew (a few seconds ahead) remains tolerated → fresh.
        skew = self.base + timedelta(seconds=5)
        m = self._msg(ts=skew)
        self.assertTrue(self.store.is_fresh(m, max_age_s=86400, now=_epoch(self.base)))


if __name__ == "__main__":
    unittest.main()
