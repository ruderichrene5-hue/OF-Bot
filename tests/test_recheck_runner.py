"""The deferred pass exists to answer the question the run could not.

The case that carries the design is `test_equal_count_is_a_real_failure`: during
a run, "the count did not move" means nothing, because the counter lags. Fifteen
minutes later it is proof. Everything else here is about not overreaching when
the evidence isn't there.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from adb_bot.automation import post_ledger, recheck_runner
from adb_bot.automation.flows.reel_verify import Count
from adb_bot.automation.post_ledger import PostLedger, STATUS_CONFIRMED, STATUS_DISPROVED
from adb_bot.automation.recheck_runner import (
    OUTCOME_ABANDONED,
    OUTCOME_FAILED,
    OUTCOME_POSTED,
    OUTCOME_UNKNOWN,
    decide_recheck,
)
from adb_bot.clients import airtable as at

FIFTEEN_MIN = 15 * 60


class DecideRecheckTest(TestCase):
    def test_higher_count_confirms(self):
        outcome, detail = decide_recheck(41, True, Count(42, True), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_POSTED)
        self.assertIn("41 -> 42", detail)

    def test_equal_count_is_a_real_failure(self):
        """The whole reason for waiting. In-run this would be meaningless -- the
        counter routinely lags a minute or more -- so the run could only ever say
        "don't know". After the delay it is evidence, and this is the one moment
        Failed can be said with confidence."""
        outcome, detail = decide_recheck(41, True, Count(41, True), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_FAILED)
        self.assertIn("did not land", detail)

    def test_no_baseline_never_guesses(self):
        outcome, _ = decide_recheck(-1, False, Count(42, True), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_UNKNOWN)

    def test_inexact_baseline_never_guesses(self):
        outcome, _ = decide_recheck(1200, False, Count(1200, True), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_UNKNOWN)

    def test_unreadable_current_count_is_unknown(self):
        outcome, _ = decide_recheck(41, True, None, FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_UNKNOWN)

    def test_rounded_current_count_is_unknown(self):
        outcome, detail = decide_recheck(41, True, Count(1200, False), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_UNKNOWN)
        self.assertIn("rounded", detail)

    def test_a_dropped_count_is_not_treated_as_failure(self):
        # Something was deleted, or we're looking at the wrong account. Either
        # way it is not evidence about this reel.
        outcome, _ = decide_recheck(41, True, Count(39, True), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_UNKNOWN)

    def test_a_very_old_share_is_abandoned(self):
        outcome, _ = decide_recheck(41, True, None, 30 * 3600)
        self.assertEqual(outcome, OUTCOME_ABANDONED)


class ApplyOutcomeTest(TestCase):
    def setUp(self):
        self.airtable = MagicMock()

    def test_posted_closes_the_row_and_consumes_the_variant(self):
        recheck_runner.apply_recheck_outcome(
            self.airtable, "q1", "a1", "A", OUTCOME_POSTED, "41 -> 42", variant_id="v1")
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_POSTED, at.ISSUE_NONE)
        self.airtable.mark_variant_used.assert_called_once_with("v1")

    def test_failed_becomes_retryable(self):
        """Only here does Needs Retry become correct: we now have positive
        evidence the reel is not on the account, which the in-run check could
        never establish."""
        recheck_runner.apply_recheck_outcome(
            self.airtable, "q1", "a1", "A", OUTCOME_FAILED, "still 41")
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_FAILED, at.ISSUE_NEEDS_RETRY)
        self.airtable.mark_variant_used.assert_not_called()

    def test_unknown_leaves_the_row_parked_for_another_pass(self):
        terminal = recheck_runner.apply_recheck_outcome(
            self.airtable, "q1", "a1", "A", OUTCOME_UNKNOWN, "could not read")
        self.assertFalse(terminal)
        self.airtable.mark_post_pending_verification.assert_called_once()
        self.airtable.mark_post_result.assert_not_called()

    def test_abandoned_is_not_marked_needs_retry(self):
        # We still don't know what happened, so pushing it back into the posting
        # queue would risk the double post all of this exists to prevent.
        recheck_runner.apply_recheck_outcome(
            self.airtable, "q1", "a1", "A", OUTCOME_ABANDONED, "gave up")
        _args, _kwargs = self.airtable.mark_post_result.call_args
        self.assertNotEqual(_args[2], at.ISSUE_NEEDS_RETRY)


class AgeFromRecheckStampTest(TestCase):
    """The fallback age used when there is no ledger entry to read.

    It decides whether a row is written off, so the two ways it can be wrong
    are: reading a missing value as "old" (writes off a live post), and getting
    the timezone wrong (writes off early, west of Greenwich).
    """

    def _age(self, raw, now):
        return recheck_runner._age_from_recheck_stamp({at.F_PQ_RECHECK_AFTER: raw}, now)

    def test_it_measures_from_the_stamp(self):
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(self._age("2026-08-12T09:00:00.000Z", now), 3 * 3600)

    def test_a_naive_stamp_is_read_as_utc(self):
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(self._age("2026-08-12T09:00:00", now), 3 * 3600)

    def test_a_missing_stamp_has_no_age_rather_than_a_huge_one(self):
        self.assertIsNone(recheck_runner._age_from_recheck_stamp({}, 4e9))
        self.assertIsNone(self._age("", 4e9))

    def test_an_unparseable_stamp_has_no_age(self):
        self.assertIsNone(self._age("soon", 4e9))

    def test_a_stamp_in_the_future_is_zero_not_negative(self):
        now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self._age("2026-08-12T18:00:00.000Z", now), 0.0)


class RecheckPassTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = PostLedger(root / "ledger.jsonl")
        self.clip = root / "clip.mp4"
        self.clip.write_bytes(b"reel bytes")
        self.airtable = MagicMock()
        self.airtable.list_posts_awaiting_recheck.return_value = [{
            "id": "q1",
            "fields": {
                at.F_PQ_NAME: "acct / reel",
                at.F_PQ_TARGET_ACCOUNT: ["a1"],
                at.F_PQ_SPOOF_VARIANT: ["v1"],
            },
        }]

    def _share(self, baseline=Count(41, True)):
        return self.ledger.record_share("p1", self.clip, queue_id="q1", baseline_count=baseline)

    def test_a_confirmed_recheck_resolves_the_ledger_too(self):
        self._share()
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(42, True), ledger=self.ledger)
        self.assertEqual(tally["posted"], 1)
        record = self.ledger.lookup("p1", post_ledger.media_fingerprint(self.clip))
        self.assertEqual(record.status, STATUS_CONFIRMED)
        # Still blocked -- a confirmed post is the strongest reason not to resend.
        self.assertTrue(self.ledger.already_shared("p1", self.clip))

    def test_a_failed_recheck_re_opens_the_clip(self):
        """The only path that clears the ledger, and it needs positive evidence
        of absence to get here."""
        self._share()
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(41, True), ledger=self.ledger)
        self.assertEqual(tally["failed"], 1)
        record = self.ledger.lookup("p1", post_ledger.media_fingerprint(self.clip))
        self.assertEqual(record.status, STATUS_DISPROVED)
        self.assertFalse(self.ledger.already_shared("p1", self.clip))

    def test_an_inconclusive_recheck_leaves_the_clip_blocked(self):
        self._share()
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: None, ledger=self.ledger)
        self.assertEqual(tally["unknown"], 1)
        self.assertTrue(self.ledger.already_shared("p1", self.clip),
                        "an unanswered question must not re-open the clip")

    def test_a_probe_that_raises_is_treated_as_unknown(self):
        self._share()

        def exploding(pid, fields):
            raise RuntimeError("device gone")

        tally = recheck_runner.recheck_pending_posts(
            self.airtable, exploding, ledger=self.ledger)
        self.assertEqual(tally["unknown"], 1)
        self.assertTrue(self.ledger.already_shared("p1", self.clip))

    def test_a_row_with_no_local_ledger_entry_is_left_alone(self):
        # Posted from another machine, or the ledger was pruned. We have no
        # baseline, so any verdict would be invented.
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(99, True), ledger=self.ledger)
        self.assertEqual(tally["checked"], 0)
        self.assertEqual(tally["unknown"], 1)
        self.airtable.mark_post_result.assert_not_called()

    def _stamp_recheck_after(self, value):
        """Put a `Recheck After` on the one row the fake Airtable returns."""
        rows = self.airtable.list_posts_awaiting_recheck.return_value
        rows[0]["fields"][at.F_PQ_RECHECK_AFTER] = value

    def test_a_ledgerless_row_still_inside_the_window_is_left_alone(self):
        """Not every missing entry is permanent -- another host may be mid-run,
        and writing the row off an hour in would libel a post that landed."""
        self._stamp_recheck_after("2026-08-10T12:00:00.000Z")
        one_hour_later = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc).timestamp()
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(99, True), ledger=self.ledger,
            now=lambda: one_hour_later)
        self.assertEqual(tally["unknown"], 1)
        self.assertEqual(tally["abandoned"], 0)
        self.airtable.mark_post_result.assert_not_called()

    def test_a_ledgerless_row_past_the_window_is_written_off(self):
        """The regression this exists for: `decide_recheck` gives up at 24h, but
        it reads the age off the ledger entry -- so a row with no entry could
        never reach it and parked in Verifying for good. Seven did, for two
        days, until the 2026-08-10 migration was found to have renumbered them.
        """
        self._stamp_recheck_after("2026-08-10T12:00:00.000Z")
        two_days_later = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc).timestamp()
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(99, True), ledger=self.ledger,
            now=lambda: two_days_later)
        self.assertEqual(tally["abandoned"], 1)
        self.assertEqual(tally["unknown"], 0)
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_FAILED, at.ISSUE_OTHER)

    def test_an_unreadable_stamp_leaves_a_ledgerless_row_alone(self):
        """No age means no grounds to write it off. Silence beats a guess."""
        self._stamp_recheck_after("not a date")
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(99, True), ledger=self.ledger,
            now=lambda: 4e9)
        self.assertEqual(tally["unknown"], 1)
        self.airtable.mark_post_result.assert_not_called()

    def test_an_airtable_outage_is_survivable(self):
        self.airtable.list_posts_awaiting_recheck.side_effect = RuntimeError("503")
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(42, True), ledger=self.ledger)
        self.assertEqual(tally["checked"], 0)


class ProfileDrivenRecheckTest(TestCase):
    """Rows that target a Profiles (Cloning) row instead of an Accounts row.

    These exist for models that have MLX phones but no Accounts rows. They used
    to be read, decided, and resolved in the local ledger while Airtable was
    never touched -- the row sat in `Verifying` forever and the two records
    disagreed permanently, which is worse than not having rechecked at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = PostLedger(root / "ledger.jsonl")
        self.clip = root / "clip.mp4"
        self.clip.write_bytes(b"reel bytes")
        self.airtable = MagicMock()
        self.airtable.profile_launch_map.return_value = {
            "prof1": {"name": "aria_clone", "launch_id": "1" * 18, "serial": "s1"},
        }

    def _queue_row(self, fields):
        self.airtable.list_posts_awaiting_recheck.return_value = [{"id": "q1", "fields": fields}]
        self.ledger.record_share("p1", self.clip, queue_id="q1", baseline_count=Count(41, True))

    def _run(self):
        return recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(42, True), ledger=self.ledger)

    def test_a_profile_driven_row_reaching_posted_is_written_back(self):
        self._queue_row({
            at.F_PQ_NAME: "aria / reel",
            at.F_PQ_TARGET_PROFILE: ["prof1"],
            at.F_PQ_SPOOF_VARIANT: ["v1"],
        })
        tally = self._run()
        self.assertEqual(tally["posted"], 1)
        # The row must leave Verifying, or Airtable and the ledger disagree.
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_POSTED, at.ISSUE_NONE)
        self.airtable.mark_variant_used.assert_called_once_with("v1")
        self.airtable.create_run_log.assert_called_once()
        self.assertFalse(self.airtable.create_run_log.call_args[0][0],
                         "there is no Accounts row to link")

    def test_a_profile_driven_row_is_logged_under_the_profile_name(self):
        self._queue_row({
            at.F_PQ_NAME: "aria / reel",
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        })
        self._run()
        self.assertEqual(self.airtable.create_run_log.call_args[0][1], "aria_clone")
        # One lookup for the whole pass, never one per row.
        self.airtable.profile_launch_map.assert_called_once()

    def test_an_unresolvable_profile_name_falls_back_to_the_row_name(self):
        self.airtable.profile_launch_map.return_value = {}
        self._queue_row({
            at.F_PQ_NAME: "aria / reel",
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        })
        self._run()
        self.assertEqual(self.airtable.create_run_log.call_args[0][1], "aria / reel")

    def test_a_row_with_both_links_follows_the_account_path(self):
        """The Accounts row carries the health guards; taking the profile branch
        when both are set would quietly route around them."""
        self._queue_row({
            at.F_PQ_NAME: "acct / reel",
            at.F_PQ_TARGET_ACCOUNT: ["a1"],
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        })
        self._run()
        self.assertEqual(self.airtable.create_run_log.call_args[0][0], "a1")
        self.assertEqual(self.airtable.create_run_log.call_args[0][1], "acct / reel")
        self.airtable.set_account_result.assert_called_once()
        self.assertEqual(self.airtable.set_account_result.call_args[0][0], "a1")
        self.airtable.profile_launch_map.assert_not_called()

    def test_an_account_driven_row_is_unchanged(self):
        self._queue_row({
            at.F_PQ_NAME: "acct / reel",
            at.F_PQ_TARGET_ACCOUNT: ["a1"],
            at.F_PQ_SPOOF_VARIANT: ["v1"],
        })
        tally = self._run()
        self.assertEqual(tally["posted"], 1)
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_POSTED, at.ISSUE_NONE)
        self.assertEqual(self.airtable.create_run_log.call_args[0][0], "a1")
        self.assertEqual(self.airtable.set_account_result.call_args[0][0], "a1")
        self.airtable.profile_launch_map.assert_not_called()

    def test_a_failed_profile_driven_row_is_written_back_too(self):
        self._queue_row({
            at.F_PQ_NAME: "aria / reel",
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        })
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(41, True), ledger=self.ledger)
        self.assertEqual(tally["failed"], 1)
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_FAILED, at.ISSUE_NEEDS_RETRY)

    def test_an_inconclusive_profile_driven_row_is_re_parked(self):
        self._queue_row({
            at.F_PQ_NAME: "aria / reel",
            at.F_PQ_TARGET_PROFILE: ["prof1"],
        })
        recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: None, ledger=self.ledger)
        self.airtable.mark_post_pending_verification.assert_called_once()

    def test_a_row_with_neither_link_still_leaves_verifying(self):
        # Nothing to link the log to, but the queue row is still answerable.
        self._queue_row({at.F_PQ_NAME: "orphan / reel"})
        self._run()
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_POSTED, at.ISSUE_NONE)


class RunLogWithoutAnAccountTest(TestCase):
    """The write-back leans on the client tolerating a missing account link."""

    def _client(self):
        client = at.AirtableClient("tok", "appX", "Posting Queue")
        self.created = []

        def fake_create_in(table, fields):
            self.created.append((table, fields))
            return "rec1"

        client._create_in = fake_create_in
        return client

    def test_a_run_log_without_an_account_id_is_still_written(self):
        client = self._client()
        self.assertEqual(client.create_run_log(None, "aria_clone", "flow", at.RESULT_DONE), "rec1")
        _table, fields = self.created[0]
        self.assertNotIn(at.F_RUN_ACCOUNT, fields)
        self.assertIn("aria_clone", fields[at.F_RUN_NAME])

    def test_setting_an_account_result_without_an_account_id_no_ops(self):
        client = self._client()
        client._patch_in = lambda *a, **kw: self.fail("patched an account that does not exist")
        self.assertFalse(client.set_account_result(None, "done"))

    def test_apply_recheck_outcome_survives_a_missing_account(self):
        client = self._client()
        patched = []
        client._patch_in = lambda table, rid, fields: patched.append((table, rid, fields)) or True
        terminal = recheck_runner.apply_recheck_outcome(
            client, "q1", None, "aria_clone", OUTCOME_POSTED, "41 -> 42", variant_id="v1")
        self.assertTrue(terminal)
        self.assertIn(at.TABLE_POSTING_QUEUE, [t for t, _r, _f in patched])

    def test_the_recheck_query_asks_for_the_profile_link(self):
        """Without this field the profile branch can never fire in production --
        Airtable only returns the columns the query names."""
        client = at.AirtableClient("tok", "appX", "Posting Queue")
        seen = {}

        def fake_list_table(table, fields=None, filter_formula=None):
            seen["fields"] = fields or []
            return []

        client._list_table = fake_list_table
        client.list_posts_awaiting_recheck()
        self.assertIn(at.F_PQ_TARGET_PROFILE, seen["fields"])


class WiringTest(TestCase):
    """A recheck pass nobody runs is the same as no recheck pass -- rows would
    sit in Verifying forever, which is worse than the Failed they replaced."""

    def test_the_recheck_loop_is_runnable(self):
        from adb_bot.automation import run_loop
        self.assertIn("recheck", run_loop.LOOPS)
        self.assertIn("recheck", run_loop._DISPATCH)

    def test_the_probe_flow_is_registered(self):
        from adb_bot.automation.bootstrap import build_automation
        self.assertIn("reel_post_count_probe", build_automation().flows)

    def test_the_probe_flow_never_posts(self):
        """It subclasses the upload flow for its navigation helpers, so the one
        thing worth pinning is that none of the upload path survived."""
        import inspect
        from adb_bot.automation.flows.instagram_reel import ReelPostCountProbeFlow
        src = inspect.getsource(ReelPostCountProbeFlow.run)
        for forbidden in ("_tap_share_u2", "record_share", "_adb_push_media_to_device",
                          "commit_media_used"):
            self.assertNotIn(forbidden, src, f"the read-only probe references {forbidden}")

    def test_workflow_hands_the_raw_result_to_a_result_callback(self):
        """The status callback only carries a rendered string; the recheck needs
        the actual number back."""
        import inspect
        from adb_bot.automation import workflow
        src = inspect.getsource(workflow.run_profile_workflow)
        self.assertIn("result_callback", src)
        self.assertIn("result_callback(flow_result)", src)


class Share:
    """Just the ledger fields `confirm_from_later_share` reads."""

    def __init__(self, shared_at, baseline_count, handle="jiji.ll12",
                 profile_id="p1", exact=True):
        self.shared_at = shared_at
        self.baseline_count = baseline_count
        self.baseline_exact = exact
        self.target_handle = handle
        self.profile_id = profile_id


class ConfirmFromLaterShareTest(TestCase):
    """The next post's opening count as proof the previous one landed.

    Numbers come from Jil 5 on 2026-08-15, which sat on three rows in
    `Verifying` for a day while its own ledger read 106, 107, 109.
    """

    def test_the_next_posts_baseline_confirms_this_one(self):
        first = Share(1000.0, 106)
        shares = [first, Share(8000.0, 107), Share(15000.0, 109)]
        outcome, detail = recheck_runner.confirm_from_later_share(first, shares)
        self.assertEqual(outcome, OUTCOME_POSTED)
        self.assertIn("106", detail)
        self.assertIn("107", detail)

    def test_a_rise_of_more_than_one_still_confirms(self):
        second = Share(8000.0, 107)
        shares = [Share(1000.0, 106), second, Share(15000.0, 109)]
        outcome, _ = recheck_runner.confirm_from_later_share(second, shares)
        self.assertEqual(outcome, OUTCOME_POSTED)

    def test_the_most_recent_share_has_no_later_reading(self):
        last = Share(15000.0, 109)
        shares = [Share(1000.0, 106), Share(8000.0, 107), last]
        self.assertIsNone(recheck_runner.confirm_from_later_share(last, shares))

    def test_a_flat_count_proves_nothing_here(self):
        """Silence is `decide_recheck`'s call to make, not this one's."""
        first = Share(1000.0, 106)
        self.assertIsNone(
            recheck_runner.confirm_from_later_share(first, [first, Share(8000.0, 106)]))

    def test_a_dropped_count_is_never_a_confirmation(self):
        """Jil 6 went 77 -> 15: the phone was showing the other account."""
        first = Share(1000.0, 77)
        self.assertIsNone(
            recheck_runner.confirm_from_later_share(first, [first, Share(8000.0, 15)]))

    def test_another_handle_on_the_same_phone_is_not_evidence(self):
        first = Share(1000.0, 106, handle="jiji.ll12")
        shares = [first, Share(8000.0, 107, handle="helenaiscutee")]
        self.assertIsNone(recheck_runner.confirm_from_later_share(first, shares))

    def test_the_same_handle_on_another_profile_is_not_evidence(self):
        first = Share(1000.0, 106, profile_id="p1")
        shares = [first, Share(8000.0, 107, profile_id="p2")]
        self.assertIsNone(recheck_runner.confirm_from_later_share(first, shares))

    def test_an_unlabelled_count_on_a_two_account_phone_is_still_refused(self):
        """The property the old "no handle is never evidence" rule protected.

        An unlabelled count on a phone that holds two accounts could be either
        of them. What has changed is how that phone is recognised: not "this
        record has no handle" -- which is the *single*-account case and is how
        almost every profile-driven row is written -- but "some share on this
        profile does have one", which is the ledger saying there are two
        accounts here regardless of what the MLX tags claim.
        """
        first = Share(1000.0, 106, handle="")
        shares = [first,
                  Share(4000.0, 12, handle="helenaiscutee"),
                  Share(8000.0, 107, handle="")]
        self.assertIsNone(recheck_runner.confirm_from_later_share(first, shares))

    def test_a_rise_too_small_to_cover_every_share_confirms_nothing(self):
        """Two posts and a +1: one landed, and this does not say which."""
        first = Share(1000.0, 106)
        shares = [first, Share(2000.0, -1, exact=False), Share(8000.0, 107)]
        self.assertIsNone(recheck_runner.confirm_from_later_share(first, shares))

    def test_an_inexact_baseline_on_either_side_is_refused(self):
        first = Share(1000.0, 106, exact=False)
        self.assertIsNone(
            recheck_runner.confirm_from_later_share(first, [first, Share(8000.0, 107)]))
        exact_first = Share(1000.0, 106)
        self.assertIsNone(recheck_runner.confirm_from_later_share(
            exact_first, [exact_first, Share(8000.0, 107, exact=False)]))

    def test_handles_compare_case_and_at_insensitively(self):
        first = Share(1000.0, 106, handle="@Jiji.LL12 ")
        shares = [first, Share(8000.0, 107, handle="jiji.ll12")]
        outcome, _ = recheck_runner.confirm_from_later_share(first, shares)
        self.assertEqual(outcome, OUTCOME_POSTED)


class RetroConfirmInThePassTest(TestCase):
    """The pass must take the free answer and leave the phone alone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = PostLedger(root / "ledger.jsonl")
        self.first = root / "first.mp4"
        self.first.write_bytes(b"first reel")
        self.second = root / "second.mp4"
        self.second.write_bytes(b"second reel")
        self.airtable = MagicMock()
        self.airtable.list_posts_awaiting_recheck.return_value = [{
            "id": "q1",
            "fields": {
                at.F_PQ_NAME: "Jil 5 (jiji.ll12)",
                at.F_PQ_TARGET_ACCOUNT: ["a1"],
                at.F_PQ_SPOOF_VARIANT: ["v1"],
            },
        }]

    def test_a_later_share_resolves_the_row_without_a_device(self):
        self.ledger.record_share("p1", self.first, queue_id="q1",
                                 baseline_count=Count(106, True), target_handle="jiji.ll12")
        self.ledger.record_share("p1", self.second, queue_id="q2",
                                 baseline_count=Count(107, True), target_handle="jiji.ll12")

        def must_not_run(pid, fields):
            raise AssertionError("a phone was opened for a question already answered")

        tally = recheck_runner.recheck_pending_posts(
            self.airtable, must_not_run, ledger=self.ledger)
        self.assertEqual(tally["posted"], 1)
        self.assertEqual(tally["checked"], 0, "no device probe should have been counted")
        record = self.ledger.lookup("p1", post_ledger.media_fingerprint(self.first))
        self.assertEqual(record.status, STATUS_CONFIRMED)
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_POSTED, at.ISSUE_NONE)

    def test_without_a_later_share_the_probe_still_runs(self):
        self.ledger.record_share("p1", self.first, queue_id="q1",
                                 baseline_count=Count(106, True), target_handle="jiji.ll12")
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(107, True), ledger=self.ledger)
        self.assertEqual(tally["posted"], 1)
        self.assertEqual(tally["checked"], 1)


class AccountAbsentStopsTheRetryTest(TestCase):
    """A handle the phone does not carry is a terminal answer, not a retry.

    @jiji.ll12 and @helen_aiscooll cost 122 recheck launches in five days,
    every one of them re-proving the same thing.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = PostLedger(root / "ledger.jsonl")
        self.clip = root / "clip.mp4"
        self.clip.write_bytes(b"reel bytes")
        self.airtable = MagicMock()
        self.airtable.list_posts_awaiting_recheck.return_value = [{
            "id": "q1",
            "fields": {
                at.F_PQ_NAME: "Jil 5 (jiji.ll12)",
                at.F_PQ_TARGET_ACCOUNT: ["a1"],
                at.F_PQ_SPOOF_VARIANT: ["v1"],
            },
        }]
        self.ledger.record_share("p1", self.clip, queue_id="q1",
                                 baseline_count=Count(106, True), target_handle="jiji.ll12")

    def _absent(self, pid, fields):
        raise recheck_runner.AccountNotOnPhone("the switcher does not list @jiji.ll12")

    def test_the_row_leaves_verifying_with_its_own_issue_code(self):
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, self._absent, ledger=self.ledger)
        self.assertEqual(tally["account_absent"], 1)
        self.assertEqual(tally["unknown"], 0)
        self.airtable.mark_post_result.assert_called_once_with(
            "q1", at.POST_STATUS_FAILED, at.ISSUE_ACCOUNT_MISSING)
        self.airtable.mark_post_pending_verification.assert_not_called()

    def test_the_clip_stays_blocked_because_we_still_do_not_know(self):
        """Not disproved: giving up on the question is not answering it."""
        recheck_runner.recheck_pending_posts(
            self.airtable, self._absent, ledger=self.ledger)
        self.assertTrue(self.ledger.already_shared("p1", self.clip))
        record = self.ledger.lookup("p1", post_ledger.media_fingerprint(self.clip))
        self.assertNotEqual(record.status, STATUS_DISPROVED)

    def test_an_ordinary_probe_failure_is_still_only_unknown(self):
        def lost_the_screen(pid, fields):
            raise RuntimeError("uiautomator2 died")

        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lost_the_screen, ledger=self.ledger)
        self.assertEqual(tally["unknown"], 1)
        self.assertEqual(tally["account_absent"], 0)

    def test_the_watchdog_counts_it_as_produced(self):
        """Otherwise a pass that resolves only absent rows reads as a stall."""
        from adb_bot.automation import loop_watchdog
        verdict = loop_watchdog.observe_recheck(
            loop_watchdog.LoopWatchdog(),
            {"checked": 1, "posted": 0, "failed": 0, "unknown": 0,
             "abandoned": 0, "account_absent": 1})
        self.assertEqual(verdict.produced, 1)
        self.assertEqual(verdict.due, 1)
        self.assertFalse(verdict.stalled)


class SingleAccountCounterEvidenceTest(TestCase):
    """The counter evidence on a phone that holds one account.

    Posting is profile-driven, and the posting flow only sets `target_handle`
    when it has to switch accounts first -- so on a single-account phone the
    handle is empty by design. The old guard read that empty handle as "cannot
    tell the accounts apart" and returned None before looking at anything,
    which disabled the evidence for most of the fleet. Numbers are Emely 4's,
    whose ledger read 2 either side of a share that never landed.
    """

    def test_a_missing_handle_no_longer_refuses_the_evidence(self):
        first = Share(1000.0, 5, handle="", profile_id="emely4")
        shares = [first, Share(8000.0, 6, handle="", profile_id="emely4")]
        outcome, detail = recheck_runner.confirm_from_later_share(first, shares)
        self.assertEqual(outcome, OUTCOME_POSTED)
        self.assertIn("this phone", detail)

    def test_a_handle_on_any_share_means_two_accounts_and_refuses(self):
        """The MLX tags miss most two-account phones, so the ledger decides.

        `625727267523461165` carries clips for both `Nikki 1` and
        `kikittie22`; its two counters are unrelated and neither reading says
        anything about the other.
        """
        first = Share(1000.0, 77, handle="", profile_id="two")
        shares = [first,
                  Share(4000.0, 3, handle="kikittie22", profile_id="two"),
                  Share(8000.0, 78, handle="", profile_id="two")]
        self.assertIsNone(recheck_runner.confirm_from_later_share(first, shares))
        self.assertIsNone(recheck_runner.disprove_from_later_share(first, shares))

    def test_another_profiles_shares_are_not_this_phones_counter(self):
        first = Share(1000.0, 2, handle="", profile_id="emely4")
        shares = [first, Share(8000.0, 40, handle="", profile_id="emely5")]
        self.assertIsNone(recheck_runner.confirm_from_later_share(first, shares))


class DisproveFromLaterShareTest(TestCase):
    """Proving a share did NOT land -- the half that frees the clip.

    Confirming only stops a re-send, which the ledger already did by blocking.
    Disproving is what lets the row be retried, and without it a tapped-but-
    unproven share holds its variant for good.
    """

    def test_a_flat_count_disproves_the_share(self):
        first = Share(1000.0, 2, handle="", profile_id="emely4")
        shares = [first, Share(8000.0, 2, handle="", profile_id="emely4")]
        outcome, detail = recheck_runner.disprove_from_later_share(first, shares)
        self.assertEqual(outcome, OUTCOME_FAILED)
        self.assertIn("did not", detail)

    def test_a_rise_is_not_a_disproof(self):
        first = Share(1000.0, 2, handle="", profile_id="emely4")
        shares = [first, Share(8000.0, 3, handle="", profile_id="emely4")]
        self.assertIsNone(recheck_runner.disprove_from_later_share(first, shares))

    def test_a_decrease_proves_nothing_either_way(self):
        """`77 -> 1` is a misread of the screen, not a vanished post."""
        first = Share(1000.0, 77, handle="", profile_id="jil6")
        shares = [first, Share(8000.0, 1, handle="", profile_id="jil6")]
        self.assertIsNone(recheck_runner.disprove_from_later_share(first, shares))

    def test_two_shares_at_the_same_instant_are_left_alone(self):
        """Neither landed, but ruling on one row must not reason about a set.

        `nxt` is the earliest share after this one, so the only way two shares
        share a window is a tie on the timestamp -- two clips sent to one
        account in the same moment, which the counter cannot separate.
        """
        first = Share(1000.0, 2, handle="", profile_id="emely4")
        shares = [first,
                  Share(1000.0, 2, handle="", profile_id="emely4"),
                  Share(8000.0, 2, handle="", profile_id="emely4")]
        self.assertIsNone(recheck_runner.disprove_from_later_share(first, shares))

    def test_a_later_flat_reading_disproves_each_of_a_run_of_shares(self):
        """Three flat readings in a row: each pair proves its own share dead.

        This is the Nikki 28 shape -- post count 2 across every attempt.
        """
        a = Share(1000.0, 2, handle="", profile_id="nikki28")
        b = Share(4000.0, 2, handle="", profile_id="nikki28")
        shares = [a, b, Share(8000.0, 2, handle="", profile_id="nikki28")]
        for share in (a, b):
            outcome, _ = recheck_runner.disprove_from_later_share(share, shares)
            self.assertEqual(outcome, OUTCOME_FAILED)

    def test_the_most_recent_share_has_nothing_to_compare_against(self):
        last = Share(9000.0, 2, handle="", profile_id="emely4")
        self.assertIsNone(recheck_runner.disprove_from_later_share(last, [last]))

    def test_an_inexact_baseline_is_never_evidence(self):
        first = Share(1000.0, 2, handle="", profile_id="emely4", exact=False)
        shares = [first, Share(8000.0, 2, handle="", profile_id="emely4")]
        self.assertIsNone(recheck_runner.disprove_from_later_share(first, shares))
