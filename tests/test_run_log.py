"""The run log is what lets a full day's Active_Posting run be analyzed
afterwards (timing per phone, attempts needed, final outcome) instead of
grepping hours of journal text. Explicit request 2026-08-31: "ich würde
gerne alles speichern, dass wir das später auswerten können".
"""

import tempfile
import time
from pathlib import Path
from unittest import TestCase

from adb_bot.automation.run_log import RunLogStore


class RecordTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunLogStore(Path(self.tmp.name) / "log.jsonl")

    def test_records_the_basics(self):
        started = time.time() - 5
        out = {"id": "ph1", "name": "Test 1", "cycle": "active_posting",
              "attempt": 1, "result": "posted", "posts": []}
        self.store.record(out, started_at=started)
        [rec] = self.store.load()
        self.assertEqual(rec.phone_id, "ph1")
        self.assertEqual(rec.name, "Test 1")
        self.assertEqual(rec.attempt, 1)
        self.assertEqual(rec.result, "posted")
        self.assertGreaterEqual(rec.duration_seconds, 5)

    def test_counts_post_outcomes_from_the_posts_list(self):
        out = {"id": "ph1", "name": "Test 1", "attempt": 1, "result": "post_failed",
              "posts": [{"result": "posted"}, {"result": "post_failed"},
                       {"result": "post_uncertain"}]}
        self.store.record(out, started_at=time.time())
        [rec] = self.store.load()
        self.assertEqual(rec.posts_confirmed, 1)
        self.assertEqual(rec.posts_failed, 1)
        self.assertEqual(rec.posts_uncertain, 1)

    def test_no_content_pass_records_zero_posts_not_missing_fields(self):
        out = {"id": "ph1", "name": "Test 1", "attempt": 1, "result": "no_content", "posts": []}
        self.store.record(out, started_at=time.time())
        [rec] = self.store.load()
        self.assertEqual(rec.posts_confirmed, 0)
        self.assertEqual(rec.posts_failed, 0)
        self.assertEqual(rec.posts_uncertain, 0)

    def test_multiple_attempts_for_the_same_phone_all_survive(self):
        """Not folded to "latest" like post_ledger -- the whole retry
        history for a phone is exactly what's being analyzed."""
        for attempt in (1, 2, 3):
            self.store.record(
                {"id": "ph1", "name": "Test 1", "attempt": attempt,
                 "result": "could_not_reach_over_adb", "posts": []},
                started_at=time.time())
        records = self.store.load()
        self.assertEqual(len(records), 3)
        self.assertEqual([r.attempt for r in records], [1, 2, 3])

    def test_no_phone_id_is_a_silent_no_op(self):
        result = self.store.record({"name": "x", "attempt": 1, "result": "error"},
                                   started_at=time.time())
        self.assertIsNone(result)
        self.assertEqual(self.store.load(), [])

    def test_a_torn_line_does_not_take_down_the_whole_log(self):
        self.store.record({"id": "ph1", "name": "a", "attempt": 1, "result": "posted"},
                          started_at=time.time())
        with self.store.path.open("a", encoding="utf-8") as handle:
            handle.write('{"broken\n')
        self.store.record({"id": "ph2", "name": "b", "attempt": 1, "result": "posted"},
                          started_at=time.time())
        records = self.store.load()
        self.assertEqual({r.phone_id for r in records}, {"ph1", "ph2"})
