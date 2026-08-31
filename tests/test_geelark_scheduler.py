"""Time-windowed queue: Warmup at night (23:00-06:00 Berlin, wraps past
midnight), Active_Posting by day. Confirmed schedule 2026-08-29.
"""

import os
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
    """Added 2026-08-31, extended same day: a duration until whichever comes
    first -- the next 23:00 Berlin, or the day-posting timer's own next fire
    -- minus the safety buffer. Not an absolute deadline -- see
    active_posting_budget_seconds's own docstring for why that split matters
    for testability. The fire-time half exists because systemd won't start a
    second instance of an already-active oneshot service: a pass still
    running at the next fire previously absorbed that slot silently, which
    meant it never picked up whatever code changed since it started.

    Fire times went from 3x/day (07/12:30/18:30) to hourly (07:00-22:00)
    later the same day -- explicit instruction: a run finishing after its
    next fixed slot had already passed left the fleet idle for hours with
    nobody noticing. Hourly keeps that gap to under an hour without anyone
    having to restart it by hand."""

    def test_a_pass_yields_before_the_next_fire_time_not_just_at_23_00(self):
        """Starting at noon, the 13:00 fire is the nearer boundary --
        the old behavior (ignoring fire times) would have given ~11h."""
        budget = scheduler.active_posting_budget_seconds(_at(12, 0))
        self.assertAlmostEqual(budget, 45 * 60, delta=1)

    def test_inside_the_safety_buffer_before_23_is_negative(self):
        """22:50 is only 10 min before 23:00, less than the 15-min buffer,
        and past every fire time today (the last is 22:00) -- a pass
        starting this close to the window must not claim new work."""
        budget = scheduler.active_posting_budget_seconds(_at(22, 50))
        self.assertLess(budget, 0)

    def test_just_after_midnight_counts_to_the_first_fire_not_23_00(self):
        """Not reachable via run_scheduled_pass (00:30 resolves to Warmup),
        but the function itself must pick 07:00 -- the nearer boundary --
        over the far-off 23:00 that same calendar day."""
        budget = scheduler.active_posting_budget_seconds(_at(0, 30))
        self.assertAlmostEqual(budget, 6 * 3600 + 15 * 60, delta=1)

    def test_after_the_last_daily_fire_only_the_night_boundary_applies(self):
        """22:30 is after 22:00, the last of today's hourly fire times --
        nothing left to yield early for until the night window itself."""
        budget = scheduler.active_posting_budget_seconds(_at(22, 30))
        self.assertAlmostEqual(budget, 15 * 60, delta=1)

    def test_starting_exactly_at_a_fire_time_is_not_the_next_one(self):
        """15:00 itself is not > 15:00 -- the next boundary is 16:00, not an
        instant, zero-length budget."""
        budget = scheduler.active_posting_budget_seconds(_at(15, 0))
        self.assertAlmostEqual(budget, 45 * 60, delta=1)


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
    """Confirmed order, updated 2026-08-31: in-review recheck, then human
    verification -- every profile tagged by then, including ones the recheck
    or the day's Active_Posting just added -- then the regular window pass.
    One service, guaranteed order, not independently scheduled timers that
    might race, and human verification kept to this one nightly slot rather
    than its own timer since it spends real money unattended."""

    def test_recheck_then_human_verification_then_warmup(self):
        order = []
        with patch.object(scheduler, "run_in_review_recheck_pass",
                         side_effect=lambda *a, **k: order.append("recheck") or []) as recheck, \
             patch.object(scheduler, "run_human_verification_pass",
                         side_effect=lambda *a, **k: order.append("human_verification") or []) as human, \
             patch.object(scheduler, "run_scheduled_pass",
                         side_effect=lambda *a, **k: order.append("warmup") or []) as warmup:
            result = scheduler.run_night_sequence(adb_client=object())
        self.assertEqual(order, ["recheck", "human_verification", "warmup"])
        recheck.assert_called_once()
        human.assert_called_once()
        warmup.assert_called_once()
        self.assertIn("in_review_recheck", result)
        self.assertIn("human_verification", result)
        self.assertIn("warmup", result)


class DayPassTest(unittest.TestCase):
    """Added 2026-08-31: Active_Posting's pass, then human verification with
    whatever proxy capacity it leaves free, instead of that capacity sitting
    idle until the once-nightly slot. Only during the daytime window --
    outside it, run_night_sequence already owns human verification, and a
    second run here would double-spend the real money it costs. Falls back
    to Warmup instead when there's no SMS balance for human verification,
    so idle capacity always has *something* to do."""

    def setUp(self):
        # Without this, every test here calls the REAL run_geelark_recheck,
        # which reaches for real Geelark credentials/network -- harmless
        # when they're absent (it bails out and returns []), but a live
        # launch risk on a box where they happen to be set. Same lesson as
        # the run_log pollution bug found the same day.
        patcher = patch("adb_bot.automation.geelark_recheck.run_geelark_recheck",
                        return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        # Same reasoning as the recheck patch above, for the account-check
        # sweep added the same day.
        account_check_patcher = patch(
            "adb_bot.automation.geelark_account_check.run_geelark_account_check",
            return_value=[])
        account_check_patcher.start()
        self.addCleanup(account_check_patcher.stop)

    def test_daytime_runs_human_verification_after_the_scheduled_pass(self):
        order = []
        with patch.object(scheduler, "run_scheduled_pass",
                         side_effect=lambda *a, **k: order.append("scheduled") or []) as scheduled, \
             patch.object(scheduler, "_best_sms_balance", return_value=5.0), \
             patch.object(scheduler, "run_human_verification_pass",
                         side_effect=lambda *a, **k: order.append("human_verification") or []) as human:
            result = scheduler.run_day_pass(adb_client=object(), now=_at(12, 0))
        self.assertEqual(order, ["scheduled", "human_verification"])
        scheduled.assert_called_once()
        human.assert_called_once()
        self.assertIn("scheduled", result)
        self.assertIn("human_verification", result)
        self.assertEqual(result["warmup"], [])

    def test_the_env_toggle_skips_human_verification_without_falling_back_to_warmup(self):
        """Added 2026-08-31: needs to be pausable without a code change, and
        without spending the freed-up capacity on Warmup either -- unlike the
        no-SMS-balance fallback, this is "focus on posting", not "keep busy"."""
        with patch.dict(os.environ, {"ADBBOT_GEELARK_HUMAN_VERIFICATION_ENABLED": "0"}), \
             patch.object(scheduler, "run_scheduled_pass", return_value=[]) as scheduled, \
             patch.object(scheduler, "_best_sms_balance") as balance, \
             patch.object(scheduler, "run_human_verification_pass") as human:
            result = scheduler.run_day_pass(adb_client=object(), now=_at(12, 0))
        human.assert_not_called()
        balance.assert_not_called()
        self.assertEqual(result["human_verification"], [])
        self.assertEqual(result["warmup"], [])
        # The posting pass itself is unaffected by the toggle.
        scheduled.assert_called_once()

    def test_nighttime_skips_both_fallbacks_to_avoid_double_spending(self):
        """If this ever runs during the Warmup window, run_night_sequence
        already covers human verification for that slot -- and the regular
        Warmup pass already happened as the scheduled pass itself."""
        with patch.object(scheduler, "run_scheduled_pass", return_value=[]), \
             patch.object(scheduler, "_best_sms_balance") as balance, \
             patch.object(scheduler, "run_human_verification_pass") as human:
            result = scheduler.run_day_pass(adb_client=object(), now=_at(23, 30))
        human.assert_not_called()
        balance.assert_not_called()
        self.assertEqual(result["human_verification"], [])
        self.assertEqual(result["warmup"], [])

    def test_nothing_tagged_costs_nothing_extra(self):
        with patch.object(scheduler, "run_scheduled_pass", return_value=[]), \
             patch.object(scheduler, "_best_sms_balance", return_value=5.0), \
             patch.object(scheduler, "run_human_verification_pass", return_value=[]) as human:
            result = scheduler.run_day_pass(adb_client=object(), now=_at(9, 0))
        human.assert_called_once()
        self.assertEqual(result["human_verification"], [])

    def test_low_balance_falls_back_to_warmup_instead_of_sitting_idle(self):
        from adb_bot.automation.verification_runner import MIN_BALANCE_TO_START
        calls = []

        def fake_scheduled(*a, **k):
            calls.append(k.get("tag"))
            return []

        with patch.object(scheduler, "run_scheduled_pass", side_effect=fake_scheduled), \
             patch.object(scheduler, "_best_sms_balance", return_value=MIN_BALANCE_TO_START - 0.5), \
             patch.object(scheduler, "run_human_verification_pass") as human:
            result = scheduler.run_day_pass(adb_client=object(), now=_at(12, 0))
        human.assert_not_called()
        # First call is the normal Active_Posting pass (tag=None -> resolved
        # by the clock), second is the explicit Warmup fallback.
        self.assertEqual(calls, [None, scheduler.lifecycle.TAG_WARMUP])
        self.assertEqual(result["human_verification"], [])

    def test_an_unreadable_balance_check_tries_human_verification_anyway(self):
        """Failing to preflight the balance must never itself be the reason
        real work doesn't happen -- only a genuinely low read balance is."""
        with patch.object(scheduler, "run_scheduled_pass", return_value=[]), \
             patch.object(scheduler, "_best_sms_balance", return_value=None), \
             patch.object(scheduler, "run_human_verification_pass", return_value=[]) as human:
            scheduler.run_day_pass(adb_client=object(), now=_at(12, 0))
        human.assert_called_once()

    def test_recheck_runs_every_time_and_its_results_come_back(self):
        """Explicit finding 2026-08-31: Geelark shares had no deferred-recheck
        path at all (recheck_runner.py is Airtable-bound), so this must run
        unconditionally -- not gated on the Active_Posting/Warmup tag the
        way human verification is."""
        with patch.object(scheduler, "run_scheduled_pass", return_value=[]), \
             patch.object(scheduler, "_best_sms_balance", return_value=5.0), \
             patch.object(scheduler, "run_human_verification_pass", return_value=[]), \
             patch("adb_bot.automation.geelark_recheck.run_geelark_recheck",
                  return_value=[{"phone_id": "ph1", "outcome": "posted"}]) as recheck:
            result = scheduler.run_day_pass(adb_client=object(), now=_at(12, 0))
        recheck.assert_called_once()
        self.assertEqual(result["recheck"], [{"phone_id": "ph1", "outcome": "posted"}])

    def test_account_check_runs_every_time_and_its_results_come_back(self):
        """Requested 2026-08-31: sweep untouched Active_Posting phones for
        human-verification/logged-out/banned once a day -- same
        unconditional placement as recheck, for the same reason (bounded
        and fast, must not wait on the Active_Posting/Warmup tag)."""
        with patch.object(scheduler, "run_scheduled_pass", return_value=[]), \
             patch.object(scheduler, "_best_sms_balance", return_value=5.0), \
             patch.object(scheduler, "run_human_verification_pass", return_value=[]), \
             patch("adb_bot.automation.geelark_recheck.run_geelark_recheck", return_value=[]), \
             patch("adb_bot.automation.geelark_account_check.run_geelark_account_check",
                  return_value=[{"phone_id": "ph1", "name": "Lea 1", "result": "healthy"}]
                  ) as account_check:
            result = scheduler.run_day_pass(adb_client=object(), now=_at(12, 0))
        account_check.assert_called_once()
        self.assertEqual(result["account_check"],
                         [{"phone_id": "ph1", "name": "Lea 1", "result": "healthy"}])


class BestSmsBalanceTest(unittest.TestCase):
    """The preflight check itself -- see verification_runner's own version,
    which this mirrors."""

    def test_takes_the_best_of_several_providers_not_the_first(self):
        class Provider:
            def __init__(self, name, value):
                self.name = name
                self._value = value
            def balance(self):
                return self._value

        class FakeRouter:
            providers = [Provider("5sim", 0.10), Provider("smspool", 4.50)]

        with patch("adb_bot.clients.sms.router.build_router", return_value=FakeRouter()):
            self.assertEqual(scheduler._best_sms_balance(), 4.50)

    def test_a_provider_that_cannot_report_its_balance_is_skipped_not_fatal(self):
        class BadProvider:
            name = "5sim"
            def balance(self):
                raise RuntimeError("network error")

        class GoodProvider:
            name = "smspool"
            def balance(self):
                return 2.0

        class FakeRouter:
            providers = [BadProvider(), GoodProvider()]

        with patch("adb_bot.clients.sms.router.build_router", return_value=FakeRouter()):
            self.assertEqual(scheduler._best_sms_balance(), 2.0)

    def test_the_router_itself_being_unavailable_returns_none_not_zero(self):
        with patch("adb_bot.clients.sms.router.build_router",
                  side_effect=RuntimeError("no credentials")):
            self.assertIsNone(scheduler._best_sms_balance())


if __name__ == "__main__":
    unittest.main()
