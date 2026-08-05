"""A profile flagged for a human posts nothing until somebody clears the box.

The flag is a fact about the *phone*. When Instagram challenges an account it is
reacting to the device, so the second account on a two-account phone is in the
same trouble as the first even though nothing has failed for it yet -- and
posting from it while its twin sits flagged is how one warning becomes two.

Two places have to hold, because they cover different rows:

- `profile_targets_by_model` stops new queue rows and new spoof variants;
- `plan_posting_queue` stops rows that were already Pending when the flag landed,
  which the first check cannot reach.
"""

from datetime import datetime
from unittest import TestCase

from adb_bot.automation.posting_planner import plan_posting_queue
from adb_bot.clients import airtable as at
from adb_bot.clients.airtable import AirtableClient

NOW = datetime(2026, 8, 5, 12, 0)


# --- target building ------------------------------------------------------

def _profile_record(rec_id, name, launch_id="555", status="Active", needs_human=False,
                    has_second=False, primary="", second=""):
    return {"id": rec_id, "fields": {
        at.F_PROF_NAME: name,
        at.F_PROF_MLX_API_ID: launch_id,
        at.F_PROF_STATUS: status,
        at.F_PROF_NEEDS_HUMAN: needs_human,
        at.F_PROF_HAS_SECOND: has_second,
        at.F_PROF_PRIMARY_HANDLE: primary,
        at.F_PROF_SECOND_HANDLE: second,
    }}


class _StubClient(AirtableClient):
    """Real target-building logic, stubbed table read."""

    def __init__(self, records):
        self._records = records

    def _list_table(self, table, fields=None, **kwargs):
        return self._records


class TargetsTest(TestCase):
    def test_a_flagged_two_account_phone_offers_neither_account(self):
        """The point: one account's warning takes the other one off the air too."""
        client = _StubClient([_profile_record(
            "p1", "Jasmin 1", needs_human=True, has_second=True,
            primary="janabahdim", second="jsmin3075")])
        self.assertEqual(client.profile_targets_by_model(), {})

    def test_the_same_phone_offers_both_accounts_once_cleared(self):
        client = _StubClient([_profile_record(
            "p1", "Jasmin 1", needs_human=False, has_second=True,
            primary="janabahdim", second="jsmin3075")])
        targets = client.profile_targets_by_model()["jasmin"]
        self.assertEqual([t["ig_handle"] for t in targets], ["janabahdim", "jsmin3075"])

    def test_a_flagged_single_account_phone_is_dropped_too(self):
        client = _StubClient([_profile_record("p2", "Luisa 7", needs_human=True)])
        self.assertEqual(client.profile_targets_by_model(), {})

    def test_an_unflagged_phone_is_untouched(self):
        client = _StubClient([_profile_record("p3", "Luisa 9")])
        self.assertEqual(len(client.profile_targets_by_model()["luisa"]), 1)

    def test_status_and_the_flag_are_independent_switches(self):
        """A person parks with Status; the bot parks with Needs Human Check."""
        for status, flagged in (("Inactive", False), ("Active", True), ("Inactive", True)):
            client = _StubClient([_profile_record("p4", "Jil 4", status=status,
                                                  needs_human=flagged)])
            self.assertEqual(client.profile_targets_by_model(), {},
                             f"status={status} flagged={flagged} should not post")


# --- already-queued rows --------------------------------------------------

def _row(rid, profile_id, ig_handle="", variant="v1"):
    fields = {
        at.F_PQ_NAME: "Jasmin 1 / 09:00",
        at.F_PQ_POST_STATUS: at.POST_STATUS_PENDING,
        at.F_PQ_SCHEDULED: "2026-08-05T07:00:00.000Z",
        at.F_PQ_TARGET_PROFILE: [profile_id],
        at.F_PQ_SPOOF_VARIANT: [variant],
    }
    if ig_handle:
        fields[at.F_PQ_IG_HANDLE] = ig_handle
    return {"id": rid, "fields": fields}


VARIANTS = {"v1": {"file_path": "/out/v1.mp4", "status": at.SV_STATUS_READY},
            "v2": {"file_path": "/out/v2.mp4", "status": at.SV_STATUS_READY}}


class QueuedRowsTest(TestCase):
    def _profiles(self, needs_human, reason=None):
        return {"p1": {"launch_id": "555", "name": "Jasmin 1",
                       "needs_human": needs_human, "issue_reason": reason}}

    def test_a_pending_row_for_a_flagged_phone_does_not_post(self):
        """Rows queued before the flag landed are the ones that would slip
        through -- the target filter cannot reach them."""
        plan = plan_posting_queue(
            [_row("q1", "p1", "janabahdim")], {}, self._profiles(True, "Human Verification Required"),
            VARIANTS, {}, now=NOW)

        self.assertEqual(plan.to_post, [])
        self.assertEqual(len(plan.skipped), 1)
        self.assertIn("needs a human check", plan.skipped[0].reason)

    def test_the_skip_says_which_problem_so_the_log_is_actionable(self):
        plan = plan_posting_queue(
            [_row("q1", "p1")], {}, self._profiles(True, "Banned / Blocked"),
            VARIANTS, {}, now=NOW)
        self.assertIn("Banned / Blocked", plan.skipped[0].reason)

    def test_both_accounts_of_a_flagged_phone_are_stopped(self):
        rows = [_row("q1", "p1", "janabahdim", variant="v1"),
                _row("q2", "p1", "jsmin3075", variant="v2")]
        plan = plan_posting_queue(rows, {}, self._profiles(True, "Retries Exhausted"),
                                  VARIANTS, {}, now=NOW)

        self.assertEqual(plan.to_post, [])
        self.assertEqual(len(plan.skipped), 2)

    def test_an_unflagged_phone_still_posts_both(self):
        rows = [_row("q1", "p1", "janabahdim", variant="v1"),
                _row("q2", "p1", "jsmin3075", variant="v2")]
        plan = plan_posting_queue(rows, {}, self._profiles(False), VARIANTS, {}, now=NOW)

        self.assertEqual(len(plan.to_post), 2)
        self.assertEqual({i.ig_handle for i in plan.to_post}, {"janabahdim", "jsmin3075"})

    def test_a_profile_map_without_the_field_still_posts(self):
        """Back-compat: an older caller's dict has no `needs_human` key at all,
        and must not be read as "flagged" -- that would stop every post."""
        profiles = {"p1": {"launch_id": "555", "name": "Jasmin 1"}}
        plan = plan_posting_queue([_row("q1", "p1")], {}, profiles, VARIANTS, {}, now=NOW)
        self.assertEqual(len(plan.to_post), 1)

    def test_an_account_driven_row_is_blocked_by_its_phone_too(self):
        """The row links an Accounts record, but it still runs on that phone."""
        accounts = {"a1": {at.F_ACC_NAME: "nikki_1", at.F_ACC_PROFILE: ["p1"]}}
        row = _row("q1", "p1")
        row["fields"].pop(at.F_PQ_TARGET_PROFILE)
        row["fields"][at.F_PQ_TARGET_ACCOUNT] = ["a1"]

        plan = plan_posting_queue([row], accounts, self._profiles(True, "Banned / Blocked"),
                                  VARIANTS, {}, now=NOW)
        self.assertEqual(plan.to_post, [])
        self.assertIn("needs a human check", plan.skipped[0].reason)
