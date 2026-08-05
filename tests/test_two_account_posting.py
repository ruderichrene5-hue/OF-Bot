"""The handle's journey from a queue row to the phone, and the ledger split.

Two things decide whether two-account posting is safe rather than merely
possible:

1. the handle actually reaches the flow (a row that says "post as the second
   account" but arrives without the handle posts as the primary instead);
2. the post ledger stops identifying an account by phone alone, or the first
   account to post a clip permanently blocks the second from posting it.
"""

from datetime import datetime
from unittest import TestCase

from adb_bot.automation import post_ledger
from adb_bot.automation.posting_planner import plan_posting_queue
from adb_bot.automation.workflow import coerce_profile
from adb_bot.clients import airtable as at


def _row(rid, profile_id, ig_handle=None, variant="v1", scheduled="2026-08-03T07:00:00.000Z"):
    fields = {
        at.F_PQ_NAME: "Jasmin 5 / 09:00",
        at.F_PQ_POST_STATUS: at.POST_STATUS_PENDING,
        at.F_PQ_SCHEDULED: scheduled,
        at.F_PQ_TARGET_PROFILE: [profile_id],
        at.F_PQ_SPOOF_VARIANT: [variant],
    }
    if ig_handle is not None:
        fields[at.F_PQ_IG_HANDLE] = ig_handle
    return {"id": rid, "fields": fields}


PROFILES = {"p1": {"launch_id": "555", "name": "Jasmin 5"}}
VARIANTS = {"v1": {"file_path": "/out/v1.mp4", "status": at.SV_STATUS_READY},
            "v2": {"file_path": "/out/v2.mp4", "status": at.SV_STATUS_READY}}
NOW = datetime(2026, 8, 3, 12, 0)


class PlannerCarriesTheHandleTest(TestCase):
    def test_the_handle_reaches_the_posting_item(self):
        plan = plan_posting_queue([_row("q1", "p1", "naughty_jasminn")], {}, PROFILES,
                                  VARIANTS, {}, now=NOW)
        self.assertEqual(len(plan.to_post), 1)
        self.assertEqual(plan.to_post[0].ig_handle, "naughty_jasminn")

    def test_an_at_prefixed_handle_is_normalised_for_the_device(self):
        """The phone renders handles bare; a stored "@name" must not reach the
        switcher as "@name" and fail to match any row."""
        plan = plan_posting_queue([_row("q1", "p1", "@Naughty_Jasminn")], {}, PROFILES,
                                  VARIANTS, {}, now=NOW)
        self.assertEqual(plan.to_post[0].ig_handle, "naughty_jasminn")

    def test_a_single_account_row_carries_no_handle(self):
        plan = plan_posting_queue([_row("q1", "p1")], {}, PROFILES, VARIANTS, {}, now=NOW)
        self.assertEqual(plan.to_post[0].ig_handle, "")

    def test_the_handle_shows_up_in_the_name_used_for_logs_and_writeback(self):
        """Two rows for one phone are indistinguishable in a log otherwise."""
        plan = plan_posting_queue([_row("q1", "p1", "naughty_jasminn")], {}, PROFILES,
                                  VARIANTS, {}, now=NOW)
        self.assertIn("naughty_jasminn", plan.to_post[0].account_name)

    def test_two_rows_for_one_phone_are_both_planned(self):
        rows = [_row("q1", "p1", "jasmindiecoolee", variant="v1"),
                _row("q2", "p1", "naughty_jasminn", variant="v2")]
        plan = plan_posting_queue(rows, {}, PROFILES, VARIANTS, {}, now=NOW)

        self.assertEqual(len(plan.to_post), 2)
        self.assertEqual({i.ig_handle for i in plan.to_post},
                         {"jasmindiecoolee", "naughty_jasminn"})
        # One phone: both launch the same profile.
        self.assertEqual({i.launch_id for i in plan.to_post}, {"555"})


class ProfileCarriesTheHandleTest(TestCase):
    def test_coerce_profile_puts_the_handle_on_the_profile(self):
        profile = coerce_profile({"id": "555", "status": "active", "ip": "1.2.3.4",
                                  "port": "5555", "pwd": "x"}, "555",
                                 ig_handle="naughty_jasminn")
        self.assertEqual(profile.ig_handle, "naughty_jasminn")

    def test_a_profile_without_a_handle_defaults_to_none(self):
        profile = coerce_profile({"id": "555", "status": "active"}, "555")
        self.assertIsNone(profile.ig_handle)


class LedgerIsPerAccountTest(TestCase):
    """One MLX profile, two Instagram accounts -- the ledger must tell them apart."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self.tmp.close()
        self.ledger = post_ledger.PostLedger(self.tmp.name)

    def tearDown(self):
        import os
        os.unlink(self.tmp.name)

    def test_the_second_account_may_post_a_clip_the_first_already_posted(self):
        """They are different accounts. Keyed on the phone alone, the second one
        could never post anything the first had."""
        self.ledger.record_share("555", "/out/v1.mp4", media_hash="abc",
                                 ig_handle="jasmindiecoolee")

        blocked = self.ledger.lookup("555", "abc", "jasmindiecoolee")
        other = self.ledger.lookup("555", "abc", "naughty_jasminn")

        self.assertIsNotNone(blocked)
        self.assertTrue(blocked.blocks_repost())
        self.assertIsNone(other)

    def test_the_same_account_is_still_blocked_from_reposting(self):
        """The guard this whole ledger exists for must survive the split."""
        self.ledger.record_share("555", "/out/v1.mp4", media_hash="abc",
                                 ig_handle="naughty_jasminn")
        record = self.ledger.lookup("555", "abc", "naughty_jasminn")
        self.assertTrue(record.blocks_repost())

    def test_a_single_account_phone_keys_exactly_as_it_always_did(self):
        """Records written before handles existed must still be found, or the
        first run after this change re-posts everything they cover."""
        self.assertEqual(post_ledger.ledger_key("555", "abc"), "555:abc")
        self.assertEqual(post_ledger.ledger_key("555", "abc", ""), "555:abc")

        self.ledger.record_share("555", "/out/v1.mp4", media_hash="abc")
        self.assertIsNotNone(self.ledger.lookup("555", "abc"))

    def test_resolving_one_accounts_share_leaves_the_others_alone(self):
        self.ledger.record_share("555", "/out/v1.mp4", media_hash="abc",
                                 ig_handle="jasmindiecoolee")
        self.ledger.record_share("555", "/out/v1.mp4", media_hash="abc",
                                 ig_handle="naughty_jasminn")
        self.ledger.resolve("555", "abc", post_ledger.STATUS_DISPROVED, "not found",
                            ig_handle="jasmindiecoolee")

        first = self.ledger.lookup("555", "abc", "jasmindiecoolee")
        second = self.ledger.lookup("555", "abc", "naughty_jasminn")

        self.assertEqual(first.status, post_ledger.STATUS_DISPROVED)
        self.assertFalse(first.blocks_repost())
        self.assertEqual(second.status, post_ledger.STATUS_SHARED)
        self.assertTrue(second.blocks_repost())

    def test_the_recheck_can_still_relaunch_the_phone(self):
        """`profile_id` stays the real launch key -- the recheck pass hands it
        straight to the launcher, so a composite id there would break it."""
        self.ledger.record_share("555", "/out/v1.mp4", media_hash="abc",
                                 queue_id="q1", ig_handle="naughty_jasminn")
        entry = self.ledger.pending()[0]
        self.assertEqual(entry.profile_id, "555")
        self.assertEqual(entry.queue_id, "q1")
