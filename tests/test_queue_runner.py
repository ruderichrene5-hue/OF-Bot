import logging
from datetime import datetime
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


def _queue_row(rid, status, scheduled, variant_id=None, account_id=None, profile_id=None):
    fields = {at.F_PQ_POST_STATUS: status, at.F_PQ_SCHEDULED: scheduled}
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
        self.assertEqual([label for label, _ in due], ["09:00", "12:00"])

    def test_naive_now_is_read_as_local_wall_clock(self):
        # A naive `now` from datetime.now() must not silently become UTC, or the
        # 09:00 slot opens two hours late in summer.
        due = due_slots(datetime(2026, 8, 3, 13, 0), DEFAULT_SLOT_TIMES, BERLIN)
        self.assertEqual([label for label, _ in due], ["09:00", "12:00"])


class RunQueueSlotsTest(TestCase):
    def test_one_row_per_due_slot(self):
        client = _account_client(variants=[_variant("v1", account_id="acc1"),
                                           _variant("v2", account_id="acc1"),
                                           _variant("v3", account_id="acc1")])
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)

        self.assertEqual(report.slots_due, 2)          # 09:00 + 12:00
        self.assertEqual(report.rows_created, 2)
        self.assertEqual([row["scheduled"] for row in client.created],
                         ["2026-08-03T07:00:00+00:00", "2026-08-03T10:00:00+00:00"])
        # Every row is Pending, carries a variant, and no two share one.
        self.assertEqual([row["variant_id"] for row in client.created], ["v1", "v2"])
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
        self.assertIn("no unused Ready Spoof Variant", report.skipped[0][1])

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

    def test_already_filled_slot_is_not_duplicated(self):
        # A Posted row for the 09:00 slot means that slot has been served.
        client = _account_client(
            variants=[_variant("v1", account_id="acc1"), _variant("v2", account_id="acc1")],
            queue_rows=[_queue_row("pq1", at.POST_STATUS_POSTED, "2026-08-03T07:00:00+00:00",
                                   variant_id="v0", account_id="acc1")],
        )
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)
        self.assertEqual(report.rows_created, 1)
        self.assertEqual(client.created[0]["scheduled"], "2026-08-03T10:00:00+00:00")

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
        self.assertEqual(report.slots_due, 5)
        # Only two variants exist, so only two of the five slots can be filled --
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
