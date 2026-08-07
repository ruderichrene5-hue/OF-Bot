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
    PLAN = {1: {"Day": 1, "Scroll": True}, 2: {"Day": 2, "Scroll": True},
            3: {"Day": 3, "Scroll": True}}

    def _mlx(self, *names):
        return [{"serial_no": s, "serial_name": n, "id": f"L{s}",
                 "tags": ["Created"], "created_at": "2026-08-01T00:00:00Z"}
                for n, s in names]

    def _rows(self, *specs):
        return {s: {"record_id": f"rec{s}", "name": n, "api_id": f"L{s}",
                    "status": "Active", "warmup_started": started}
                for n, s, started in specs}

    def _log(self, *entries):
        return [{"id": f"r{i}", "fields": {"Name": f"{key} / warm_up_process / x",
                                           "Flow": "warm_up_process", "Result": result,
                                           "Run At": at, "Notes": notes}}
                for i, (key, result, at, notes) in enumerate(entries)]

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
            self._mlx(("Blank (5)", "100")),
            self._rows(("Blank (5)", "100", "2026-08-08")),
            self._log(("Blank (5)", "Done", "2026-08-09T09:00:00.000Z", "")))
        row = out["profiles"][0]
        self.assertTrue(row["ambiguous"])
        self.assertEqual(row["runs_done"], 1)

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


class WarmupProgressRenderTest(unittest.TestCase):
    def _render(self, **overrides):
        data = {"profiles": [], "plan_days": 3, "next_run": "2026-08-09 16:00",
                "last_run": "2026-08-09 15:00", "timer_stopped": False,
                "account_driven": False, "error": "",
                "counts": {"ok": 0, "failed": 0, "running": 0, "never": 0, "finished": 0}}
        data.update(overrides)
        return report_html._section_warmup_progress(data)

    def _row(self, **overrides):
        row = {"name": "Blank (5)", "serial": "262894", "launch_id": "L1", "day": 2,
               "started": "2026-08-08", "runs_done": 1, "runs_logged": 1,
               "last_at": "2026-08-09 09:00", "last_result": "Done", "last_notes": "",
               "state": "ok", "ambiguous": False}
        row.update(overrides)
        return row

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
