"""The client-editable Warmup Plan table drives the warm-up.

Covers the pure checkbox -> flow mapping, the Airtable read, and the planner
integration (table wins; missing table falls back to the built-in schedule).
"""
import logging
from datetime import date
from unittest import TestCase

from adb_bot.automation import lifecycle
from adb_bot.automation.airtable_planner import plan_airtable_runs
from adb_bot.clients import airtable as at
from adb_bot.clients.airtable import AirtableClient

LOG = logging.getLogger("test")
START = date(2026, 1, 1)


def _row(scroll=False, follow=False, feed_posts=0, picture=False, bio=False, reel=False, notes=None):
    return {"scroll": scroll, "follow": follow, "feed_posts": feed_posts,
            "picture": picture, "bio": bio, "reel": reel, "notes": notes}


class RowMappingTest(TestCase):
    def test_scroll_and_follow_maps_to_the_warmup_flow(self):
        actions, warnings = lifecycle.plan_actions_from_row(1, _row(scroll=True, follow=True))
        self.assertEqual([a.flow for a in actions], [lifecycle.FLOW_WARMUP])
        self.assertEqual(warnings, [])

    def test_scroll_only_uses_the_scroll_flow(self):
        # warm_up_process also follows people, so it would over-deliver here.
        actions, _ = lifecycle.plan_actions_from_row(1, _row(scroll=True))
        self.assertEqual([a.flow for a in actions], [lifecycle.FLOW_SCROLL_ONLY])

    def test_follow_only_falls_back_to_the_warmup_flow(self):
        actions, _ = lifecycle.plan_actions_from_row(1, _row(follow=True))
        self.assertEqual([a.flow for a in actions], [lifecycle.FLOW_WARMUP])

    def test_picture_bio_and_reel_map_to_their_flows(self):
        actions, _ = lifecycle.plan_actions_from_row(
            2, _row(scroll=True, follow=True, picture=True, bio=True, reel=True))
        self.assertEqual(
            [a.flow for a in actions],
            [lifecycle.FLOW_WARMUP, lifecycle.FLOW_UPDATE_PICTURE,
             lifecycle.FLOW_UPDATE_BIO, lifecycle.FLOW_REEL],
        )

    def test_empty_row_plans_nothing(self):
        actions, warnings = lifecycle.plan_actions_from_row(5, _row())
        self.assertEqual(actions, [])
        self.assertEqual(warnings, [])

    def test_feed_posts_warns_rather_than_silently_doing_nothing(self):
        # No flow implements feed posts. A silent drop would look like success.
        actions, warnings = lifecycle.plan_actions_from_row(2, _row(scroll=True, feed_posts=2))
        self.assertEqual([a.flow for a in actions], [lifecycle.FLOW_SCROLL_ONLY])
        self.assertEqual(len(warnings), 1)
        self.assertIn("feed posts", warnings[0])
        self.assertIn("2", warnings[0])


class TableLookupTest(TestCase):
    def test_day_present_in_table(self):
        actions, _ = lifecycle.plan_actions_from_table(3, {3: _row(bio=True)})
        self.assertEqual([a.flow for a in actions], [lifecycle.FLOW_UPDATE_BIO])

    def test_day_past_the_end_of_the_plan_is_empty(self):
        # Warm-up is over; posting is driven by the Posting Queue from here on,
        # so planning reels as well would post twice.
        actions, warnings = lifecycle.plan_actions_from_table(9, {1: _row(scroll=True)})
        self.assertEqual(actions, [])
        self.assertEqual(warnings, [])

    def test_day_zero_is_empty(self):
        self.assertEqual(lifecycle.plan_actions_from_table(0, {1: _row(scroll=True)}), ([], []))


class WarmupPlanReadTest(TestCase):
    """AirtableClient.warmup_plan_by_day parsing, with _list_table stubbed."""

    def _client(self, rows=None, raises=False):
        client = AirtableClient("tok", "appX", "Accounts")

        def fake_list_table(table, fields=None, filter_formula=None):
            if raises:
                raise RuntimeError("no such table")
            return rows or []

        client._list_table = fake_list_table
        return client

    def test_parses_rows_keyed_by_day(self):
        client = self._client([
            {"fields": {at.F_WP_DAY: 1, at.F_WP_SCROLL: True, at.F_WP_FOLLOW: True}},
            {"fields": {at.F_WP_DAY: 2, at.F_WP_FEED_POSTS: 2, at.F_WP_PICTURE: True}},
        ])
        plan = client.warmup_plan_by_day()
        self.assertEqual(sorted(plan), [1, 2])
        self.assertTrue(plan[1]["scroll"])
        self.assertTrue(plan[1]["follow"])
        self.assertEqual(plan[2]["feed_posts"], 2)
        self.assertTrue(plan[2]["picture"])
        self.assertFalse(plan[2]["bio"])

    def test_rows_without_a_usable_day_are_ignored(self):
        client = self._client([
            {"fields": {at.F_WP_SCROLL: True}},                    # no Day
            {"fields": {at.F_WP_DAY: "abc", at.F_WP_SCROLL: True}},  # unparsable
            {"fields": {at.F_WP_DAY: 0, at.F_WP_SCROLL: True}},      # out of range
            {"fields": {at.F_WP_DAY: 4, at.F_WP_REEL: True}},
        ])
        self.assertEqual(sorted(client.warmup_plan_by_day()), [4])

    def test_missing_table_returns_empty_rather_than_raising(self):
        # A base without the table must keep working on the built-in schedule.
        self.assertEqual(self._client(raises=True).warmup_plan_by_day(), {})


class FakePlannerClient:
    """Minimal stand-in for AirtableClient as the planner uses it."""

    def __init__(self, warmup_plan=None, plan_raises=False, creation=START):
        self._warmup_plan = warmup_plan or {}
        self._plan_raises = plan_raises
        self._creation = creation

    def list_accounts(self):
        return [{
            "id": "acc1",
            "fields": {
                at.F_ACC_NAME: "tester",
                at.F_ACC_PROFILE: ["prof1"],
                at.F_ACC_LIFECYCLE_STAGE: "Warmup",
                at.F_ACC_CREATION_DATE: self._creation.isoformat(),
                at.F_ACC_BIO: "hello",
            },
        }]

    def profile_launch_map(self):
        return {"prof1": {"name": "P1", "launch_id": "123456789012345678", "serial": "1"}}

    def todays_completed_runs(self):
        return set()

    def warmup_plan_by_day(self):
        if self._plan_raises:
            raise RuntimeError("boom")
        return self._warmup_plan


class PlannerIntegrationTest(TestCase):
    def test_table_drives_the_plan(self):
        client = FakePlannerClient(warmup_plan={1: _row(scroll=True, follow=True, bio=True)})
        plan = plan_airtable_runs(client, today=START, logger=LOG)
        self.assertEqual(len(plan.plans), 1)
        self.assertEqual(
            [r.flow for r in plan.plans[0].runs],
            [lifecycle.FLOW_WARMUP, lifecycle.FLOW_UPDATE_BIO],
        )

    def test_day_past_the_table_plans_nothing(self):
        # Day 9 of a 4-day plan: warm-up finished, nothing due.
        client = FakePlannerClient(warmup_plan={1: _row(scroll=True)}, creation=date(2025, 12, 24))
        plan = plan_airtable_runs(client, today=START, logger=LOG)
        self.assertEqual(plan.plans, [])

    def test_empty_table_falls_back_to_the_builtin_schedule(self):
        client = FakePlannerClient(warmup_plan={})
        plan = plan_airtable_runs(client, today=START, logger=LOG)
        self.assertEqual([r.flow for r in plan.plans[0].runs], [lifecycle.FLOW_WARMUP])

    def test_unreadable_table_falls_back_instead_of_failing_the_run(self):
        client = FakePlannerClient(plan_raises=True)
        plan = plan_airtable_runs(client, today=START, logger=LOG)
        self.assertEqual([r.flow for r in plan.plans[0].runs], [lifecycle.FLOW_WARMUP])

    def test_reels_run_by_default(self):
        # Media is wired now, so a plan row asking for a reel fires without any
        # extra flag. Pinned by a test so the default can't flip back unnoticed.
        client = FakePlannerClient(warmup_plan={1: _row(reel=True)})
        plan = plan_airtable_runs(client, today=START, logger=LOG)
        self.assertEqual([r.flow for r in plan.plans[0].runs], [lifecycle.FLOW_REEL])

    def test_reels_can_still_be_disabled(self):
        client = FakePlannerClient(warmup_plan={1: _row(reel=True)})
        gated = plan_airtable_runs(client, today=START, logger=LOG, run_reels=False)
        self.assertEqual(gated.plans, [])

    def test_override_flow_ignores_the_table(self):
        client = FakePlannerClient(warmup_plan={1: _row(scroll=True, follow=True)})
        plan = plan_airtable_runs(client, today=START, logger=LOG, override_flow="update_bio_u2")
        self.assertEqual([r.flow for r in plan.plans[0].runs], ["update_bio_u2"])
