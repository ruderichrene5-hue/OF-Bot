import logging
from datetime import datetime, timezone
from unittest import TestCase
from zoneinfo import ZoneInfo

from adb_bot.automation import queue_runner
from adb_bot.automation.queue_runner import (
    DEFAULT_SLOT_TIMES, TARGET_ACCOUNT, TARGET_PROFILE, SlotTarget,
    due_slots, parse_slot_times, plan_slot_rows, run_queue_slots,
)
from adb_bot.clients import airtable as at

LOG = logging.getLogger("test")
BERLIN = ZoneInfo("Europe/Berlin")


def _now(hour, minute=0, day=3):
    """A wall-clock instant in the slot timezone (August -> Berlin is UTC+2)."""
    return datetime(2026, 8, day, hour, minute, tzinfo=BERLIN)


def _variant(vid, account_id=None, profile_id=None, status=at.SV_STATUS_READY, created="2026-08-01"):
    return {"id": vid, "file_path": f"/out/{vid}.mp4", "status": status,
            "account_id": account_id, "profile_id": profile_id, "created": created}


def _queue_row(rid, status, scheduled, variant_id=None, account_id=None, profile_id=None,
               name=None, created_time=None):
    fields = {at.F_PQ_POST_STATUS: status, at.F_PQ_SCHEDULED: scheduled}
    if name:
        fields[at.F_PQ_NAME] = name
    if variant_id:
        fields[at.F_PQ_SPOOF_VARIANT] = [variant_id]
    if account_id:
        fields[at.F_PQ_TARGET_ACCOUNT] = [account_id]
    if profile_id:
        fields[at.F_PQ_TARGET_PROFILE] = [profile_id]
    row = {"id": rid, "fields": fields}
    if created_time:
        # Airtable stamps every record with this; it is what tells the day guards
        # which day a row was made for, whatever the retry pass did to Scheduled.
        row["createdTime"] = created_time
    return row


class FakeQueueClient:
    """Stands in for AirtableClient: the four calls the slot runner makes."""

    def __init__(self, accounts=None, profiles=None, variants=None, queue_rows=None,
                 create_fails=False):
        self._accounts = accounts or {}       # model -> [{'account_id', 'handle'}]
        self._profiles = profiles or {}       # model -> [{'profile_id', 'handle', 'launch_id'}]
        self._variants = variants or []
        self._queue_rows = queue_rows or []
        self._create_fails = create_fails
        self.created = []

    def active_accounts_by_model(self):
        return self._accounts

    def profile_targets_by_model(self):
        return self._profiles

    def list_ready_variants(self):
        return list(self._variants)

    def list_queue_rows(self, statuses=None):
        return list(self._queue_rows)

    def create_posting_queue(self, scheduled_iso, variant_id, target_account_id=None,
                             target_profile_id=None, name=None, caption_id=None,
                             target_handle=None, account_slot=None):
        if self._create_fails:
            return None
        self.created.append({
            "scheduled": scheduled_iso, "variant_id": variant_id,
            "account_id": target_account_id, "profile_id": target_profile_id,
            "name": name, "caption_id": caption_id,
            "target_handle": target_handle, "account_slot": account_slot,
        })
        return f"recPQ{len(self.created)}"


def _account_client(handles=("nikki_1",), variants=None, queue_rows=None, **kwargs):
    accounts = {"nikki": [{"account_id": f"acc{i + 1}", "handle": h} for i, h in enumerate(handles)]}
    return FakeQueueClient(accounts=accounts, variants=variants, queue_rows=queue_rows, **kwargs)


class SlotTimeTest(TestCase):
    def test_parse_slot_times_sorts_and_drops_junk(self):
        parsed = parse_slot_times(["21:00", "09:00", "", "not-a-time", "12:00"])
        self.assertEqual([t.strftime("%H:%M") for t in parsed], ["09:00", "12:00", "21:00"])

    def test_only_slots_that_have_arrived_are_due(self):
        due = due_slots(_now(13), DEFAULT_SLOT_TIMES, BERLIN)
        self.assertEqual([label for label, _ in due], ["09:00", "11:00", "13:00"])

    def test_naive_now_is_read_as_local_wall_clock(self):
        # A naive `now` from datetime.now() must not silently become UTC, or the
        # 09:00 slot opens two hours late in summer.
        due = due_slots(datetime(2026, 8, 3, 13, 0), DEFAULT_SLOT_TIMES, BERLIN)
        self.assertEqual([label for label, _ in due], ["09:00", "11:00", "13:00"])


class RunQueueSlotsTest(TestCase):
    def test_one_row_per_due_slot(self):
        client = _account_client(variants=[_variant("v1", account_id="acc1"),
                                           _variant("v2", account_id="acc1"),
                                           _variant("v3", account_id="acc1")])
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)

        self.assertEqual(report.slots_due, 3)          # 09:00 + 11:00 + 13:00
        self.assertEqual(report.rows_created, 3)
        # Berlin is UTC+2 in August, so the local slots land two hours earlier.
        self.assertEqual([row["scheduled"] for row in client.created],
                         ["2026-08-03T07:00:00+00:00", "2026-08-03T09:00:00+00:00",
                          "2026-08-03T11:00:00+00:00"])
        # Every row is Pending, carries a variant, and no two share one.
        self.assertEqual([row["variant_id"] for row in client.created], ["v1", "v2", "v3"])
        self.assertEqual(report.errors, [])

    def test_dry_run_writes_nothing(self):
        client = _account_client(variants=[_variant("v1", account_id="acc1"),
                                           _variant("v2", account_id="acc1")])
        report = run_queue_slots(client, LOG, now=_now(13))          # dry_run defaults True
        self.assertTrue(report.dry_run)
        self.assertEqual(report.rows_created, 2)
        self.assertEqual(client.created, [])
        self.assertIn("DRY-RUN", report.summary())

    def test_future_slots_are_not_created(self):
        # 08:00: nothing has come round yet. Creating the whole day up front
        # would make all five rows due immediately for the posting loop.
        client = _account_client(variants=[_variant("v1", account_id="acc1")])
        report = run_queue_slots(client, LOG, now=_now(8), dry_run=False)
        self.assertEqual(report.slots_due, 0)
        self.assertEqual(report.rows_created, 0)
        self.assertEqual(client.created, [])

    def test_variant_on_a_pending_row_is_not_requeued(self):
        """The core correctness property: a variant already sitting on a Pending
        row must not be handed to a second row -- that is the same clip posted
        twice on the same account."""
        client = _account_client(
            variants=[_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")],
            queue_rows=[_queue_row("pq1", at.POST_STATUS_PENDING, "2026-08-02T07:00:00.000Z",
                                   variant_id="v1", account_id="acc1")],
        )
        report = run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        self.assertEqual(report.rows_created, 1)                     # only the 09:00 slot
        self.assertEqual([row["variant_id"] for row in client.created], ["v2"])

    def test_variant_on_a_verifying_row_is_not_requeued(self):
        # Verifying = sent but unproven. The recheck may still turn it into
        # Posted, so the variant is not free.
        client = _account_client(
            variants=[_variant("v1", account_id="acc1")],
            queue_rows=[_queue_row("pq1", at.POST_STATUS_VERIFYING, "2026-08-02T07:00:00.000Z",
                                   variant_id="v1", account_id="acc1")],
        )
        report = run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        self.assertEqual(client.created, [])
        # The reason names the holder: "no media" and "media owned by a live
        # row" call for opposite responses, so they must not read alike.
        self.assertIn("belong to an existing", report.skipped[0][1])
        self.assertIn(at.POST_STATUS_VERIFYING, report.skipped[0][1])

    def test_used_variant_is_not_queued(self):
        # A Used variant has already been posted. list_ready_variants filters
        # these out server-side; the planner re-checks so it is correct on any input.
        client = _account_client(variants=[_variant("v1", account_id="acc1", status=at.SV_STATUS_USED)])
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)
        self.assertEqual(client.created, [])
        self.assertEqual(report.rows_created, 0)
        self.assertEqual(len(report.skipped), 1)

    def test_account_target_writes_the_account_link_only(self):
        client = _account_client(variants=[_variant("v1", account_id="acc1")])
        run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        row = client.created[0]
        self.assertEqual(row["account_id"], "acc1")
        self.assertIsNone(row["profile_id"])          # never both
        self.assertEqual(row["name"], "nikki_1 / 09:00")
        self.assertIsNone(row["caption_id"])          # captions are optional, left unset

    def test_profile_target_writes_the_profile_link_only(self):
        client = FakeQueueClient(
            accounts={},   # no Accounts rows exist for this model
            profiles={"nikki": [{"profile_id": "p1", "handle": "Nikki 1", "launch_id": "111"}]},
            variants=[_variant("v1", profile_id="p1")],
        )
        run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        row = client.created[0]
        self.assertEqual(row["profile_id"], "p1")
        self.assertIsNone(row["account_id"])
        self.assertEqual(row["variant_id"], "v1")

    def test_target_with_no_ready_variant_is_skipped_with_reason(self):
        client = _account_client(variants=[])
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)
        self.assertEqual(report.rows_created, 0)
        self.assertEqual(report.skipped[0][0], "nikki_1")
        self.assertIn("no unused Ready Spoof Variant", report.skipped[0][1])
        # One skip for the target, not one per due slot.
        self.assertEqual(len(report.skipped), 1)

    def test_requeued_row_still_owns_its_slot(self):
        """A retried row keeps its slot even though its timestamp moved.

        The retry pass re-queues a failed row at now+backoff, so its Scheduled
        DateTime no longer matches the slot it was created for. Keyed only on
        that timestamp this loop saw the slot as free and created a SECOND row
        with a different variant -- one failed post became two live posts. Six
        of these were created against the live base on 2026-08-04.
        """
        client = _account_client(
            variants=[_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")],
            # Created for the 09:00 slot, then requeued to 13:20 by the retry pass.
            queue_rows=[_queue_row("pq1", at.POST_STATUS_PENDING, "2026-08-03T11:20:00+00:00",
                                   variant_id="v0", account_id="acc1",
                                   name="nikki_1 / 09:00")],
        )
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)

        # 09:00 is still owned by the requeued row; only 11:00 and 13:00 are open.
        self.assertEqual([row["scheduled"] for row in client.created],
                         ["2026-08-03T09:00:00+00:00", "2026-08-03T11:00:00+00:00"])

    def test_unnamed_row_still_guards_by_timestamp(self):
        """A hand-made row with no slot label falls back to the old guard."""
        client = _account_client(
            variants=[_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")],
            queue_rows=[_queue_row("pq1", at.POST_STATUS_POSTED, "2026-08-03T07:00:00+00:00",
                                   variant_id="v0", account_id="acc1")],
        )
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)
        self.assertEqual(client.created[0]["scheduled"], "2026-08-03T09:00:00+00:00")

    def test_already_filled_slot_is_not_duplicated(self):
        # A Posted row for the 09:00 slot means that slot has been served.
        client = _account_client(
            variants=[_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")],
            queue_rows=[_queue_row("pq1", at.POST_STATUS_POSTED, "2026-08-03T07:00:00+00:00",
                                   variant_id="v0", account_id="acc1")],
        )
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)
        # 09:00 is served, so only 11:00 and 13:00 are open -- and exactly two
        # variants exist to fill them.
        self.assertEqual(report.rows_created, 2)
        self.assertEqual(client.created[0]["scheduled"], "2026-08-03T09:00:00+00:00")

    def test_rerunning_the_same_slot_creates_nothing_new(self):
        """Idempotence: the loop runs every few minutes, so a second pass over an
        already-filled slot must be a no-op rather than a second post."""
        client = _account_client(variants=[_variant("v1", account_id="acc1"),
                                           _variant("v2", account_id="acc1")])
        run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        self.assertEqual(len(client.created), 1)
        first = client.created[0]
        client._queue_rows.append(_queue_row("pq1", at.POST_STATUS_PENDING, first["scheduled"],
                                             variant_id=first["variant_id"], account_id="acc1"))
        report = run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        self.assertEqual(report.rows_created, 0)
        self.assertEqual(len(client.created), 1)

    def test_variants_for_an_ineligible_target_are_left_alone(self):
        # active_accounts_by_model() already applied the posting guards, so an
        # account missing from it is paused/banned/needs-verification: its
        # variants must not be queued for anyone.
        client = FakeQueueClient(accounts={}, variants=[_variant("v1", account_id="accPaused")])
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)
        self.assertEqual(client.created, [])
        self.assertIn("not an eligible target", report.skipped[0][1])

    def test_each_target_gets_its_own_variants(self):
        client = _account_client(handles=("nikki_1", "nikki_2"),
                                 variants=[_variant("v1", account_id="acc1"),
                                           _variant("v2", account_id="acc2")])
        run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        self.assertEqual([(row["account_id"], row["variant_id"]) for row in client.created],
                         [("acc1", "v1"), ("acc2", "v2")])

    def test_oldest_variant_is_queued_first(self):
        client = _account_client(variants=[_variant("vNew", account_id="acc1", created="2026-08-02"),
                                           _variant("vOld", account_id="acc1", created="2026-07-01")])
        run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        self.assertEqual(client.created[0]["variant_id"], "vOld")

    def test_create_failure_is_reported_not_counted(self):
        client = _account_client(variants=[_variant("v1", account_id="acc1")], create_fails=True)
        report = run_queue_slots(client, LOG, now=_now(10), dry_run=False)
        self.assertEqual(report.rows_created, 0)
        self.assertEqual(len(report.errors), 1)

    def test_airtable_read_failure_is_an_error_not_a_crash(self):
        class Boom(FakeQueueClient):
            def list_ready_variants(self):
                raise RuntimeError("429 rate limited")

        report = run_queue_slots(Boom(), LOG, now=_now(13), dry_run=False)
        self.assertEqual(report.rows_created, 0)
        self.assertEqual(report.errors[0][0], "<airtable>")


class PlanSlotRowsTest(TestCase):
    """The planner on its own -- no client, no I/O."""

    def test_a_variant_is_never_planned_into_two_slots(self):
        targets = [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1")]
        variants = [_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")]
        report = plan_slot_rows(targets, variants, [], now=_now(22), tz=BERLIN)
        self.assertEqual(report.slots_due, 7)
        # Only two variants exist, so only two of the seven slots can be filled --
        # and each gets a different one.
        used = [row.variant_id for row in report.planned]
        self.assertEqual(used, ["v1", "v2"])
        self.assertEqual(len(set(used)), len(used))

    def test_profile_link_wins_when_a_variant_carries_both(self):
        targets = [SlotTarget(TARGET_PROFILE, "p1", "Nikki 1")]
        variants = [_variant("v1", account_id="acc1", profile_id="p1")]
        report = plan_slot_rows(targets, variants, [], now=_now(10), tz=BERLIN)
        self.assertEqual(report.planned[0].target.kind, TARGET_PROFILE)

    def test_custom_slot_times(self):
        targets = [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1")]
        variants = [_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")]
        report = plan_slot_rows(targets, variants, [], now=_now(11), tz=BERLIN,
                                slot_times=["10:00", "10:30", "23:00"])
        self.assertEqual([row.slot for row in report.planned], ["10:00", "10:30"])

    def test_missing_timezone_falls_back_to_local(self):
        # A box without a tz database must still create slots, not fail the loop.
        self.assertIsNotNone(queue_runner._zone("Not/AZone", LOG))


class FailedRowOwnsItsVariantTest(TestCase):
    """Recovering a failed row belongs to the retry pass, not to this loop.

    Both act on the same clip: retry re-queues the original row after asking the
    ledger, while this loop would draw the same variant into a fresh slot. That
    produced two Pending rows for one clip -- the ledger still stopped the second
    post, so nothing went out twice, but it cost a phone launch and filed a
    Skipped row that reads like a fault. Observed in the 2026-08-03 dry-run,
    which offered new rows for Jil 1 and Katja 2, the two whose posts had failed.
    """

    def _variant(self, vid="recV1", profile="recProf1"):
        return _variant(vid, profile_id=profile)

    def _target(self):
        return queue_runner.SlotTarget(kind=queue_runner.TARGET_PROFILE,
                                       record_id="recProf1", name="Katja 2")

    def _row(self, status, vid="recV1"):
        return {"id": "recQ1", "fields": {at.F_PQ_POST_STATUS: status,
                                          at.F_PQ_SPOOF_VARIANT: [vid],
                                          at.F_PQ_TARGET_PROFILE: ["recProf1"],
                                          at.F_PQ_SCHEDULED: "2026-08-03T05:30:00.000Z"}}

    def _plan(self, rows):
        return queue_runner.plan_slot_rows(
            [self._target()], [self._variant()], rows,
            now=_now(22),
        )

    def test_a_failed_row_still_owns_its_variant(self):
        report = self._plan([self._row(at.POST_STATUS_FAILED)])
        self.assertEqual(report.planned, [])

    def test_the_skip_says_the_variant_is_owned_not_missing(self):
        """'no media' and 'media tied to a live row' need opposite responses."""
        report = self._plan([self._row(at.POST_STATUS_FAILED)])
        _name, reason = report.skipped[0]
        self.assertIn("belong to an existing", reason)
        self.assertIn(at.POST_STATUS_FAILED, reason)
        self.assertNotIn("no unused", reason)

    def test_a_target_with_genuinely_no_media_still_says_so(self):
        report = queue_runner.plan_slot_rows([self._target()], [], [], now=_now(22))
        _name, reason = report.skipped[0]
        self.assertIn("no unused Ready Spoof Variant", reason)

    def test_every_terminal_and_in_flight_status_holds(self):
        for status in (at.POST_STATUS_PENDING, at.POST_STATUS_VERIFYING,
                       at.POST_STATUS_POSTED, at.POST_STATUS_FAILED):
            with self.subTest(status=status):
                self.assertEqual(self._plan([self._row(status)]).planned, [])

    def test_a_fresh_variant_is_still_queued_alongside_a_failed_one(self):
        """Holding the failed row's clip must not freeze the target entirely --
        new media spoofed later is still eligible."""
        fresh = self._variant(vid="recV2")
        report = queue_runner.plan_slot_rows(
            [self._target()], [self._variant(), fresh], [self._row(at.POST_STATUS_FAILED)],
            now=_now(22),
        )
        self.assertTrue(report.planned)
        self.assertEqual({r.variant_id for r in report.planned}, {"recV2"})


class ModelPostTimesTest(TestCase):
    """`Models.Reel Post Times` decides a model's day: the picked times, or --
    when nothing is picked -- "whenever there is a video", inside two bounds."""

    def _targets(self):
        return [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1", "nikki"),
                SlotTarget(TARGET_ACCOUNT, "acc2", "jil_1", "jil")]

    def _variants(self):
        return [_variant("v1", account_id="acc1"), _variant("v2", account_id="acc2")]

    def _plan(self, schedules, queue_rows=(), now=None, **kwargs):
        return plan_slot_rows(self._targets(), self._variants(), list(queue_rows),
                              now=now or _now(13), tz=BERLIN, schedules=schedules, **kwargs)

    def _plan_nikki(self, schedule, queue_rows=()):
        """Only nikki is in play: jil is parked on a time that is still hours
        away, so anything planned here is a decision about nikki. 22:00 rather
        than 23:00 because 23:00 is the close of posting hours, not a time a
        model may post at -- see `test_a_pick_outside_posting_hours_is_dropped`."""
        return self._plan({"nikki": schedule, "jil": queue_runner.ModelSchedule(times=("22:00",))},
                          queue_rows=queue_rows)

    def test_each_model_posts_at_its_own_times(self):
        report = self._plan({"nikki": queue_runner.ModelSchedule(times=("10:00",)),
                             "jil": queue_runner.ModelSchedule(times=("12:00",))})
        self.assertEqual({(row.target.name, row.slot) for row in report.planned},
                         {("nikki_1", "10:00"), ("jil_1", "12:00")})

    def test_a_time_still_ahead_of_now_is_not_queued_early(self):
        """Same rule as the global grid: a row written now is due now, so a
        14:00 time must not be created at 13:00."""
        report = self._plan({"nikki": queue_runner.ModelSchedule(times=("14:00",)),
                             "jil": queue_runner.ModelSchedule(times=("14:00",))})
        self.assertEqual(report.planned, [])

    def test_a_model_with_no_times_posts_now(self):
        """The ask: a model nobody scheduled posts whenever a video is ready."""
        report = self._plan({"nikki": queue_runner.ModelSchedule(times=()),
                             "jil": queue_runner.ModelSchedule(times=("12:00",))})
        by_name = {row.target.name: row for row in report.planned}
        # Scheduled for now, so the posting loop takes it on its next tick.
        self.assertEqual(by_name["nikki_1"].scheduled, "2026-08-03T11:00:00+00:00")
        self.assertEqual(by_name["nikki_1"].slot, "13:00")
        self.assertEqual(report.flexible_targets, 1)

    def test_a_model_absent_from_the_base_is_flexible_not_silent(self):
        """No Models row for this key at all -- it must still post, or a model
        added to MLX before Airtable would quietly never post again."""
        report = self._plan({"jil": queue_runner.ModelSchedule(times=("12:00",))})
        self.assertIn("nikki_1", {row.target.name for row in report.planned})

    def test_a_recent_post_no_longer_holds_the_next_one_back(self):
        """The 120-min gap used to block this. It is a spacing preference now,
        not a bound: the clip is spoofed and today is when it goes out."""
        rows = [_queue_row("pq1", at.POST_STATUS_POSTED, "2026-08-03T10:30:00+00:00",
                           variant_id="v0", account_id="acc1", name="nikki_1 / 12:30",
                           created_time="2026-08-03T10:30:00.000Z")]
        report = self._plan_nikki(queue_runner.ModelSchedule(), queue_rows=rows)
        self.assertEqual([r.target.name for r in report.planned], ["nikki_1"])

    def test_a_new_row_lands_after_the_ones_already_scheduled(self):
        """A row scheduled for later today pushes the fan-out past it, so a
        second run adds to the end of the day instead of on top of it."""
        rows = [_queue_row("pq1", at.POST_STATUS_PENDING, "2026-08-03T16:00:00+00:00",
                           variant_id="v0", account_id="acc1", name="nikki_1 / 18:00",
                           created_time="2026-08-03T10:00:00.000Z")]
        report = self._plan_nikki(queue_runner.ModelSchedule(), queue_rows=rows)
        planned = [r for r in report.planned if r.target.name == "nikki_1"]
        self.assertEqual([r.scheduled for r in planned], ["2026-08-03T16:01:00+00:00"])

    def test_reels_per_day_no_longer_caps_the_day(self):
        """Two posts today and Reels Per Day = 2: it used to stop here. Every
        spoofed clip goes out the day it was spoofed, so the third one goes."""
        rows = [_queue_row(f"pq{i}", at.POST_STATUS_POSTED, f"2026-08-03T0{i}:00:00+00:00",
                           variant_id=f"v0{i}", account_id="acc1", name=f"nikki_1 / 0{i + 2}:00",
                           created_time=f"2026-08-03T0{i}:00:00.000Z")
                for i in (1, 2)]
        report = self._plan_nikki(queue_runner.ModelSchedule(per_day=2), queue_rows=rows)
        self.assertEqual([r.target.name for r in report.planned], ["nikki_1"])

    def test_the_anytime_max_escape_hatch_still_caps_when_asked(self):
        """Nobody passes --anytime-max now, but an operator who does gets the
        old ceiling back rather than a flag that quietly does nothing."""
        rows = [_queue_row(f"pq{i}", at.POST_STATUS_POSTED, f"2026-08-03T0{i}:00:00+00:00",
                           variant_id=f"v0{i}", account_id="acc1", name=f"nikki_1 / 0{i + 2}:00",
                           created_time=f"2026-08-03T0{i}:00:00.000Z")
                for i in (1, 2)]
        report = self._plan({"nikki": queue_runner.ModelSchedule(),
                             "jil": queue_runner.ModelSchedule(times=("22:00",))},
                            queue_rows=rows, anytime_max_per_day=2)
        self.assertEqual([r.target.name for r in report.planned], [])
        self.assertIn("ceiling of 2", dict(report.skipped)["nikki_1"])

    def test_yesterdays_posts_do_not_count_against_that_ceiling(self):
        rows = [_queue_row(f"pq{i}", at.POST_STATUS_POSTED, f"2026-08-02T0{i}:00:00+00:00",
                           variant_id=f"v0{i}", account_id="acc1", name=f"nikki_1 / 0{i + 2}:00",
                           created_time=f"2026-08-02T0{i}:00:00.000Z")
                for i in (1, 2)]
        report = self._plan({"nikki": queue_runner.ModelSchedule(),
                             "jil": queue_runner.ModelSchedule(times=("22:00",))},
                            queue_rows=rows, anytime_max_per_day=2)
        self.assertEqual([r.target.name for r in report.planned], ["nikki_1"])

    def test_a_flexible_model_with_nothing_spoofed_is_skipped_not_queued(self):
        report = plan_slot_rows(self._targets(), [], [], now=_now(13), tz=BERLIN,
                                schedules={"nikki": queue_runner.ModelSchedule()})
        self.assertEqual(report.planned, [])
        self.assertIn("no unused Ready Spoof Variant", dict(report.skipped)["nikki_1"])

    def test_every_ready_clip_gets_a_row_the_same_day(self):
        """Two Ready variants, one run: both go out today. This is the rule --
        two clips or nine, the day they are spoofed is the day they post."""
        report = plan_slot_rows(
            [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1", "nikki")],
            [_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")],
            [], now=_now(13), tz=BERLIN, schedules={"nikki": queue_runner.ModelSchedule()})
        self.assertEqual([r.scheduled for r in report.planned],
                         # 13:00 Berlin now, window closes 22:45: two hours apart
                         # because the preferred gap fits.
                         ["2026-08-03T11:00:00+00:00", "2026-08-03T13:00:00+00:00"])

    def test_nine_clips_late_in_the_day_still_all_go_out_today(self):
        """The spacing gives way, not the same-day rule: at 20:00 there is no
        room for 2-hour gaps, so nine clips pack into what is left."""
        report = plan_slot_rows(
            [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1", "nikki")],
            [_variant(f"v{i}", account_id="acc1") for i in range(9)],
            [], now=_now(20), tz=BERLIN, schedules={"nikki": queue_runner.ModelSchedule()})
        self.assertEqual(len(report.planned), 9)
        stamps = [r.scheduled for r in report.planned]
        self.assertEqual(stamps[0], "2026-08-03T18:00:00+00:00")   # 20:00 Berlin
        self.assertEqual(stamps[-1], "2026-08-03T20:45:00+00:00")  # 22:45 Berlin

    def test_nothing_is_scheduled_once_the_window_has_closed(self):
        """23:00 Berlin: the day is over, so these wait for the morning rather
        than being posted at midnight to nobody."""
        report = plan_slot_rows(
            [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1", "nikki")],
            [_variant("v1", account_id="acc1")],
            [], now=_now(23), tz=BERLIN, schedules={"nikki": queue_runner.ModelSchedule()})
        self.assertEqual(report.planned, [])
        self.assertIn("posting window is over", dict(report.skipped)["nikki_1"])

    def test_the_first_row_of_the_morning_waits_for_the_window_to_open(self):
        report = plan_slot_rows(
            [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1", "nikki")],
            [_variant("v1", account_id="acc1")],
            [], now=_now(7), tz=BERLIN, schedules={"nikki": queue_runner.ModelSchedule()})
        self.assertEqual([r.scheduled for r in report.planned],
                         ["2026-08-03T07:00:00+00:00"])   # 09:00 Berlin

    def test_a_pick_outside_posting_hours_is_dropped(self):
        """A model that picked 02:00 does not post at 02:00. The clip is not
        lost -- it comes back as surplus and goes out inside the window."""
        report = plan_slot_rows(
            [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1", "nikki")],
            [_variant("v1", account_id="acc1")],
            [], now=_now(13), tz=BERLIN,
            schedules={"nikki": queue_runner.ModelSchedule(times=("02:00",))})
        self.assertEqual([r.scheduled for r in report.planned],
                         ["2026-08-03T11:00:00+00:00"])   # 13:00 Berlin, not 02:00

    def test_picked_times_are_anchors_not_a_ration(self):
        """Three picked times, nine clips: the model still clears its day. The
        due 09:00 slot is served as a slot, the surplus is fanned out."""
        report = plan_slot_rows(
            [SlotTarget(TARGET_ACCOUNT, "acc1", "nikki_1", "nikki")],
            [_variant(f"v{i}", account_id="acc1") for i in range(9)],
            [], now=_now(13), tz=BERLIN,
            schedules={"nikki": queue_runner.ModelSchedule(times=("09:00", "14:00", "19:00"))})
        # 09:00 came round, so it is served as a slot. 14:00 and 19:00 are still
        # ahead, so two clips are left for them and the other six are fanned out
        # now -- nine posts today between the three routes.
        self.assertEqual(len(report.planned), 7)
        self.assertEqual(report.planned[0].slot, "09:00")

    def test_no_schedules_at_all_keeps_the_global_grid(self):
        """A base with no Reel Post Times field must behave exactly as before --
        `schedules=None` is not "everyone is flexible"."""
        report = plan_slot_rows(self._targets(), self._variants(), [], now=_now(13),
                                tz=BERLIN, schedules=None, slot_times=("09:00",))
        self.assertEqual({row.slot for row in report.planned}, {"09:00"})
        self.assertEqual(report.flexible_targets, 0)


class ScheduleParsingTest(TestCase):
    def test_none_is_passed_through_as_none(self):
        self.assertIsNone(queue_runner.schedules_from_airtable(None))

    def test_times_and_cap_are_read_and_junk_dropped(self):
        out = queue_runner.schedules_from_airtable(
            {"Nikki": {"times": ["21:00", "nonsense", "09:00"], "per_day": "4"}})
        self.assertEqual(out["nikki"].times, ("09:00", "21:00"))
        self.assertEqual(out["nikki"].per_day, 4)

    def test_an_empty_pick_is_flexible(self):
        out = queue_runner.schedules_from_airtable({"jil": {"times": [], "per_day": None}})
        self.assertTrue(out["jil"].is_flexible)


class SlotGuardIsPerDayTest(TestCase):
    """The Name-based duplicate guard has to be scoped to the row's day.

    Nothing deletes yesterday's Posting Queue rows and the loop reads the table
    in full, so a guard keyed on the label alone ("nikki_1 / 09:00") let each
    target serve each slot exactly once, ever -- the day after a slot first
    filled, it looked served forever.
    """

    def _run(self, rows, now=_now(13)):
        client = _account_client(
            variants=[_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")],
            queue_rows=rows,
        )
        run_queue_slots(client, LOG, now=now, dry_run=False, slot_times=("09:00", "11:00"))
        return [row["scheduled"] for row in client.created]

    def test_yesterdays_row_does_not_block_todays_slot(self):
        yesterday = _queue_row("pq1", at.POST_STATUS_POSTED, "2026-08-02T07:00:00+00:00",
                               variant_id="v0", account_id="acc1", name="nikki_1 / 09:00",
                               created_time="2026-08-02T07:01:00.000Z")
        self.assertIn("2026-08-03T07:00:00+00:00", self._run([yesterday]))

    def test_todays_retried_row_still_owns_its_slot(self):
        """The 2026-08-04 duplicate guard, still holding within the day: the
        retry pass moved this row's Scheduled DateTime off its slot."""
        retried = _queue_row("pq1", at.POST_STATUS_PENDING, "2026-08-03T11:20:00+00:00",
                             variant_id="v0", account_id="acc1", name="nikki_1 / 09:00",
                             created_time="2026-08-03T07:01:00.000Z")
        self.assertEqual(self._run([retried]), ["2026-08-03T09:00:00+00:00"])


class AirtableTimesReachTheRunnerTest(TestCase):
    """`run_queue_slots` must actually read the per-model times, not just accept
    them as an argument -- the wiring is the part that silently rots."""

    class _Client(FakeQueueClient):
        def __init__(self, schedules, **kwargs):
            super().__init__(**kwargs)
            self._schedules = schedules

        def reel_schedules_by_model(self):
            return self._schedules

    def _client(self, schedules):
        return self._Client(
            schedules,
            accounts={"nikki": [{"account_id": "acc1", "handle": "nikki_1"}]},
            variants=[_variant("v1", account_id="acc1")],
        )

    def test_the_models_own_times_are_used(self):
        client = self._client({"nikki": {"times": ["10:00"], "per_day": None}})
        run_queue_slots(client, LOG, now=_now(13), dry_run=False, slot_times=("09:00",))
        self.assertEqual([row["name"] for row in client.created], ["nikki_1 / 10:00"])

    def test_an_empty_pick_posts_now(self):
        client = self._client({"nikki": {"times": [], "per_day": None}})
        run_queue_slots(client, LOG, now=_now(13), dry_run=False, slot_times=("09:00",))
        self.assertEqual([row["scheduled"] for row in client.created],
                         ["2026-08-03T11:00:00+00:00"])

    def test_no_field_in_the_base_falls_back_to_the_grid(self):
        client = self._client(None)
        run_queue_slots(client, LOG, now=_now(13), dry_run=False, slot_times=("09:00",))
        self.assertEqual([row["name"] for row in client.created], ["nikki_1 / 09:00"])

    def test_model_times_can_be_turned_off_for_one_run(self):
        client = self._client({"nikki": {"times": ["10:00"], "per_day": None}})
        run_queue_slots(client, LOG, now=_now(13), dry_run=False, slot_times=("09:00",),
                        use_model_times=False)
        self.assertEqual([row["name"] for row in client.created], ["nikki_1 / 09:00"])
