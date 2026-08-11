"""The one definition of "warm-up finished", pinned case by case.

Four readers of the Run Log used to answer this question four ways, and on
2026-08-11 that left 41 phones counted `finished` by the dashboard while the
worklist that asks a person for their bio and picture showed nobody. These tests
exist so the rule cannot drift again -- so they pin the two things that broke:

- the plan's LAST day is a scroll-only day, mapping to `instagram_scroll`, and a
  fixture in the older tests built plan rows out of raw Airtable field names
  ("Day", "Scroll") instead of the normalised keys `warmup_plan_by_day` actually
  returns. Every row here is built through `_row`, in the normalised shape, so a
  day-4 case cannot silently degrade into an empty row again;
- a day is credited when its work is DONE, not when the calendar passes it. The
  catch-up case below (a day-4 scroll finally landing on calendar day 6) is the
  one the whole module turns on.
"""

import os
import time
import unittest
from datetime import date
from unittest import TestCase

from adb_bot.automation import lifecycle, warmup_completion as wc
from adb_bot.clients import airtable as at

FLOW_WARMUP = lifecycle.FLOW_WARMUP
FLOW_SCROLL = lifecycle.FLOW_SCROLL_ONLY


def _row(scroll=False, follow=False, feed_posts=0, picture=False, bio=False,
         reel=False, notes=None):
    """A Warmup Plan row in the shape `AirtableClient.warmup_plan_by_day` returns.

    Normalised keys, not Airtable's field names: the client translates once on
    read, and a fixture that skips that translation tests a row the code will
    never see (it reads as an empty day and maps to no flows at all).
    """
    return {"scroll": scroll, "follow": follow, "feed_posts": feed_posts,
            "picture": picture, "bio": bio, "reel": reel, "notes": notes}


def _live_plan():
    """The client's plan as it stands in the live base on 2026-08-11.

    Days 1-3 scroll and follow; day 4 scrolls and asks for a reel. The reel is
    stripped by `warmup_targets` (reels belong to the Posting Queue), which is
    why day 4's only run -- and the only thing that can finish the warm-up -- is
    `instagram_scroll`.
    """
    return {1: _row(scroll=True, follow=True),
            2: _row(scroll=True, follow=True),
            3: _row(scroll=True, follow=True),
            4: _row(scroll=True, follow=False, reel=True)}


def _entry(at_value, flow=FLOW_WARMUP, result=at.RESULT_DONE, notes=""):
    """One `history_by_key` entry, as `day_done` consumes them."""
    return {"at": at_value, "result": result, "notes": notes, "flow": flow}


def _log_row(name, flow=FLOW_WARMUP, result=at.RESULT_DONE,
             run_at="2026-08-05T12:00:00.000Z", notes=""):
    """A Run Log record as Airtable hands it back."""
    return {"id": "rec" + name, "fields": {
        at.F_RUN_NAME: f"{name} / {flow} / 05 Aug 12:00",
        at.F_RUN_FLOW: flow,
        at.F_RUN_RESULT: result,
        at.F_RUN_AT: run_at,
        at.F_RUN_NOTES: notes,
    }}


class FlowSetsTest(TestCase):
    """Which flows are warm-up history, and which of them gate."""

    def test_picture_and_bio_are_read_but_do_not_gate(self):
        # The base holds 52 `update_profile_picture` rows and not one is Done --
        # every one skipped for want of a photo a person has to supply. Gating
        # on them would mean nobody ever finishes, and the hand-off tab that
        # exists to ASK for the photo would sit empty waiting for it.
        for flow in (lifecycle.FLOW_UPDATE_PICTURE, lifecycle.FLOW_UPDATE_BIO):
            self.assertIn(flow, wc.WARMUP_RUN_FLOWS)
            self.assertNotIn(flow, wc.WARMUP_ACTIVITY_FLOWS)

    def test_both_activity_flows_are_read_and_gate(self):
        for flow in (FLOW_WARMUP, FLOW_SCROLL):
            self.assertIn(flow, wc.WARMUP_RUN_FLOWS)
            self.assertIn(flow, wc.WARMUP_ACTIVITY_FLOWS)

    def test_the_reel_flow_is_neither(self):
        # `warmup_targets` strips it, so requiring it would gate on a run the
        # warm-up never makes.
        self.assertNotIn(lifecycle.FLOW_REEL, wc.WARMUP_RUN_FLOWS)
        self.assertNotIn(lifecycle.FLOW_REEL, wc.WARMUP_ACTIVITY_FLOWS)


class RequiredFlowsTest(TestCase):

    def test_the_live_plan_ends_on_a_scroll_only_day(self):
        # The bug in one assertion: day 4 is NOT warm_up_process, and every
        # reader that filtered the Run Log to warm_up_process was blind to it.
        self.assertEqual(wc.required_flows_by_day(_live_plan()), {
            1: {FLOW_WARMUP}, 2: {FLOW_WARMUP}, 3: {FLOW_WARMUP}, 4: {FLOW_SCROLL},
        })
        self.assertEqual(wc.gating_days(_live_plan()), [1, 2, 3, 4])
        self.assertEqual(wc.finish_day(_live_plan()), 4)

    def test_a_day_asking_for_a_picture_as_well_still_requires_only_the_warmup(self):
        plan = {1: _row(scroll=True, follow=True),
                2: _row(scroll=True, follow=True, picture=True)}
        self.assertEqual(wc.required_flows_by_day(plan)[2], {FLOW_WARMUP})

    def test_a_follow_only_day_requires_the_warmup_flow(self):
        # No follow-only flow exists; the planner sends warm_up_process, so that
        # is what the day owes.
        self.assertEqual(wc.required_flows_by_day({1: _row(follow=True)})[1], {FLOW_WARMUP})

    def test_a_reel_only_last_day_cannot_finish_the_warmup(self):
        # The reel is stripped before it ever runs, so a plan ending on one would
        # hold every profile open forever if that day gated.
        plan = _live_plan()
        plan[5] = _row(reel=True)
        self.assertEqual(wc.required_flows_by_day(plan)[5], set())
        self.assertEqual(wc.gating_days(plan), [1, 2, 3, 4])
        self.assertEqual(wc.finish_day(plan), 4)

    def test_a_picture_or_bio_last_day_does_not_gate_either(self):
        for extra in ({"picture": True}, {"bio": True}):
            plan = {1: _row(scroll=True, follow=True), 2: _row(**extra)}
            self.assertEqual(wc.required_flows_by_day(plan)[2], set())
            self.assertEqual(wc.finish_day(plan), 1)

    def test_an_empty_day_is_kept_as_an_empty_requirement(self):
        # Dropping it would make "how long is the plan" and "which days gate"
        # the same question, and they are not.
        plan = {1: _row(scroll=True), 2: _row()}
        self.assertEqual(sorted(wc.required_flows_by_day(plan)), [1, 2])
        self.assertEqual(wc.gating_days(plan), [1])

    def test_unusable_day_keys_are_ignored_rather_than_raising(self):
        # A hand-edited Warmup Plan row is the client's to break; it must not
        # take the whole count down with it.
        plan = {"abc": _row(scroll=True), None: _row(scroll=True),
                0: _row(scroll=True), -3: _row(scroll=True),
                1: _row(scroll=True, follow=True)}
        self.assertEqual(wc.required_flows_by_day(plan), {1: {FLOW_WARMUP}})
        self.assertEqual(wc.finish_day(plan), 1)

    def test_a_numeric_string_day_is_honoured(self):
        self.assertEqual(wc.gating_days({"2": _row(scroll=True)}), [2])

    def test_a_none_row_is_treated_as_an_empty_day(self):
        self.assertEqual(wc.required_flows_by_day({1: None}), {1: set()})

    def test_gating_days_come_back_in_order_whatever_order_the_table_was_read_in(self):
        plan = {3: _row(scroll=True), 1: _row(scroll=True), 2: _row(scroll=True)}
        self.assertEqual(wc.gating_days(plan), [1, 2, 3])


class FinishDayTest(TestCase):

    def test_an_unreadable_plan_finishes_nobody(self):
        # Zero is not "finished immediately": a Warmup Plan table that fails to
        # read must not retag a fleet against a plan nobody chose.
        self.assertEqual(wc.finish_day({}), 0)
        self.assertEqual(wc.finish_day(None), 0)
        self.assertFalse(wc.is_finished(99, 0))
        self.assertFalse(wc.is_finished(99, wc.finish_day({})))

    def test_a_plan_of_nothing_but_pictures_finishes_nobody(self):
        self.assertEqual(wc.finish_day({1: _row(picture=True), 2: _row(bio=True)}), 0)

    def test_is_finished_wants_the_last_gating_day_completed(self):
        finish = wc.finish_day(_live_plan())
        self.assertFalse(wc.is_finished(3, finish))
        self.assertTrue(wc.is_finished(4, finish))
        self.assertTrue(wc.is_finished(5, finish))

    def test_is_finished_tolerates_missing_numbers(self):
        self.assertFalse(wc.is_finished(None, 4))
        self.assertFalse(wc.is_finished(0, 4))


class ParseRunAtTest(TestCase):

    def test_airtables_z_suffix_parses_as_utc(self):
        stamp = wc.parse_run_at("2026-08-05T12:00:00.000Z")
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertEqual(stamp.hour, 12)

    def test_a_naive_stamp_is_assumed_utc(self):
        # Airtable writes UTC; assuming local here would move rows across the
        # date boundary depending on which box read them.
        self.assertEqual(wc.parse_run_at("2026-08-05T12:00:00").utcoffset().total_seconds(), 0)

    def test_junk_and_blanks_come_back_as_none(self):
        for value in ("", None, "not a date", 0, []):
            self.assertIsNone(wc.parse_run_at(value))


class DayDoneTest(TestCase):
    """Days *completed*, from Done rows -- never days elapsed."""

    START = date(2026, 8, 1)

    def _done(self, day_of_august, flow=FLOW_WARMUP, result=at.RESULT_DONE):
        # Midday UTC so no plausible machine timezone can shift a row onto a
        # neighbouring date; the boundary itself is tested under a pinned TZ in
        # LocalDateBoundaryTest.
        return _entry(f"2026-08-{day_of_august:02d}T12:00:00.000Z", flow=flow, result=result)

    def test_three_consecutive_days_of_the_four_day_plan(self):
        history = [self._done(3), self._done(2), self._done(1)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 3)

    def test_a_catch_up_scroll_days_late_still_completes_day_four(self):
        """The regression the whole module exists for.

        Day 4 asks for a scroll; the run that finally does it lands on calendar
        day 6. Crediting it to the calendar would call the profile finished for
        a day it skipped -- or, as the old readers did, ignore it entirely and
        leave 41 phones parked at day 3 with nobody told to pick them up.
        """
        history = [self._done(6, flow=FLOW_SCROLL),
                   self._done(3), self._done(2), self._done(1)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 4)
        self.assertTrue(wc.is_finished(wc.day_done(history, self.START, _live_plan()),
                                       wc.finish_day(_live_plan())))

    def test_a_day_four_scroll_alone_does_not_finish_anybody(self):
        # Days 1-3 are still owed; the scroll cannot satisfy warm_up_process.
        history = [self._done(6, flow=FLOW_SCROLL)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 0)

    def test_two_runs_on_one_date_advance_exactly_one_plan_day(self):
        # A retry after a partial run must not push a profile a day ahead of a
        # plan it has not done.
        history = [self._done(1), self._done(1)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 1)

    def test_a_scroll_and_a_warmup_on_one_date_still_advance_one_day(self):
        history = [self._done(1), self._done(1, flow=FLOW_SCROLL)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 1)

    def test_a_missed_night_is_two_days_done_not_three(self):
        # Ran on calendar days 1 and 3. The gap is exactly what separates a
        # profile doing its plan from one that sat broken for a night.
        history = [self._done(3), self._done(1)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 2)

    def test_failed_skipped_and_running_rows_never_advance_it(self):
        for result in (at.RESULT_FAILED, at.RESULT_SKIPPED, at.RESULT_RUNNING, "", None):
            history = [self._done(1, result=result), self._done(2, result=result)]
            self.assertEqual(wc.day_done(history, self.START, _live_plan()), 0, result)

    def test_a_failed_night_between_two_good_ones_counts_two(self):
        history = [self._done(3), self._done(2, result=at.RESULT_FAILED), self._done(1)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 2)

    def test_rows_from_before_the_start_date_belong_to_a_previous_life(self):
        history = [self._done(1), self._done(2), self._done(3)]
        self.assertEqual(wc.day_done(history, date(2026, 8, 10), _live_plan()), 0)

    def test_a_row_on_the_start_date_itself_counts(self):
        self.assertEqual(wc.day_done([self._done(4)], date(2026, 8, 4), _live_plan()), 1)

    def test_no_start_date_means_nothing_can_be_attributed(self):
        history = [self._done(1), self._done(2), self._done(3)]
        self.assertEqual(wc.day_done(history, None, _live_plan()), 0)

    def test_an_unreadable_plan_gives_zero(self):
        history = [self._done(1), self._done(2), self._done(3)]
        for plan in ({}, None, {1: _row(picture=True)}):
            self.assertEqual(wc.day_done(history, self.START, plan), 0)

    def test_no_history_at_all_gives_zero(self):
        for history in ([], None):
            self.assertEqual(wc.day_done(history, self.START, _live_plan()), 0)

    def test_rows_with_an_unusable_timestamp_are_dropped_not_fatal(self):
        history = [_entry("", flow=FLOW_WARMUP), _entry("whenever"), self._done(1)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 1)

    def test_extra_days_beyond_the_plan_do_not_run_past_it(self):
        history = [self._done(d) for d in (1, 2, 3)] + [
            self._done(4, flow=FLOW_SCROLL), self._done(5, flow=FLOW_SCROLL)]
        self.assertEqual(wc.day_done(history, self.START, _live_plan()), 4)

    def test_a_gating_day_gap_returns_the_plan_day_not_the_count(self):
        # Day 2 asks for nothing, so the gating days are 1 and 3: completing two
        # of them means the profile is on plan day 3.
        plan = {1: _row(scroll=True, follow=True), 2: _row(picture=True),
                3: _row(scroll=True)}
        history = [self._done(1), self._done(2, flow=FLOW_SCROLL)]
        self.assertEqual(wc.gating_days(plan), [1, 3])
        self.assertEqual(wc.day_done(history, self.START, plan), 3)


class LocalDateBoundaryTest(TestCase):
    """Run dates are the server's local dates, because the start date is.

    `Warm-up Started` is stamped from the server's own `date.today()`, so a run
    logged at 00:30 local -- 15:30 UTC the previous day, on this box's tz --
    would be read as belonging to the day *before* the campaign began and thrown
    away. The timezone is pinned here rather than inherited so the test says the
    same thing on a developer laptop and on the server.
    """

    TZ = "Asia/Tokyo"  # UTC+9, no DST: a fixed nine-hour walk across midnight.

    def setUp(self):
        if not hasattr(time, "tzset"):
            self.skipTest("no tzset on this platform")
        previous = os.environ.get("TZ")
        os.environ["TZ"] = self.TZ
        time.tzset()

        def restore():
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore)

    def test_a_run_just_after_local_midnight_counts_for_the_local_day(self):
        # 2026-08-07T15:30Z is 00:30 on 2026-08-08 here. Read as UTC it lands on
        # the 7th, before the start date, and the night's work vanishes.
        history = [_entry("2026-08-07T15:30:00.000Z")]
        self.assertEqual(wc.day_done(history, date(2026, 8, 8), _live_plan()), 1)

    def test_two_runs_either_side_of_local_midnight_are_two_days(self):
        # Both stamps fall on 2026-08-08 in UTC; locally they are the 8th and the
        # 9th. Counting them as one date would hold the profile a day back.
        history = [_entry("2026-08-08T18:00:00.000Z"),   # 03:00 on the 9th, local
                   _entry("2026-08-08T02:00:00.000Z")]   # 11:00 on the 8th, local
        self.assertEqual(wc.day_done(history, date(2026, 8, 8), _live_plan()), 2)


class HistoryByKeyTest(TestCase):

    def test_a_serial_qualified_name_lands_under_the_serial(self):
        by_serial, by_name = wc.history_by_key([_log_row("Blank (5) [262894]")])
        self.assertEqual(sorted(by_serial), ["262894"])
        self.assertEqual(dict(by_name), {})

    def test_a_legacy_bare_name_row_lands_under_the_name(self):
        # MultiLogin names are not unique -- this workspace has three "Blank (5)"
        # -- so a row without a serial cannot be pinned to one twin.
        by_serial, by_name = wc.history_by_key([_log_row("Blank (5)")])
        self.assertEqual(dict(by_serial), {})
        self.assertEqual(sorted(by_name), ["Blank (5)"])

    def test_the_entry_carries_the_flow(self):
        # Completion is per-flow now: without this, two rows on one date are
        # indistinguishable from one day's work done twice.
        by_serial, _ = wc.history_by_key(
            [_log_row("Nikki 1 [262894]", flow=FLOW_SCROLL, notes="scrolled 600s")])
        entry = by_serial["262894"][0]
        self.assertEqual(entry["flow"], FLOW_SCROLL)
        self.assertEqual(entry["result"], at.RESULT_DONE)
        self.assertEqual(entry["notes"], "scrolled 600s")
        self.assertEqual(entry["at"], "2026-08-05T12:00:00.000Z")

    def test_newest_first_input_order_is_preserved(self):
        # `warmup_run_log` sorts newest first and the dashboard shows the head of
        # this list as "last run".
        rows = [_log_row("A [11]", run_at="2026-08-07T12:00:00.000Z", flow=FLOW_SCROLL),
                _log_row("B [11]", run_at="2026-08-06T12:00:00.000Z"),
                _log_row("C [11]", run_at="2026-08-05T12:00:00.000Z")]
        by_serial, _ = wc.history_by_key(rows)
        self.assertEqual([e["at"][:10] for e in by_serial["11"]],
                         ["2026-08-07", "2026-08-06", "2026-08-05"])

    def test_a_select_returned_as_a_dict_is_read_the_same_way(self):
        row = _log_row("Nikki 1 [7]")
        row["fields"][at.F_RUN_RESULT] = {"name": at.RESULT_DONE}
        row["fields"][at.F_RUN_FLOW] = {"name": FLOW_WARMUP}
        entry = wc.history_by_key([row])[0]["7"][0]
        self.assertEqual((entry["result"], entry["flow"]), (at.RESULT_DONE, FLOW_WARMUP))

    def test_empty_and_blank_rows_do_not_raise(self):
        by_serial, by_name = wc.history_by_key(None)
        self.assertEqual((dict(by_serial), dict(by_name)), ({}, {}))
        by_serial, by_name = wc.history_by_key([{}, {"fields": {}}])
        self.assertEqual(dict(by_serial), {})
        self.assertEqual([e["at"] for e in by_name[""]], ["", ""])


class DayDoneBySerialTest(TestCase):
    """The number the runner plans from and the dashboard prints, derived once."""

    def _profiles(self, **overrides):
        row = {"name": "Blank (5)", "warmup_started": "2026-08-01"}
        row.update(overrides)
        return {"262894": row}

    def test_a_profile_with_no_history_is_on_day_zero(self):
        self.assertEqual(
            wc.day_done_by_serial(self._profiles(), [], _live_plan()), {"262894": 0})

    def test_its_own_serial_qualified_rows_are_counted(self):
        rows = [_log_row("Blank (5) [262894]", run_at="2026-08-02T12:00:00.000Z"),
                _log_row("Blank (5) [262894]", run_at="2026-08-01T12:00:00.000Z")]
        self.assertEqual(
            wc.day_done_by_serial(self._profiles(), rows, _live_plan()), {"262894": 2})

    def test_another_phones_rows_are_not_borrowed(self):
        rows = [_log_row("Blank (5) [999999]", run_at="2026-08-02T12:00:00.000Z")]
        self.assertEqual(
            wc.day_done_by_serial(self._profiles(), rows, _live_plan()), {"262894": 0})

    def test_the_bare_name_fallback_carries_legacy_history(self):
        # Rows written before 2026-08-07 have no serial in them at all.
        rows = [_log_row("Blank (5)", run_at="2026-08-02T12:00:00.000Z"),
                _log_row("Blank (5)", run_at="2026-08-01T12:00:00.000Z")]
        self.assertEqual(
            wc.day_done_by_serial(self._profiles(), rows, _live_plan()), {"262894": 2})

    def test_the_fallback_is_used_only_when_the_serial_has_no_history(self):
        # A phone that ran under its serial has been seen; the name-keyed rows
        # may belong to either of its twins, so borrowing them would credit days
        # this profile never did.
        rows = [_log_row("Blank (5) [262894]", result=at.RESULT_FAILED,
                         run_at="2026-08-03T12:00:00.000Z"),
                _log_row("Blank (5)", run_at="2026-08-02T12:00:00.000Z"),
                _log_row("Blank (5)", run_at="2026-08-01T12:00:00.000Z")]
        self.assertEqual(
            wc.day_done_by_serial(self._profiles(), rows, _live_plan()), {"262894": 0})

    def test_a_profile_without_a_start_date_is_on_day_zero(self):
        rows = [_log_row("Blank (5) [262894]", run_at="2026-08-02T12:00:00.000Z")]
        profiles = self._profiles(warmup_started="")
        self.assertEqual(wc.day_done_by_serial(profiles, rows, _live_plan()), {"262894": 0})

    def test_every_profile_gets_an_answer_even_with_no_rows_and_no_plan(self):
        profiles = {"1": {"name": "A", "warmup_started": "2026-08-01"},
                    "2": {"name": "B", "warmup_started": "2026-08-01"}}
        self.assertEqual(wc.day_done_by_serial(profiles, [], {}), {"1": 0, "2": 0})
        self.assertEqual(wc.day_done_by_serial(None, [], _live_plan()), {})


if __name__ == "__main__":
    unittest.main()
