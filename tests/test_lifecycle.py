from datetime import date
from unittest import TestCase

from adb_bot.automation.lifecycle import (
    FLOW_REEL,
    FLOW_UPDATE_BIO,
    FLOW_UPDATE_PICTURE,
    FLOW_WARMUP,
    STAGE_NOT_STARTED,
    STAGE_POSTING,
    STAGE_WARMUP,
    WARMUP_DAYS,
    campaign_stage,
    day_number,
    describe_campaign,
    plan_actions_for_day,
)

START = date(2026, 1, 1)


class LifecycleTest(TestCase):
    def test_day_number(self):
        self.assertEqual(day_number(START, date(2026, 1, 1)), 1)
        self.assertEqual(day_number(START, date(2026, 1, 3)), 3)
        self.assertEqual(day_number(START, date(2025, 12, 31)), 0)  # not started

    def test_stage(self):
        self.assertEqual(campaign_stage(START, date(2025, 12, 31)), STAGE_NOT_STARTED)
        self.assertEqual(campaign_stage(START, date(2026, 1, 1)), STAGE_WARMUP)
        self.assertEqual(campaign_stage(START, date(2026, 1, 5)), STAGE_WARMUP)
        self.assertEqual(campaign_stage(START, date(2026, 1, 6)), STAGE_POSTING)

    def test_every_warmup_day_is_warmup_only(self):
        for d in (1, 2, 3, 4, 5):
            actions = plan_actions_for_day(START, date(2026, 1, d))
            self.assertEqual([a.flow for a in actions], [FLOW_WARMUP])

    def test_no_warmup_day_sets_the_picture_or_bio(self):
        """Dropped from the warm-up on 2026-08-06 (client's call) -- picture and
        bio are set up outside it now. The flows themselves still exist for the
        UI and for a Warmup Plan row that asks for them; what changed is that
        the built-in schedule never asks on its own."""
        scheduled = {a.flow for d in range(1, WARMUP_DAYS + 1)
                     for a in plan_actions_for_day(START, date(2026, 1, d))}
        self.assertNotIn(FLOW_UPDATE_PICTURE, scheduled)
        self.assertNotIn(FLOW_UPDATE_BIO, scheduled)

    def test_day_6_onward_three_reels_with_times(self):
        actions = plan_actions_for_day(START, date(2026, 1, 6))
        self.assertEqual([a.flow for a in actions], [FLOW_REEL, FLOW_REEL, FLOW_REEL])
        self.assertEqual([a.scheduled_time for a in actions], ["09:00", "14:00", "19:00"])

    def test_custom_reel_times(self):
        actions = plan_actions_for_day(START, date(2026, 1, 10), reel_times=("08:00", "20:00"))
        self.assertEqual([a.scheduled_time for a in actions], ["08:00", "20:00"])

    def test_not_started_returns_nothing(self):
        self.assertEqual(plan_actions_for_day(START, date(2025, 12, 30)), [])

    def test_describe_campaign_shape(self):
        preview = describe_campaign(START, num_days=6)
        self.assertEqual(len(preview), 6)
        self.assertEqual(preview[0]["day"], 1)
        self.assertEqual(preview[2]["day"], 3)
        self.assertEqual(len(preview[2]["actions"]), 1)  # warm-up only, every warm-up day
        self.assertEqual(preview[5]["stage"], STAGE_POSTING)
        self.assertEqual(len(preview[5]["actions"]), 3)  # three reels
