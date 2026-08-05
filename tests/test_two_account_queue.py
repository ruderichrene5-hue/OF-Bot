"""Slot creation for a phone that carries two Instagram accounts.

One MLX profile is one cloud phone launched once, so both accounts share a
`profile_id` and a `launch_id` and differ only in the handle the reel flow
switches to. Two things have to hold: each account gets its own row for a slot
(that is the whole point -- twice the posts off one phone), and the two never
receive the same clip.
"""

import logging
from datetime import datetime
from unittest import TestCase
from zoneinfo import ZoneInfo

from adb_bot.automation.queue_runner import run_queue_slots
from adb_bot.clients import airtable as at

from tests.test_queue_runner import FakeQueueClient, _queue_row, _variant

LOG = logging.getLogger("test")
BERLIN = ZoneInfo("Europe/Berlin")


def _now(hour, minute=0, day=3):
    return datetime(2026, 8, day, hour, minute, tzinfo=BERLIN)


def _two_account_client(variants=None, queue_rows=None, **kwargs):
    profiles = {"jasmin": [
        {"profile_id": "p1", "handle": "Jasmin 5", "launch_id": "555",
         "ig_handle": "jasmindiecoolee", "slot": at.SLOT_PRIMARY},
        {"profile_id": "p1", "handle": "Jasmin 5 (naughty_jasminn)", "launch_id": "555",
         "ig_handle": "naughty_jasminn", "slot": at.SLOT_SECOND},
    ]}
    return FakeQueueClient(profiles=profiles, variants=variants,
                           queue_rows=queue_rows, **kwargs)


def _one_account_client(variants=None, queue_rows=None, **kwargs):
    profiles = {"luisa": [{"profile_id": "p2", "handle": "Luisa 9", "launch_id": "777",
                           "ig_handle": "", "slot": ""}]}
    return FakeQueueClient(profiles=profiles, variants=variants,
                           queue_rows=queue_rows, **kwargs)


class TwoAccountPhoneTest(TestCase):
    def test_each_account_gets_its_own_row_for_the_same_slot(self):
        client = _two_account_client(variants=[_variant("v1", profile_id="p1"),
                                               _variant("v2", profile_id="p1")])
        report = run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        self.assertEqual(report.slots_due, 1)      # only 09:00 has come round
        self.assertEqual(report.rows_created, 2)   # ...but two accounts want it
        self.assertEqual(sorted(row["ig_handle"] for row in client.created),
                         ["jasmindiecoolee", "naughty_jasminn"])
        self.assertEqual(sorted(row["account_slot"] for row in client.created),
                         [at.SLOT_PRIMARY, at.SLOT_SECOND])
        self.assertEqual({row["profile_id"] for row in client.created}, {"p1"})

    def test_the_two_accounts_never_get_the_same_clip(self):
        """Both draw from the profile's single variant pool, so a clip is popped
        once. The same video on both accounts of one model in one slot is the
        most obviously automated thing this could do."""
        client = _two_account_client(variants=[_variant("v1", profile_id="p1"),
                                               _variant("v2", profile_id="p1")])
        run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        used = [row["variant_id"] for row in client.created]
        self.assertEqual(sorted(used), ["v1", "v2"])

    def test_one_spare_clip_serves_only_one_of_the_two_accounts(self):
        client = _two_account_client(variants=[_variant("v1", profile_id="p1")])
        report = run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        self.assertEqual(report.rows_created, 1)
        self.assertTrue(any("no unused Ready Spoof Variant" in reason
                            for _name, reason in report.skipped))

    def test_a_row_for_one_account_does_not_fill_the_others_slot(self):
        """The guard the whole feature rests on: 09:00 is served once per
        account, not once per phone."""
        existing = _queue_row("q1", at.POST_STATUS_POSTED, "2026-08-03T07:00:00+00:00",
                              variant_id="v0", profile_id="p1", name="Jasmin 5 / 09:00")
        existing["fields"][at.F_PQ_IG_HANDLE] = "jasmindiecoolee"
        client = _two_account_client(variants=[_variant("v1", profile_id="p1")],
                                     queue_rows=[existing])
        report = run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        self.assertEqual(report.rows_created, 1)
        self.assertEqual(client.created[0]["ig_handle"], "naughty_jasminn")

    def test_rerunning_creates_nothing_once_both_accounts_are_served(self):
        rows = []
        for handle, name in (("jasmindiecoolee", "Jasmin 5 / 09:00"),
                             ("naughty_jasminn", "Jasmin 5 (naughty_jasminn) / 09:00")):
            row = _queue_row(f"q-{handle}", at.POST_STATUS_PENDING,
                             "2026-08-03T07:00:00+00:00", profile_id="p1", name=name)
            row["fields"][at.F_PQ_IG_HANDLE] = handle
            rows.append(row)
        client = _two_account_client(variants=[_variant("v9", profile_id="p1")],
                                     queue_rows=rows)
        report = run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        self.assertEqual(report.rows_created, 0)

    def test_an_at_prefixed_handle_on_a_row_still_matches_its_target(self):
        """Someone typing "@name" into Airtable must not make the row stop
        counting as the one that filled the slot -- that would double-post."""
        existing = _queue_row("q1", at.POST_STATUS_POSTED, "2026-08-03T07:00:00+00:00",
                              variant_id="v0", profile_id="p1", name="Jasmin 5 / 09:00")
        existing["fields"][at.F_PQ_IG_HANDLE] = "@JasminDiecoolee"
        client = _two_account_client(variants=[_variant("v1", profile_id="p1")],
                                     queue_rows=[existing])
        report = run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        self.assertEqual(report.rows_created, 1)
        self.assertEqual(client.created[0]["ig_handle"], "naughty_jasminn")


class SingleAccountUnchangedTest(TestCase):
    """The ~115 one-account phones must behave exactly as they did before."""

    def test_a_legacy_row_without_a_handle_still_holds_its_slot(self):
        """Rows written before the field existed carry no handle. If they stopped
        owning their slot, the first run after this change re-posts every one."""
        existing = _queue_row("q1", at.POST_STATUS_POSTED, "2026-08-03T07:00:00+00:00",
                              variant_id="v0", profile_id="p2", name="Luisa 9 / 09:00")
        client = _one_account_client(variants=[_variant("v1", profile_id="p2")],
                                     queue_rows=[existing])
        report = run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        self.assertEqual(report.rows_created, 0)

    def test_a_single_account_phone_writes_no_handle_at_all(self):
        client = _one_account_client(variants=[_variant("v1", profile_id="p2")])
        run_queue_slots(client, LOG, now=_now(9, 30), dry_run=False)

        self.assertEqual(client.created[0]["ig_handle"], "")
        self.assertEqual(client.created[0]["account_slot"], "")

    def test_one_account_phone_still_gets_exactly_one_row_per_slot(self):
        client = _one_account_client(variants=[_variant("v1", profile_id="p2"),
                                               _variant("v2", profile_id="p2"),
                                               _variant("v3", profile_id="p2")])
        report = run_queue_slots(client, LOG, now=_now(13), dry_run=False)

        self.assertEqual(report.slots_due, 3)
        self.assertEqual(report.rows_created, 3)
