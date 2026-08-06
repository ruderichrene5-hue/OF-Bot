"""Profile-driven warm-up: who gets warmed up, and on which day.

The MLX items here carry the field names a live probe of the workspace returned
on 2026-08-06 (`tags`, `serial_no`, `id`, `serial_name`, `status`), so the
selection is tested against the payload shape the API actually sends.
"""

from datetime import date
from unittest import TestCase

from adb_bot.automation import lifecycle, warmup_targets
from adb_bot.automation.warmup_targets import (
    WARMUP_TAG, collect_warmup_targets, has_tag, plan_profile_warmup, stamp_started,
)

TODAY = date(2026, 8, 6)


def _mlx(serial="254765", name="Blank (1)", tags=("Created",), api_id="631202076931653950"):
    return {"serial_no": serial, "id": api_id, "serial_name": name,
            "status": 2, "tags": list(tags), "created_at": "2026-08-03T11:09:18.574134Z",
            "equipment_info": {}, "proxy": {}}


def _row(record_id="recP1", name="Blank (1)", api_id="631202076931653950",
         status="Active", started=None):
    return {"record_id": record_id, "name": name, "api_id": api_id,
            "status": status, "warmup_started": started}


class TagSelectionTest(TestCase):
    def test_only_tagged_profiles_are_warmed_up(self):
        items = [_mlx(serial="1", name="Blank (1)"),
                 _mlx(serial="2", name="Nikki 3", tags=("Active / Posting",)),
                 _mlx(serial="3", name="Katja 1", tags=())]
        rows = {"1": _row("recA"), "2": _row("recB"), "3": _row("recC")}
        targets, skipped = collect_warmup_targets(items, rows, today=TODAY)
        self.assertEqual([t.serial_no for t in targets], ["1"])
        # An untagged profile is not a problem to report -- with 150 profiles in
        # the workspace that would bury the skips that mean something.
        self.assertEqual(skipped, [])

    def test_the_tag_is_matched_case_insensitively(self):
        self.assertTrue(has_tag(["created"]))
        self.assertTrue(has_tag([" Created "]))
        self.assertTrue(has_tag(["Issue", "Created"]))
        self.assertFalse(has_tag(["Ready for Posting"]))
        self.assertFalse(has_tag([]))

    def test_a_second_tag_alongside_created_still_counts(self):
        targets, _ = collect_warmup_targets([_mlx(tags=("Created", "gmail"))],
                                            {"254765": _row()}, today=TODAY)
        self.assertEqual(len(targets), 1)

    def test_profiles_are_matched_by_serial_not_name(self):
        """This workspace has three different profiles called "Blank (1)",
        created on different days. Matching on the name would launch the wrong
        phone, so the serial is the key."""
        items = [_mlx(serial="254765", name="Blank (1)", api_id="aaa"),
                 _mlx(serial="262890", name="Blank (1)", api_id="bbb")]
        rows = {"254765": _row("recOld", api_id="aaa")}
        targets, skipped = collect_warmup_targets(items, rows, today=TODAY)
        self.assertEqual([(t.serial_no, t.launch_id) for t in targets], [("254765", "aaa")])
        self.assertIn("no Profiles (Cloning) row yet", skipped[0].reason)

    def test_a_profile_missing_from_airtable_says_to_run_mlx_sync(self):
        _targets, skipped = collect_warmup_targets([_mlx()], {}, today=TODAY)
        self.assertIn("mlx-sync", skipped[0].reason)

    def test_a_row_without_a_launch_key_is_skipped(self):
        _targets, skipped = collect_warmup_targets([_mlx()], {"254765": _row(api_id=None)},
                                                   today=TODAY)
        self.assertIn("no MLX API ID", skipped[0].reason)

    def test_an_inactive_airtable_row_is_parked(self):
        """Status is how a person takes a profile out of the run, exactly as the
        queue and pipeline loops honour it."""
        _targets, skipped = collect_warmup_targets([_mlx()], {"254765": _row(status="Inactive")},
                                                   today=TODAY)
        self.assertIn("Inactive", skipped[0].reason)


class DayNumberTest(TestCase):
    def test_a_profile_with_no_start_date_is_on_day_1(self):
        targets, _ = collect_warmup_targets([_mlx()], {"254765": _row()}, today=TODAY)
        self.assertEqual(targets[0].day, 1)
        self.assertTrue(targets[0].needs_start_date)

    def test_the_day_counts_from_the_stored_start_date(self):
        targets, _ = collect_warmup_targets([_mlx()], {"254765": _row(started="2026-08-04")},
                                            today=TODAY)
        self.assertEqual(targets[0].day, 3)
        self.assertFalse(targets[0].needs_start_date)

    def test_mlx_creation_date_does_not_decide_the_day(self):
        """A profile created in MLX days ago still starts at day 1 when the
        warm-up begins -- counting from created_at would push a batch made last
        week straight past the plan without ever warming it up."""
        old = _mlx()
        old["created_at"] = "2026-07-01T00:00:00Z"
        targets, _ = collect_warmup_targets([old], {"254765": _row()}, today=TODAY)
        self.assertEqual(targets[0].day, 1)


class PlanTest(TestCase):
    def _plan(self, rows=None, **kwargs):
        return plan_profile_warmup([_mlx()], rows or {"254765": _row()}, today=TODAY, **kwargs)

    def test_a_day_1_profile_is_planned_for_the_warmup_flow(self):
        plan = self._plan()
        self.assertEqual(len(plan.plans), 1)
        entry = plan.plans[0]
        self.assertEqual([r.flow for r in entry.runs], [lifecycle.FLOW_WARMUP])
        self.assertEqual(entry.launch_id, "631202076931653950")
        # No Accounts row exists; the runner writes an unlinked Run Log for it.
        self.assertIsNone(entry.account_id)

    def test_the_warmup_never_schedules_the_picture_or_bio(self):
        """Taken out of the warm-up on 2026-08-06 (client's call)."""
        flows = set()
        for day, started in ((1, None), (2, "2026-08-05"), (3, "2026-08-04"),
                             (4, "2026-08-03"), (5, "2026-08-02")):
            plan = self._plan(rows={"254765": _row(started=started)})
            flows.update(r.flow for e in plan.plans for r in e.runs)
        self.assertNotIn(lifecycle.FLOW_UPDATE_PICTURE, flows)
        self.assertNotIn(lifecycle.FLOW_UPDATE_BIO, flows)

    def test_a_finished_warmup_is_reported_not_rerun(self):
        plan = self._plan(rows={"254765": _row(started="2026-07-01")})
        self.assertEqual(plan.plans, [])
        self.assertIn("warm-up finished", plan.skipped[0].reason)

    def test_todays_run_is_not_repeated(self):
        """The warm-up timer fires hourly; without this every tick would run the
        same day's warm-up again."""
        plan = self._plan(completed={("Blank (1)", lifecycle.FLOW_WARMUP)})
        self.assertEqual(plan.plans, [])
        self.assertIn("already run today", plan.skipped[0].reason)

    def test_the_client_table_drives_the_day_when_it_is_populated(self):
        table = {1: {"scroll": True, "follow": False, "feed_posts": 0, "picture": False,
                     "bio": False, "reel": False, "notes": None}}
        plan = self._plan(warmup_plan=table)
        self.assertEqual([r.flow for r in plan.plans[0].runs], [lifecycle.FLOW_SCROLL_ONLY])

    def test_a_reel_day_is_left_to_the_posting_queue(self):
        """A `Created` profile has no model, so no spoofed variant exists for it
        -- a reel scheduled here could only fail for want of media."""
        table = {1: {"scroll": False, "follow": False, "feed_posts": 0, "picture": False,
                     "bio": False, "reel": True, "notes": None}}
        plan = self._plan(warmup_plan=table)
        self.assertEqual(plan.plans, [])
        self.assertIn("left to the Posting Queue", plan.skipped[0].reason)

    def test_selected_launch_ids_restrict_the_run(self):
        plan = self._plan(selected_launch_ids={"some-other-id"})
        self.assertEqual(plan.plans, [])


class StampStartedTest(TestCase):
    class _Airtable:
        def __init__(self, ok=True):
            self.ok, self.written = ok, []

        def set_warmup_started(self, record_id, day_iso):
            self.written.append((record_id, day_iso))
            return self.ok

    def test_day_1_profiles_are_stamped_with_todays_date(self):
        plan = plan_profile_warmup([_mlx()], {"254765": _row("recP1")}, today=TODAY)
        client = self._Airtable()
        self.assertEqual(stamp_started(client, plan, today=TODAY), 1)
        self.assertEqual(client.written, [("recP1", "2026-08-06")])

    def test_a_profile_already_under_way_is_not_restamped(self):
        """The start date is written once and never moved -- rewriting it would
        hold a profile on day 1 forever."""
        plan = plan_profile_warmup([_mlx()], {"254765": _row("recP1", started="2026-08-04")},
                                   today=TODAY)
        client = self._Airtable()
        self.assertEqual(stamp_started(client, plan, today=TODAY), 0)
        self.assertEqual(client.written, [])

    def test_a_failed_write_is_counted_as_not_stamped(self):
        plan = plan_profile_warmup([_mlx()], {"254765": _row("recP1")}, today=TODAY)
        self.assertEqual(stamp_started(self._Airtable(ok=False), plan, today=TODAY), 0)


class TagsReachTheNormalizerTest(TestCase):
    def test_tags_survive_normalization(self):
        from adb_bot.automation.mlx_sync import normalize_mlx_item
        profile = normalize_mlx_item(_mlx(tags=("Created", "Second Account")))
        self.assertEqual(profile.tags, ("Created", "Second Account"))

    def test_a_profile_without_tags_normalizes_to_empty(self):
        from adb_bot.automation.mlx_sync import normalize_mlx_item
        item = _mlx()
        del item["tags"]
        self.assertEqual(normalize_mlx_item(item).tags, ())

    def test_the_default_tag_is_the_one_the_workspace_uses(self):
        self.assertEqual(WARMUP_TAG, "Created")
        self.assertIs(warmup_targets.WARMUP_TAG, WARMUP_TAG)
