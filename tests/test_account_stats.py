"""The GeeLark dashboard reads this store for follower/post counts. Every
test here is really asking: does the store survive being written from a
live posting flow -- concurrent-ish appends, a torn line, an unreadable
probe -- without losing or corrupting anyone else's data.
"""

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.automation.account_stats import AccountStatsStore, AccountStatsRecord


class Count:
    """Stand-in for reel_verify.Count -- the store only reads .value/.exact."""
    def __init__(self, value, exact=True):
        self.value = value
        self.exact = exact


class StoreTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AccountStatsStore(self.root / "stats.jsonl")


class RecordAndLoadTest(StoreTestBase):
    def test_a_fresh_store_loads_empty(self):
        self.assertEqual(self.store.load(), {})

    def test_a_recorded_snapshot_can_be_loaded_back(self):
        self.store.record("ph1", handle="alina.sommer74",
                          followers=Count(1234, exact=False), posts=Count(87))
        loaded = self.store.load()
        self.assertIn("ph1", loaded)
        rec = loaded["ph1"]
        self.assertEqual(rec.handle, "alina.sommer74")
        self.assertEqual(rec.followers, 1234)
        self.assertFalse(rec.followers_exact)
        self.assertEqual(rec.posts, 87)
        self.assertTrue(rec.posts_exact)

    def test_a_later_record_wins_for_the_same_phone(self):
        self.store.record("ph1", handle="old.handle", followers=Count(100), posts=Count(5))
        self.store.record("ph1", handle="new.handle", followers=Count(150), posts=Count(6))
        loaded = self.store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded["ph1"].handle, "new.handle")
        self.assertEqual(loaded["ph1"].followers, 150)

    def test_different_phones_stay_separate(self):
        self.store.record("ph1", handle="a", followers=Count(1), posts=Count(1))
        self.store.record("ph2", handle="b", followers=Count(2), posts=Count(2))
        loaded = self.store.load()
        self.assertEqual(set(loaded), {"ph1", "ph2"})

    def test_an_unreadable_probe_is_recorded_as_minus_one_not_zero(self):
        """None must never be silently folded into a real value -- a
        dashboard reading 0 followers would look like real data, not a
        failed read."""
        self.store.record("ph1", handle="x", followers=None, posts=None)
        rec = self.store.load()["ph1"]
        self.assertEqual(rec.followers, -1)
        self.assertEqual(rec.posts, -1)

    def test_no_phone_id_is_a_silent_no_op(self):
        result = self.store.record("", handle="x", followers=Count(1), posts=Count(1))
        self.assertIsNone(result)
        self.assertEqual(self.store.load(), {})


class CorruptionResilienceTest(StoreTestBase):
    def test_a_torn_line_does_not_take_down_the_whole_store(self):
        self.store.record("ph1", handle="good", followers=Count(10), posts=Count(1))
        with self.store.path.open("a", encoding="utf-8") as handle:
            handle.write('{"phone_id": "ph2", "at": 1.0, "not json\n')
        self.store.record("ph3", handle="also good", followers=Count(20), posts=Count(2))
        loaded = self.store.load()
        self.assertEqual(set(loaded), {"ph1", "ph3"})

    def test_a_blank_line_is_skipped(self):
        self.store.record("ph1", handle="x", followers=Count(1), posts=Count(1))
        with self.store.path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        loaded = self.store.load()
        self.assertEqual(list(loaded), ["ph1"])


class AppendFormatTest(StoreTestBase):
    def test_appends_are_one_json_object_per_line(self):
        self.store.record("ph1", handle="a", followers=Count(1), posts=Count(1))
        self.store.record("ph2", handle="b", followers=Count(2), posts=Count(2))
        lines = self.store.path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # must not raise
