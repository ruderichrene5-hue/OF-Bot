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
