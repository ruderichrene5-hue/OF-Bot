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


class ActivePostingBudgetTest(unittest.TestCase):
    """Added 2026-08-31: a duration until the next 23:00 Berlin (minus the
    safety buffer), not an absolute deadline -- see active_posting_budget_seconds's
    own docstring for why that split matters for testability."""

    def test_midday_has_most_of_a_day_left(self):
        budget = scheduler.active_posting_budget_seconds(_at(12, 0))
        self.assertAlmostEqual(budget, 10 * 3600 + 45 * 60, delta=1)

    def test_inside_the_safety_buffer_before_23_is_negative(self):
        """22:50 is only 10 min before 23:00, less than the 15-min buffer --
        a pass starting this close to the window must not claim new work."""
        budget = scheduler.active_posting_budget_seconds(_at(22, 50))
        self.assertLess(budget, 0)

    def test_just_after_midnight_counts_to_that_same_calendar_days_23_00(self):
        """Not reachable via run_scheduled_pass (00:30 resolves to Warmup),
        but the function itself must pick the *upcoming* 23:00 (still later
        that same calendar day), not a stale or off-by-one-day one."""
        budget = scheduler.active_posting_budget_seconds(_at(0, 30))
        self.assertAlmostEqual(budget, 22 * 3600 + 15 * 60, delta=1)


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

    def test_a_negative_budget_claims_nothing(self):
        """Already past the deadline when the pass starts -- the fleet has
        badly outgrown its window. Must not crash or hang, just do nothing
        and let the caller's warning explain why."""
        worklist = [{"id": "1", "serialName": "P1"}]
        work_fn = unittest.mock.Mock()
        results = scheduler.run_queue(worklist, work_fn, concurrency=1,
                                      budget_seconds=-10)
        self.assertEqual(results, [])
        work_fn.assert_not_called()

    def test_an_in_flight_item_finishes_even_past_the_budget(self):
        """The deadline is checked only before claiming the *next* item --
        a phone already launched must always run to completion."""
        import threading
        import time

        worklist = [{"id": "1", "serialName": "P1"}, {"id": "2", "serialName": "P2"}]
        started = threading.Event()

        def work_fn(phone_id, name):
            if phone_id == "1":
                started.set()
                time.sleep(0.15)   # still "in flight" when the budget expires
            return {"id": phone_id, "result": "ok"}

        # Budget expires 0.05s in -- item "1" is already claimed and running
        # (concurrency=1, so "2" is still waiting) by the time it does.
        results = scheduler.run_queue(worklist, work_fn, concurrency=1,
                                      budget_seconds=0.05)
        self.assertTrue(started.is_set())
        self.assertEqual([r["id"] for r in results], ["1"])

    def test_logs_how_much_of_the_worklist_the_budget_did_not_reach(self):
        worklist = [{"id": str(i), "serialName": f"P{i}"} for i in range(3)]
        logger = unittest.mock.Mock()
        scheduler.run_queue(worklist, lambda *_: {}, concurrency=1,
                            budget_seconds=-1, logger=logger)
        logger.warning.assert_called_once()
        message = logger.warning.call_args.args[0] % logger.warning.call_args.args[1:]
        self.assertIn("3/3", message)


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


class ActivePostingContentTest(unittest.TestCase):
    """Confirmed 2026-08-30: Active_Posting resolves its own content per
    phone (model from the phone's Geelark group, today's date folder), not
    one media_path for the whole pass."""

    def _content(self, name, path):
        from adb_bot.automation import geelark_content
        return geelark_content.SpoofedContent(
            path=path, raw_video=geelark_content.RawVideo(model="Luisa", name=name, path=""),
            handle="1")

    def test_resolves_up_to_posts_per_launch_videos_and_posts_the_batch(self):
        """POSTS_PER_LAUNCH=2 (changed 2026-08-31): one launch posts up to 2
        clips instead of 1, so the fixed ~90s launch cost is paid once for
        two posts instead of twice."""
        from adb_bot.automation import geelark_content

        row = {"id": "1", "serialName": "P1", "group": {"name": "Luisa"}}
        first = self._content("a.mp4", "/tmp/spoofed_a.mp4")
        second = self._content("b.mp4", "/tmp/spoofed_b.mp4")
        # exclude_names is mutated in place across the two calls, so a
        # mock's call_args_list (which stores a live reference, not a
        # snapshot) would show the same final set for both calls if
        # inspected afterwards -- snapshot it as a copy at call time instead.
        seen_excludes = []

        def fake_get_post_media(model, handle, logger=None, exclude_names=None):
            seen_excludes.append(set(exclude_names or ()))
            return [first, second][len(seen_excludes) - 1]

        with patch.object(lifecycle, "phones_by_tag", return_value=[row]), \
             patch.object(geelark_content, "get_post_media",
                         side_effect=fake_get_post_media), \
             patch.object(lifecycle, "run_active_posting_cycle",
                         return_value={"result": "posted"}) as posting_cycle, \
             patch("pathlib.Path.unlink"):
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(12, 0))
        self.assertEqual(len(seen_excludes), scheduler.POSTS_PER_LAUNCH)
        self.assertEqual(seen_excludes[0], set())
        # the second call must exclude the first pick, or a model with fewer
        # videos than POSTS_PER_LAUNCH today would post the same clip twice
        # in one launch before either lands in the post_ledger.
        self.assertEqual(seen_excludes[1], {"a.mp4"})
        self.assertEqual(posting_cycle.call_args.kwargs["media_paths"],
                         ["/tmp/spoofed_a.mp4", "/tmp/spoofed_b.mp4"])

    def test_fewer_videos_than_posts_per_launch_gives_a_shorter_batch(self):
        """A model with only one video today gets a 1-post batch, not a
        repeat of that video to fill POSTS_PER_LAUNCH."""
        from adb_bot.automation import geelark_content

        row = {"id": "1", "serialName": "P1", "group": {"name": "Luisa"}}
        first = self._content("a.mp4", "/tmp/spoofed_a.mp4")
        with patch.object(lifecycle, "phones_by_tag", return_value=[row]), \
             patch.object(geelark_content, "get_post_media",
                         side_effect=[first, None]), \
             patch.object(lifecycle, "run_active_posting_cycle",
                         return_value={"result": "posted"}) as posting_cycle, \
             patch("pathlib.Path.unlink"):
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(12, 0))
        self.assertEqual(posting_cycle.call_args.kwargs["media_paths"],
                         ["/tmp/spoofed_a.mp4"])

    def test_no_content_today_calls_the_cycle_with_an_empty_batch(self):
        from adb_bot.automation import geelark_content

        row = {"id": "1", "serialName": "P1", "group": {"name": "Luisa"}}
        with patch.object(lifecycle, "phones_by_tag", return_value=[row]), \
             patch.object(geelark_content, "get_post_media", return_value=None), \
             patch.object(lifecycle, "run_active_posting_cycle",
                         return_value={"result": "no_content"}) as posting_cycle:
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(12, 0))
        self.assertEqual(posting_cycle.call_args.kwargs["media_paths"], [])

    def test_a_phone_with_no_group_never_calls_get_post_media(self):
        from adb_bot.automation import geelark_content

        row = {"id": "1", "serialName": "P1"}   # no "group" key at all
        with patch.object(lifecycle, "phones_by_tag", return_value=[row]), \
             patch.object(geelark_content, "get_post_media") as get_media, \
             patch.object(lifecycle, "run_active_posting_cycle",
                         return_value={"result": "no_content"}):
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(12, 0))
        get_media.assert_not_called()

    def test_every_spoofed_file_in_the_batch_is_deleted_after_the_cycle(self):
        from adb_bot.automation import geelark_content

        row = {"id": "1", "serialName": "P1", "group": {"name": "Luisa"}}
        first = self._content("a.mp4", "/tmp/spoofed_a.mp4")
        second = self._content("b.mp4", "/tmp/spoofed_b.mp4")
        with patch.object(lifecycle, "phones_by_tag", return_value=[row]), \
             patch.object(geelark_content, "get_post_media",
                         side_effect=[first, second]), \
             patch.object(lifecycle, "run_active_posting_cycle",
                         return_value={"result": "posted"}), \
             patch("pathlib.Path.unlink") as unlink:
            scheduler.run_scheduled_pass(adb_client=object(), now=_at(12, 0))
        self.assertEqual(unlink.call_count, 2)


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
