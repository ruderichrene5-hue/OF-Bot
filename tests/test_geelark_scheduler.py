"""Time-windowed queue: Warmup at night (23:00-06:00 Berlin, wraps past
midnight), Active_Posting by day. Confirmed schedule 2026-08-29.
"""

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from adb_bot.automation import geelark_lifecycle as lifecycle
from adb_bot.automation import geelark_scheduler as scheduler

BERLIN = ZoneInfo("Europe/Berlin")


def _at(hour, minute=0):
    return datetime(2026, 8, 29, hour, minute, tzinfo=BERLIN)


class WindowTest(unittest.TestCase):
    def test_just_after_23_is_warmup(self):
        self.assertTrue(scheduler.in_warmup_window(_at(23, 1)))

    def test_just_before_6_is_warmup(self):
        self.assertTrue(scheduler.in_warmup_window(_at(5, 59)))

    def test_exactly_23_is_warmup_inclusive(self):
        self.assertTrue(scheduler.in_warmup_window(_at(23, 0)))

    def test_exactly_6_is_active_posting_exclusive(self):
        self.assertFalse(scheduler.in_warmup_window(_at(6, 0)))

    def test_midday_is_active_posting(self):
        self.assertFalse(scheduler.in_warmup_window(_at(14, 30)))

    def test_midnight_itself_is_warmup(self):
        """The wrap-past-midnight case -- an OR check, not a naive between."""
        self.assertTrue(scheduler.in_warmup_window(_at(0, 30)))

    def test_active_tag_matches_the_window(self):
        self.assertEqual(scheduler.active_tag_for_now(_at(1, 0)), lifecycle.TAG_WARMUP)
        self.assertEqual(scheduler.active_tag_for_now(_at(12, 0)),
                         lifecycle.TAG_ACTIVE_POSTING)


class RunQueueTest(unittest.TestCase):
    def test_processes_every_item_exactly_once(self):
        worklist = [{"id": str(i), "serialName": f"P{i}"} for i in range(10)]
        seen = []
        lock_free_seen = []

        def work_fn(phone_id, name):
            seen.append(phone_id)
            return {"id": phone_id, "result": "ok"}

        results = scheduler.run_queue(worklist, work_fn, concurrency=4)
        self.assertEqual(sorted(seen), sorted(str(i) for i in range(10)))
        self.assertEqual(len(results), 10)

    def test_never_runs_more_than_the_concurrency_cap_at_once(self):
        import threading
        import time

        worklist = [{"id": str(i), "serialName": f"P{i}"} for i in range(8)]
        concurrent = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def work_fn(phone_id, name):
            with lock:
                concurrent["now"] += 1
                concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
            time.sleep(0.05)
            with lock:
                concurrent["now"] -= 1
            return {"id": phone_id}

        scheduler.run_queue(worklist, work_fn, concurrency=4)
        self.assertLessEqual(concurrent["peak"], 4)

    def test_empty_worklist_returns_empty_without_hanging(self):
        self.assertEqual(scheduler.run_queue([], lambda *_: {}), [])


class RunScheduledPassTest(unittest.TestCase):
    def test_night_window_picks_warmup_tag_and_cycle(self):
        with patch.object(lifecycle, "phones_by_tag") as phones_by_tag, \
             patch.object(scheduler, "run_queue") as run_queue:
            phones_by_tag.return_value = [{"id": "1", "serialName": "P1"}]
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(1, 0))
        self.assertEqual(phones_by_tag.call_args.args[0], lifecycle.TAG_WARMUP)
        run_queue.assert_called_once()

    def test_day_window_picks_active_posting_tag(self):
        with patch.object(lifecycle, "phones_by_tag") as phones_by_tag, \
             patch.object(scheduler, "run_queue") as run_queue:
            phones_by_tag.return_value = []
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(12, 0))
        self.assertEqual(phones_by_tag.call_args.args[0], lifecycle.TAG_ACTIVE_POSTING)

    def test_night_window_never_calls_active_posting_cycle(self):
        """Already-Active_Posting profiles sleep during the night window --
        confirmed 2026-08-29 -- so the work function must be the warmup
        cycle, never the posting one, regardless of what's in the worklist."""
        with patch.object(lifecycle, "phones_by_tag", return_value=[{"id": "1", "serialName": "P1"}]), \
             patch.object(lifecycle, "run_warmup_cycle") as warmup_cycle, \
             patch.object(lifecycle, "run_active_posting_cycle") as posting_cycle:
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(23, 30))
        warmup_cycle.assert_called_once()
        posting_cycle.assert_not_called()

    def test_day_window_never_calls_warmup_cycle(self):
        with patch.object(lifecycle, "phones_by_tag", return_value=[{"id": "1", "serialName": "P1"}]), \
             patch.object(lifecycle, "run_warmup_cycle") as warmup_cycle, \
             patch.object(lifecycle, "run_active_posting_cycle") as posting_cycle:
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(10, 0))
        posting_cycle.assert_called_once()
        warmup_cycle.assert_not_called()


class InReviewRecheckPassTest(unittest.TestCase):
    def test_lists_only_in_review_tagged_profiles_and_runs_the_recheck_cycle(self):
        with patch.object(lifecycle, "phones_by_tag") as phones_by_tag, \
             patch.object(lifecycle, "run_in_review_recheck_cycle") as recheck_cycle:
            phones_by_tag.return_value = [{"id": "1", "serialName": "P1"}]
            scheduler.run_in_review_recheck_pass(adb_client=object())
        self.assertEqual(phones_by_tag.call_args.args[0], "in review")
        recheck_cycle.assert_called_once()

    def test_never_calls_the_other_cycles(self):
        """This pass is only for in-review profiles -- confirming it never
        touches warmup or active-posting even if the fakes would let it."""
        with patch.object(lifecycle, "phones_by_tag", return_value=[{"id": "1", "serialName": "P1"}]), \
             patch.object(lifecycle, "run_in_review_recheck_cycle"), \
             patch.object(lifecycle, "run_warmup_cycle") as warmup_cycle, \
             patch.object(lifecycle, "run_active_posting_cycle") as posting_cycle:
            scheduler.run_in_review_recheck_pass(adb_client=object())
        warmup_cycle.assert_not_called()
        posting_cycle.assert_not_called()


class NightSequenceTest(unittest.TestCase):
    """Confirmed order 2026-08-30: in-review recheck first, then the regular
    window pass -- one service, guaranteed order, not two independently
    scheduled timers that might race."""

    def test_recheck_runs_before_warmup(self):
        order = []
        with patch.object(scheduler, "run_in_review_recheck_pass",
                         side_effect=lambda *a, **k: order.append("recheck") or []) as recheck, \
             patch.object(scheduler, "run_scheduled_pass",
                         side_effect=lambda *a, **k: order.append("warmup") or []) as warmup:
            result = scheduler.run_night_sequence(adb_client=object())
        self.assertEqual(order, ["recheck", "warmup"])
        recheck.assert_called_once()
        warmup.assert_called_once()
        self.assertIn("in_review_recheck", result)
        self.assertIn("warmup", result)


if __name__ == "__main__":
    unittest.main()
