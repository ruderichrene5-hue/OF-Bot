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
               name=None):
    fields = {at.F_PQ_POST_STATUS: status, at.F_PQ_SCHEDULED: scheduled}
    if name:
        fields[at.F_PQ_NAME] = name
    if variant_id:
        fields[at.F_PQ_SPOOF_VARIANT] = [variant_id]
    if account_id:
        fields[at.F_PQ_TARGET_ACCOUNT] = [account_id]
    if profile_id:
        fields[at.F_PQ_TARGET_PROFILE] = [profile_id]
    return {"id": rid, "fields": fields}


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
                             target_profile_id=None, name=None, caption_id=None):
        if self._create_fails:
            return None
        self.created.append({
            "scheduled": scheduled_iso, "variant_id": variant_id,
            "account_id": target_account_id, "profile_id": target_profile_id,
            "name": name, "caption_id": caption_id,
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
