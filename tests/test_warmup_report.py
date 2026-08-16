"""The warm-up tab: who is warming up, and the field edit that starts everyone else.

The gate order tested here is `airtable_planner.plan_from_airtable`'s. If the two
ever disagree, the page tells somebody to flip a switch the loop then ignores --
worse than no page at all -- so these pin the *order*, not just the set.
"""

import unittest
from datetime import datetime

from adb_bot.automation import report, report_html


class WarmupStatusTest(unittest.TestCase):

    def setUp(self):
        report.invalidate_cache()
        self.addCleanup(report.invalidate_cache)

    class FakeAirtable:
        def __init__(self, accounts, plan=None, profiles=None):
            self._accounts, self._plan = accounts, plan
            self._profiles = profiles or {"prof1": {"launch_id": "L1", "name": "Nikki 1"}}

        def warmup_plan_by_day(self):
            if self._plan is not None:
                return self._plan
            return {1: {"Day": 1, "Scroll": True}, 2: {"Day": 2, "Scroll": True},
                    3: {"Day": 3, "Scroll": True}, 4: {"Day": 4, "Scroll": True}}

        def list_accounts(self):
            return self._accounts

        def profile_launch_map(self):
            return self._profiles

    @staticmethod
    def _account(name="acct", created="2026-08-06", stage="Active",
                 mode="Posting", verify=False, profile="prof1"):
        fields = {"Name": name, "Lifecycle Stage": stage, "Automation Mode": mode,
                  "Needs Human Verification": verify}
        if created:
            fields["Creation Date"] = created
        if profile:
            fields["Profile"] = [profile]
        return {"id": "rec" + name, "fields": fields}

    def _run(self, *accounts, plan=None, today="2026-08-06"):
        now = datetime.fromisoformat(today + "T10:00:00")
        return report.warmup_status(self.FakeAirtable(list(accounts), plan=plan), now=now)

    def test_an_active_account_on_day_one_is_running(self):
        row = self._run(self._account(created="2026-08-06"))["accounts"][0]
        self.assertEqual(row["day"], 1)
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["blocker"], "")
        self.assertFalse(row["stale_date"])

    def test_a_paused_stage_is_reported_as_the_blocker(self):
        out = self._run(self._account(stage="Paused"))
        self.assertEqual(out["accounts"][0]["blocker"], "lifecycle stage Paused")
        self.assertEqual(out["counts"]["blocked"], 1)

    def test_verification_outranks_every_other_reason(self):
        # Paused AND unverified AND undated: a person clears the verification
        # first, so that is the reason the row must carry.
        out = self._run(self._account(stage="Paused", mode="Paused",
                                      created="", verify=True))
        self.assertEqual(out["accounts"][0]["blocker"], "needs human verification")

    def test_mode_outranks_stage(self):
        out = self._run(self._account(stage="Paused", mode="Paused"))
        self.assertEqual(out["accounts"][0]["blocker"], "automation mode paused")

    def test_a_missing_launch_id_is_named_before_a_missing_date(self):
        out = self._run(self._account(created="", profile="gone"))
        self.assertEqual(out["accounts"][0]["blocker"],
                         "no MLX API ID on linked profile")

    def test_an_account_past_the_plan_is_finished_not_blocked(self):
        row = self._run(self._account(created="2026-06-17"))["accounts"][0]
        self.assertEqual(row["state"], "finished")
        self.assertEqual(row["blocker"], "")
        self.assertEqual(row["actions"], [])

    def test_a_future_creation_date_has_not_started(self):
        out = self._run(self._account(created="2026-08-20"))
        self.assertEqual(out["accounts"][0]["state"], "not_started")

    def test_a_paused_account_dated_months_ago_is_flagged_stale(self):
        """Un-pausing this one alone would run nothing. The page has to say so."""
        row = self._run(self._account(stage="Paused", created="2026-06-17"))["accounts"][0]
        self.assertTrue(row["stale_date"])
        self.assertGreater(row["day"], 4)

    def test_a_paused_account_dated_today_is_not_flagged_stale(self):
        out = self._run(self._account(stage="Paused", created="2026-08-06"))
        self.assertFalse(out["accounts"][0]["stale_date"])

    def test_a_placeholder_row_is_ignored(self):
        self.assertEqual(self._run({"id": "recblank", "fields": {}})["accounts"], [])

    def test_an_airtable_failure_is_reported_not_raised(self):
        class Broken:
            def warmup_plan_by_day(self):
                raise RuntimeError("no")

        out = report.warmup_status(Broken())
        self.assertIn("RuntimeError", out["error"])
        self.assertEqual(out["accounts"], [])


class WarmupRenderTest(unittest.TestCase):
    """The tab names the field to edit, not the state of the code."""

    def _render(self, **kw):
        base = {"plan": [{"day": 1, "actions": ["Warm-up (day 1)"]}],
                "plan_days": 4, "error": "",
                "counts": {"running": 0, "blocked": 1, "finished": 0, "not_started": 0},
                "accounts": [{"name": "nikki_1", "profile": "Nikki 1", "stage": "Paused",
                              "mode": "Posting", "created": "2026-06-17", "day": 51,
                              "state": "blocked", "blocker": "lifecycle stage Paused",
                              "stale_date": True, "actions": []}]}
        base.update(kw)
        return report_html._section_warmup(base)

    def test_it_names_the_field_and_the_value(self):
        self.assertIn("Lifecycle Stage to Active", self._render())

    def test_a_stale_date_is_called_out_as_a_second_edit(self):
        page = self._render()
        self.assertIn("and set Creation Date", page)
        self.assertIn("day 51 of a 4-day plan", page)

    def test_a_fresh_account_gets_no_date_warning(self):
        accounts = [{"name": "n", "profile": "p", "stage": "Paused", "mode": "Posting",
                     "created": "2026-08-06", "day": 1, "state": "blocked",
                     "blocker": "lifecycle stage Paused", "stale_date": False,
                     "actions": []}]
        self.assertNotIn("and set Creation Date", self._render(accounts=accounts))

    def test_an_error_is_shown_instead_of_an_empty_table(self):
        self.assertIn("Could not read", self._render(error="RuntimeError: no"))

    def test_no_accounts_reads_as_empty_not_broken(self):
        self.assertIn("No accounts", self._render(accounts=[]))


if __name__ == "__main__":
    unittest.main()


class WarmupProgressTest(unittest.TestCase):
    """The campaign table: 45 profiles moving through a plan an hour at a time,
    and which of them have stopped moving."""

    NOW = datetime(2026, 8, 9, 15, 0, 0)
    # The shape `warmup_plan_by_day` actually returns: lower-case normalised
    # keys, not the raw Airtable column names. This fixture used to carry
    # {"Day": 1, "Scroll": True}, which `lifecycle.plan_actions_from_row` reads
    # as a day asking for nothing at all -- so every test below was measured
    # against an empty plan, and the day-4 shape that broke 46 profiles in
    # production could never have been covered here.
    PLAN = {1: {"scroll": True, "follow": True},
            2: {"scroll": True, "follow": True},
            3: {"scroll": True, "follow": True}}

    def _mlx(self, *names):
        return [{"serial_no": s, "serial_name": n, "id": f"L{s}",
                 "tags": ["Created"], "created_at": "2026-08-01T00:00:00Z"}
                for n, s in names]

    def _rows(self, *specs):
        return {s: {"record_id": f"rec{s}", "name": n, "api_id": f"L{s}",
                    "status": "Active", "warmup_started": started}
                for n, s, started in specs}

    def _log(self, *entries):
        """Run Log rows. An entry may name its flow as a fifth element.

        The flow is not decoration any more: completion is per-flow, so a plan
        day asking for a scroll is not settled by a `warm_up_process` row.
        """
        rows = []
        for i, entry in enumerate(entries):
            key, result, at, notes = entry[:4]
            flow = entry[4] if len(entry) > 4 else "warm_up_process"
            rows.append({"id": f"r{i}",
                         "fields": {"Name": f"{key} / {flow} / x", "Flow": flow,
                                    "Result": result, "Run At": at, "Notes": notes}})
        return rows

    def _progress(self, mlx, rows, log, timers=None, plan=None):
        class Fake:
            def warmup_profiles_by_serial(inner): return rows
            def warmup_plan_by_day(inner): return self.PLAN if plan is None else plan
            def warmup_run_log(inner): return log
        return report.warmup_progress(Fake(), mlx_items=mlx, timers=timers, now=self.NOW)

    def test_a_profile_that_has_never_run_is_called_out(self):
        out = self._progress(self._mlx(("Blank (1)", "100")),
                             self._rows(("Blank (1)", "100", None)), [])
        row = out["profiles"][0]
        self.assertEqual(row["state"], "never")
        self.assertEqual(row["runs_done"], 0)
        self.assertEqual(row["day"], 1)

    def test_the_day_comes_from_warm_up_started_not_the_run_log(self):
        out = self._progress(self._mlx(("Blank (1)", "100")),
                             self._rows(("Blank (1)", "100", "2026-08-07")), [])
        self.assertEqual(out["profiles"][0]["day"], 3)

    def test_a_profile_can_reach_the_end_of_the_plan_having_completed_none_of_it(self):
        """The day advances on the calendar whether or not the run worked, so
        day number alone is not progress. `runs_done` is what says so."""
        out = self._progress(
            self._mlx(("Blank (1)", "100")),
            self._rows(("Blank (1)", "100", "2026-08-07")),
            self._log(("Blank (1) [100]", "Failed", "2026-08-08T10:00:00.000Z", "device offline")))
        row = out["profiles"][0]
        self.assertEqual((row["day"], row["runs_done"], row["state"]), (3, 0, "failed"))

    def test_the_last_run_result_and_note_are_carried(self):
        out = self._progress(
            self._mlx(("Blank (1)", "100")),
            self._rows(("Blank (1)", "100", "2026-08-08")),
            self._log(("Blank (1) [100]", "Done", "2026-08-09T09:00:00.000Z", ""),
                      ("Blank (1) [100]", "Failed", "2026-08-08T09:00:00.000Z", "went wrong")))
        row = out["profiles"][0]
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["last_result"], "Done")
        self.assertEqual(row["runs_done"], 1)
        self.assertEqual(row["runs_logged"], 2)

    def test_history_is_kept_per_profile_not_per_name(self):
        """Three profiles are called "Blank (5)" in this workspace. One twin's
        run must not show up as every twin's."""
        out = self._progress(
            self._mlx(("Blank (5)", "100"), ("Blank (5)", "200")),
            self._rows(("Blank (5)", "100", "2026-08-08"), ("Blank (5)", "200", "2026-08-08")),
            self._log(("Blank (5) [100]", "Done", "2026-08-09T09:00:00.000Z", "")))
        by_serial = {p["serial"]: p for p in out["profiles"]}
        self.assertEqual(by_serial["100"]["runs_done"], 1)
        self.assertEqual(by_serial["200"]["runs_done"], 0)
        self.assertEqual(by_serial["200"]["state"], "never")

    def test_a_legacy_row_is_shown_but_flagged_as_unpinnable(self):
        """Rows written before runs carried a serial cannot be attributed to one
        twin. Hiding them loses real history; splitting them invents it."""
        out = self._progress(
            self._mlx(("Blank (5)", "100"), ("Blank (5)", "200")),
            self._rows(("Blank (5)", "100", "2026-08-08"), ("Blank (5)", "200", "2026-08-08")),
            self._log(("Blank (5)", "Done", "2026-08-09T09:00:00.000Z", "")))
        self.assertTrue(all(p["ambiguous"] for p in out["profiles"]))
        self.assertTrue(all(p["runs_done"] == 1 for p in out["profiles"]))

    def test_legacy_history_under_a_unique_name_is_not_flagged(self):
        """Only a name two profiles share is ambiguous. Warning about a twin
        that does not exist is noise, and it is the common case: every run
        logged before the serial change has a bare name."""
        out = self._progress(
            self._mlx(("Blank (13)", "100")),
            self._rows(("Blank (13)", "100", "2026-08-08")),
            self._log(("Blank (13)", "Done", "2026-08-09T09:00:00.000Z", "")))
        row = out["profiles"][0]
        self.assertFalse(row["ambiguous"])
        self.assertEqual((row["runs_done"], row["state"]), (1, "ok"))

    def test_a_serial_qualified_row_beats_the_legacy_one(self):
        out = self._progress(
            self._mlx(("Blank (5)", "100")),
            self._rows(("Blank (5)", "100", "2026-08-08")),
            self._log(("Blank (5) [100]", "Done", "2026-08-09T09:00:00.000Z", ""),
                      ("Blank (5)", "Failed", "2026-08-08T09:00:00.000Z", "")))
        self.assertFalse(out["profiles"][0]["ambiguous"])

    def test_problems_sort_above_healthy_profiles(self):
        out = self._progress(
            self._mlx(("Good", "100"), ("Bad", "200")),
            self._rows(("Good", "100", "2026-08-08"), ("Bad", "200", "2026-08-08")),
            self._log(("Good [100]", "Done", "2026-08-09T09:00:00.000Z", ""),
                      ("Bad [200]", "Failed", "2026-08-09T09:00:00.000Z", "")))
        self.assertEqual([p["name"] for p in out["profiles"]], ["Bad", "Good"])

    def test_the_next_run_comes_from_the_loops_own_timer(self):
        out = self._progress(
            self._mlx(("Blank (1)", "100")), self._rows(("Blank (1)", "100", None)), [],
            timers=[{"loop": "warmup", "next": "2026-08-09 16:00", "last": "2026-08-09 15:00",
                     "stopped": False}])
        self.assertEqual(out["next_run"], "2026-08-09 16:00")
        self.assertEqual(out["last_run"], "2026-08-09 15:00")
        self.assertFalse(out["timer_stopped"])

    def test_an_untagged_profile_is_not_on_warm_up(self):
        mlx = self._mlx(("Blank (1)", "100"))
        mlx[0]["tags"] = ["Active / Posting"]
        out = self._progress(mlx, self._rows(("Blank (1)", "100", None)), [])
        self.assertEqual(out["profiles"], [])

    def test_a_missing_warm_up_started_field_says_which_field(self):
        class Fake:
            def warmup_profiles_by_serial(inner): return None
            def warmup_plan_by_day(inner): return {}
            def warmup_run_log(inner): return []
        self.assertIn("Warm-up Started", report.warmup_progress(Fake())["error"])

    def test_an_airtable_failure_degrades_rather_than_raising(self):
        class Broken:
            def warmup_profiles_by_serial(inner): raise RuntimeError("429 slow down")
        out = report.warmup_progress(Broken())
        self.assertIn("429", out["error"])
        self.assertEqual(out["profiles"], [])


class DayDoneTest(WarmupProgressTest):
    """`day_done` -- how many of the plan's days a profile has actually
    completed. What MultiLogin's "Warmup Day N Done" tags mean, and the number
    that stops the calendar from flattering a profile that has stalled.

    It counts plan days *completed*, not the campaign day a run landed on. The
    two agree only for a profile that never missed a night, and where they part
    is where the bug lived: crediting a run to the calendar day it happened on
    called profiles finished for days they had skipped entirely.
    """

    def _row(self, started, *entries):
        return self._progress(self._mlx(("Blank (1)", "100")),
                              self._rows(("Blank (1)", "100", started)),
                              self._log(*entries))["profiles"][0]

    def test_a_run_credits_the_next_plan_day_owed_not_the_calendar_day(self):
        """One Done run on campaign day 2 has completed day *one* of the plan.
        Day 1 was never run, and it is still owed."""
        row = self._row("2026-08-07",
                        ("Blank (1) [100]", "Done", "2026-08-08T10:00:00.000Z", ""))
        self.assertEqual((row["day"], row["day_done"]), (3, 1))

    def test_only_a_done_run_counts(self):
        row = self._row("2026-08-07",
                        ("Blank (1) [100]", "Failed", "2026-08-08T10:00:00.000Z", ""))
        self.assertEqual(row["day_done"], 0)

    def test_two_runs_on_one_day_do_not_advance_it_twice(self):
        """A retry after a partial run is still one day of the plan. Counting
        Done rows instead would put this profile a day ahead of where it is."""
        row = self._row("2026-08-07",
                        ("Blank (1) [100]", "Done", "2026-08-08T16:00:00.000Z", ""),
                        ("Blank (1) [100]", "Done", "2026-08-08T10:00:00.000Z", ""))
        self.assertEqual((row["runs_done"], row["day_done"]), (2, 1))

    def test_a_skipped_night_is_not_credited_by_the_run_that_follows_it(self):
        """Two runs, three calendar days apart: two days of the plan are done,
        not three. The old reading took the furthest campaign day a run landed
        on, so this profile read "day 3 done" having run twice -- and a plan of
        three days then called it finished."""
        row = self._row("2026-08-07",
                        ("Blank (1) [100]", "Done", "2026-08-09T10:00:00.000Z", ""),
                        ("Blank (1) [100]", "Done", "2026-08-07T10:00:00.000Z", ""))
        self.assertEqual(row["day_done"], 2)

    def test_a_profile_that_has_not_started_has_no_day_done(self):
        self.assertEqual(self._row(None)["day_done"], 0)

    def test_history_predating_the_start_date_does_not_go_negative(self):
        """`Warm-up Started` can be re-stamped by hand to restart a profile,
        which leaves Run Log rows sitting before day 1."""
        row = self._row("2026-08-08",
                        ("Blank (1) [100]", "Done", "2026-08-01T10:00:00.000Z", ""))
        self.assertEqual(row["day_done"], 0)


class DayFourScrollTest(WarmupProgressTest):
    """The client's actual plan, and the day that finished nothing.

    Days 1-3 ask for scroll + follow (`warm_up_process`); day 4 asks for a
    scroll and a reel, and `warmup_targets` strips the reel because reels belong
    to the Posting Queue -- so day 4's whole job is one `instagram_scroll` run.
    On 2026-08-11 not one of the 155 day-4 attempts had ever finished (the flow
    was missing from `FLOW_OPEN_SECONDS`, so the watchdog shut the phone at
    seven minutes), yet 41 profiles read `finished` here on the strength of the
    calendar alone and appeared on no worklist. These pin both halves: a day-4
    scroll finishes the profile, and its absence stalls it in public.
    """

    PLAN = {1: {"scroll": True, "follow": True},
            2: {"scroll": True, "follow": True},
            3: {"scroll": True, "follow": True},
            4: {"scroll": True, "reel": True}}

    DAYS_1_3 = (("Blank (1) [100]", "Done", "2026-08-06T10:00:00.000Z", ""),
                ("Blank (1) [100]", "Done", "2026-08-07T10:00:00.000Z", ""),
                ("Blank (1) [100]", "Done", "2026-08-08T10:00:00.000Z", ""))

    def _out(self, started, *entries):
        return self._progress(self._mlx(("Blank (1)", "100")),
                              self._rows(("Blank (1)", "100", started)),
                              self._log(*entries))

    @staticmethod
    def _profile():
        """A Profiles row for the same phone, with none of the three hand-off
        tasks ticked and no Stage written yet -- so what puts it on the hand-off
        list can only be the campaign's own reading of the Run Log."""
        return [{"record_id": "rec100", "name": "Blank (1)", "serial": "100",
                 "launch_id": "L100", "status": "Active", "warmup_stage": "",
                 "warmup_day": 4, "warmup_last_run": "", "handoff": {}}]

    def test_the_day_four_scroll_is_what_finishes_the_warm_up(self):
        out = self._out("2026-08-06", *self.DAYS_1_3,
                        ("Blank (1) [100]", "Done", "2026-08-09T10:00:00.000Z", "",
                         "instagram_scroll"))
        row = out["profiles"][0]
        self.assertEqual(out["finish_day"], 4)
        self.assertEqual((row["day"], row["day_done"], row["state"]), (4, 4, "finished"))

    def test_a_finished_profile_reaches_the_hand_off_list(self):
        out = self._out("2026-08-06", *self.DAYS_1_3,
                        ("Blank (1) [100]", "Done", "2026-08-09T10:00:00.000Z", "",
                         "instagram_scroll"))
        handoff = report.handoff_queue(self._profile(), out)
        self.assertEqual([p["serial"] for p in handoff["profiles"]], ["100"])
        self.assertEqual(handoff["finish_day"], 4)

    def test_without_the_day_four_scroll_it_is_stalled_not_finished(self):
        """Three days done, day five on the calendar, and nothing left to run
        it. This is the state 41 phones were in while the tab called them
        finished."""
        out = self._out("2026-08-05",
                        ("Blank (1) [100]", "Done", "2026-08-05T10:00:00.000Z", ""),
                        ("Blank (1) [100]", "Done", "2026-08-06T10:00:00.000Z", ""),
                        ("Blank (1) [100]", "Done", "2026-08-07T10:00:00.000Z", ""))
        row = out["profiles"][0]
        self.assertEqual((row["day"], row["day_done"], row["state"]), (5, 3, "stalled"))
        self.assertEqual(out["counts"]["stalled"], 1)
        self.assertEqual(out["counts"]["finished"], 0)

    def test_a_stalled_profile_is_not_offered_as_a_hand_off(self):
        """It has not finished. Putting it in front of a VA as ready for a bio
        and a picture is how the missing day-4 run stayed invisible."""
        out = self._out("2026-08-05",
                        ("Blank (1) [100]", "Done", "2026-08-05T10:00:00.000Z", ""),
                        ("Blank (1) [100]", "Done", "2026-08-06T10:00:00.000Z", ""),
                        ("Blank (1) [100]", "Done", "2026-08-07T10:00:00.000Z", ""))
        self.assertEqual(report.handoff_queue(self._profile(), out)["profiles"], [])

    def test_a_warm_up_process_row_does_not_settle_a_scroll_day(self):
        """Day 4 asks for `instagram_scroll`. A `warm_up_process` row on day 4
        is a different flow doing different work, and crediting it would hide
        exactly the failure this whole change is about."""
        out = self._out("2026-08-06", *self.DAYS_1_3,
                        ("Blank (1) [100]", "Done", "2026-08-09T10:00:00.000Z", ""))
        self.assertEqual(out["profiles"][0]["day_done"], 3)

    def test_the_stalled_profile_sorts_above_the_failures(self):
        """A failed run may well succeed tonight. A stalled profile is out of
        plan days, so nothing will retry it and somebody has to."""
        out = self._progress(
            self._mlx(("Stuck", "100"), ("Broken", "200")),
            self._rows(("Stuck", "100", "2026-08-04"), ("Broken", "200", "2026-08-08")),
            self._log(("Broken [200]", "Failed", "2026-08-09T10:00:00.000Z", "")))
        self.assertEqual([p["name"] for p in out["profiles"]], ["Stuck", "Broken"])

    def test_a_trailing_day_the_warm_up_never_runs_does_not_hold_it_open(self):
        """`plan_days` is how long the plan is; `finish_day` is the last day
        that has to be completed. A day asking only for a profile picture is a
        person's job -- gating on it would mean nobody ever finishes, and the
        list that asks that person for the picture would stay empty."""
        plan = {**{d: {"scroll": True, "follow": True} for d in (1, 2, 3)},
                4: {"picture": True}}
        out = self._progress(self._mlx(("Blank (1)", "100")),
                             self._rows(("Blank (1)", "100", "2026-08-07")),
                             self._log(("Blank (1) [100]", "Done", "2026-08-07T10:00:00.000Z", ""),
                                       ("Blank (1) [100]", "Done", "2026-08-08T10:00:00.000Z", ""),
                                       ("Blank (1) [100]", "Done", "2026-08-09T10:00:00.000Z", "")),
                             plan=plan)
        self.assertEqual((out["plan_days"], out["finish_day"]), (4, 3))
        self.assertEqual(out["profiles"][0]["state"], "finished")

    def test_an_unreadable_plan_calls_nobody_finished_and_says_why(self):
        """Falling back to a built-in length here would retire a fleet against a
        schedule nobody chose."""
        out = self._progress(self._mlx(("Blank (1)", "100")),
                             self._rows(("Blank (1)", "100", "2026-08-01")),
                             self._log(("Blank (1) [100]", "Done", "2026-08-08T10:00:00.000Z", "")),
                             plan={})
        self.assertEqual(out["finish_day"], 0)
        self.assertIn("Warmup Plan", out["plan_warning"])
        self.assertEqual(out["counts"]["finished"], 0)
        self.assertEqual(out["counts"]["stalled"], 0)


class WarmupProgressRenderTest(unittest.TestCase):
    def _render(self, **overrides):
        data = {"profiles": [], "plan_days": 3, "finish_day": 3, "plan_warning": "",
                "next_run": "2026-08-09 16:00",
                "last_run": "2026-08-09 15:00", "timer_stopped": False,
                "account_driven": False, "error": "",
                "counts": {"ok": 0, "failed": 0, "running": 0, "never": 0,
                           "stalled": 0, "finished": 0}}
        data.update(overrides)
        return report_html._section_warmup_progress(data)

    def _row(self, **overrides):
        row = {"name": "Blank (5)", "serial": "262894", "launch_id": "L1", "day": 2,
               "started": "2026-08-08", "runs_done": 1, "runs_logged": 1,
               "last_at": "2026-08-09 09:00", "last_result": "Done", "last_notes": "",
               "state": "ok", "ambiguous": False, "day_done": 2}
        row.update(overrides)
        return row

    def test_the_day_completed_sits_next_to_the_day_reached(self):
        """Two columns because they are two facts, and the gap between them is
        the profile that has stalled."""
        page = self._render(profiles=[self._row(day=3, day_done=1)])
        self.assertIn("3 of 3", page)
        self.assertIn(">day 1<", page)

    def test_a_profile_that_has_completed_nothing_shows_a_dash_not_day_zero(self):
        self.assertIn("<td class='num'>—</td>",
                      self._render(profiles=[self._row(day_done=0)]))

    def test_it_shows_day_run_count_and_when_it_next_runs(self):
        page = self._render(profiles=[self._row()], counts={"ok": 1})
        self.assertIn("2 of 3", page)
        self.assertIn("262894", page)
        self.assertIn("2026-08-09 09:00", page)
        self.assertIn("2026-08-09 16:00", page)

    def test_an_unscheduled_fleet_is_the_loudest_thing_on_the_section(self):
        """The failure that looks like success: the loop runs hourly, plans
        nothing against the Accounts table and exits 0."""
        page = self._render(profiles=[self._row()], account_driven=True)
        self.assertIn("these profiles are not scheduled", page)
        self.assertIn("--targets", page)

    def test_a_stopped_timer_is_called_out(self):
        self.assertIn("timer not active",
                      self._render(profiles=[self._row()], timer_stopped=True))

    def test_a_failure_note_is_carried_to_the_reader(self):
        page = self._render(profiles=[self._row(state="failed", last_result="Failed",
                                                last_notes="profile lost mid-run")],
                            counts={"failed": 1})
        self.assertIn("profile lost mid-run", page)
        self.assertIn("last run failed", page)

    def test_unpinnable_history_says_so(self):
        page = self._render(profiles=[self._row(ambiguous=True)])
        self.assertIn("shares its MultiLogin name", page)

    def test_nothing_tagged_explains_the_tag_rather_than_showing_a_blank(self):
        self.assertIn("Created", self._render(profiles=[]))

    def test_a_profile_name_is_escaped(self):
        page = self._render(profiles=[self._row(name="<script>x</script>")])
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("&lt;script&gt;", page)


class WarmupWaitingTest(unittest.TestCase):
    """Phones that exist and are not being warmed up.

    New phones are cloned in batches days before anyone works through them, and
    the only thing that puts one on warm-up is a person adding `Created`. That
    gate is right, but until this list existed nothing anywhere said the
    untagged batch was there -- every dashboard read green with 17 phones idle.
    """

    def _item(self, serial="100", name="Blank (1)", tags=(), created="2026-08-06"):
        return {"serial_no": serial, "serial_name": name, "id": f"L{serial}",
                "tags": list(tags), "created_at": f"{created}T01:00:00Z"}

    def _rows(self, serial="100", name="Blank (1)", status="Active", api_id=None,
              needs_human=False):
        return {serial: {"record_id": f"rec{serial}", "name": name,
                         "api_id": api_id if api_id is not None else f"L{serial}",
                         "status": status, "warmup_started": None,
                         "needs_human": needs_human}}

    def _waiting(self, items=None, rows=None, in_warmup=()):
        return report.warmup_waiting(items if items is not None else [self._item()],
                                     rows if rows is not None else self._rows(),
                                     in_warmup=set(in_warmup))

    def test_an_untagged_active_profile_is_waiting(self):
        row = self._waiting()[0]
        self.assertEqual(row["serial"], "100")
        self.assertEqual(row["reason"], "no tag at all")

    def test_a_profile_already_on_warm_up_is_not_listed(self):
        self.assertEqual(self._waiting(in_warmup=("100",)), [])

    def test_a_profile_tagged_something_else_says_which(self):
        row = self._waiting([self._item(tags=["gmail"])])[0]
        self.assertIn("gmail", row["reason"])
        self.assertIn("not Created", row["reason"])

    def test_a_created_profile_is_never_told_it_is_not_created(self):
        """It carries the tag. `collect_warmup_targets` refused it for the flag,
        and saying "not Created" sends somebody to add a tag that is already
        there while the real blocker goes unmentioned. Five phones on
        2026-08-16, every one of them flagged."""
        row = self._waiting([self._item(tags=["Created", "Issue"])],
                            rows=self._rows(needs_human=True))[0]
        self.assertNotIn("not Created", row["reason"])
        self.assertIn("flagged", row["reason"])

    def test_a_created_profile_with_nothing_wrong_is_simply_due(self):
        row = self._waiting([self._item(tags=["Created"])])[0]
        self.assertNotIn("not Created", row["reason"])
        self.assertIn("next warm-up tick", row["reason"])

    def test_the_untagged_reason_is_unchanged_for_a_phone_that_is_untagged(self):
        row = self._waiting([self._item(tags=["Account creation done"])],
                            rows=self._rows(needs_human=True))[0]
        self.assertIn("not Created", row["reason"])

    def test_a_profile_already_posting_is_not_a_candidate(self):
        """87 profiles are past the warm-up. Listing them would bury the few
        that are actually waiting."""
        for tag in ("Active / Posting", "Ready for Posting", "Banned / Dead"):
            self.assertEqual(self._waiting([self._item(tags=[tag])]), [], tag)

    def test_a_parked_profile_is_not_waiting_on_anybody(self):
        self.assertEqual(self._waiting(rows=self._rows(status="Inactive")), [])

    def test_a_profile_mlx_has_and_airtable_does_not_points_at_the_sync(self):
        row = self._waiting(rows={})[0]
        self.assertIn("mlx-sync", row["reason"])

    def test_a_row_with_no_launch_key_says_so(self):
        row = self._waiting(rows=self._rows(api_id=""))[0]
        self.assertIn("nothing can launch it", row["reason"])

    def test_the_newest_batch_is_first(self):
        items = [self._item("100", "Old", created="2026-07-01"),
                 self._item("200", "New", created="2026-08-06")]
        rows = {**self._rows("100", "Old"), **self._rows("200", "New")}
        self.assertEqual([p["name"] for p in self._waiting(items, rows)], ["New", "Old"])


class WarmupWaitingRenderTest(unittest.TestCase):
    def _render(self, waiting, error=""):
        return report_html._section_warmup_waiting({"waiting": waiting, "error": error})

    def _row(self, **kw):
        row = {"name": "Blank (2)", "serial": "262891", "tags": "",
               "created": "2026-08-06", "reason": "no tag at all"}
        row.update(kw)
        return row

    def test_it_names_the_profiles_and_the_edit_that_starts_them(self):
        page = self._render([self._row()])
        self.assertIn("262891", page)
        self.assertIn("no tag at all", page)
        self.assertIn("Created", page)

    def test_it_counts_the_wholly_untagged_separately(self):
        page = self._render([self._row(), self._row(serial="1", tags="gmail",
                                                    reason="tagged gmail, not Created")])
        self.assertIn("2 profile(s)", page)
        self.assertIn("1 of them carry no tag at all", page)

    def test_an_empty_list_says_nothing_is_unclaimed(self):
        self.assertIn("Nothing is sitting unclaimed", self._render([]))

    def test_a_broken_progress_read_renders_nothing_rather_than_a_second_error(self):
        self.assertEqual(self._render([], error="429"), "")

    def test_a_profile_name_is_escaped(self):
        page = self._render([self._row(name="<script>x</script>")])
        self.assertNotIn("<script>x</script>", page)


class ActivityOnlyReadingTest(WarmupProgressTest):
    """The pill says how the *warm-up* is going, not how the hand-off is going.

    The client's plan asks for a profile picture on day 2 and a bio on day 3,
    and the bot cannot do either without a photo or a bio somebody has to
    supply: on 2026-08-11 the base held 52 `update_profile_picture` rows and not
    one was Done -- every one skipped `no profile picture`. Reading the newest
    row of the whole warm-up history would therefore show every healthy profile
    as "last run failed" on its day 2 and its day 3, and publish
    `Warm-up Last Result = Skipped` for a phone whose scroll went fine. That is
    the same blindness this tab was fixed to remove, pointing the other way.
    Those rows belong to the Needs human tab; they are not this pill.
    """

    PLAN = {1: {"scroll": True, "follow": True},
            2: {"scroll": True, "follow": True, "picture": True},
            3: {"scroll": True, "follow": True, "bio": True},
            4: {"scroll": True, "reel": True}}

    def _out(self, *entries):
        return self._progress(self._mlx(("Blank (1)", "100")),
                              self._rows(("Blank (1)", "100", "2026-08-08")),
                              self._log(*entries))["profiles"][0]

    def test_a_skipped_picture_does_not_make_a_good_scroll_look_failed(self):
        row = self._out(
            ("Blank (1) [100]", "Skipped", "2026-08-09T12:00:00.000Z",
             "no profile picture (tick 'Use UI flow' and pick a photo)",
             "update_profile_picture"),
            ("Blank (1) [100]", "Done", "2026-08-09T11:00:00.000Z", ""))
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["last_result"], "Done")

    def test_a_skipped_picture_is_not_counted_as_a_warm_up_run(self):
        row = self._out(
            ("Blank (1) [100]", "Skipped", "2026-08-09T12:00:00.000Z", "",
             "update_profile_picture"),
            ("Blank (1) [100]", "Done", "2026-08-09T11:00:00.000Z", ""))
        self.assertEqual((row["runs_done"], row["runs_logged"]), (1, 1))

    def test_a_failed_scroll_is_still_a_failure(self):
        """The filter is about which flows answer the question, not about
        hiding bad news: the activity flow's own result still decides."""
        row = self._out(
            ("Blank (1) [100]", "Skipped", "2026-08-09T12:00:00.000Z", "",
             "update_profile_picture"),
            ("Blank (1) [100]", "Failed", "2026-08-09T11:00:00.000Z", "device offline"))
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["last_result"], "Failed")

    def test_a_day_the_picture_never_ran_still_completes(self):
        """Day 2 asks for a picture the bot cannot set. If that gated the day,
        no profile could ever finish and the tab that asks a person for the
        picture would sit empty waiting for the picture."""
        row = self._out(
            ("Blank (1) [100]", "Done", "2026-08-08T10:00:00.000Z", ""),
            ("Blank (1) [100]", "Done", "2026-08-09T10:00:00.000Z", ""),
            ("Blank (1) [100]", "Skipped", "2026-08-09T10:30:00.000Z", "",
             "update_profile_picture"))
        self.assertEqual(row["day_done"], 2)
