"""The deferred pass exists to answer the question the run could not.

The pair that carries the design is `test_equal_count_is_not_yet_a_failure` and
`test_equal_count_is_a_failure_once_the_wait_is_over`. A confirmation and a
disproof run on different clocks: a +1 is trustworthy the moment it appears, but
an unmoved count means only "not there *yet*" until enough time has passed that a
late publish is implausible. Instagram published a Nikki 3 reel after it had been
read as absent twice, and the premature Failed posted it a second time.
Everything else here is about not overreaching when the evidence isn't there.
"""

import tempfile
import time
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
# Past MIN_DISPROOF_AGE_SECONDS: old enough that an unmoved count is absence.
WELL_AGED = recheck_runner.MIN_DISPROOF_AGE_SECONDS + 3600


class DecideRecheckTest(TestCase):
    def test_higher_count_confirms(self):
        outcome, detail = decide_recheck(41, True, Count(42, True), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_POSTED)
        self.assertIn("41 -> 42", detail)

    def test_equal_count_is_not_yet_a_failure(self):
        """Fifteen minutes of absence is not absence. The reading is correct and
        the conclusion is still wrong: Instagram can publish hours after the phone
        let go, and Failed here is what re-opens the clip and posts it twice."""
        outcome, detail = decide_recheck(41, True, Count(41, True), FIFTEEN_MIN)
        self.assertEqual(outcome, OUTCOME_UNKNOWN)
        self.assertIn("too early", detail)

    def test_equal_count_is_a_failure_once_the_wait_is_over(self):
        """The whole reason for waiting. Past the floor, an unmoved count is the
        one moment Failed can be said with confidence."""
        outcome, detail = decide_recheck(41, True, Count(41, True), WELL_AGED)
        self.assertEqual(outcome, OUTCOME_FAILED)
        self.assertIn("did not land", detail)

    def test_a_late_publish_is_caught_by_a_later_pass(self):
        """Nikki 3, 2026-08-11. Absent at +39 min and again at +70 min, live
        afterwards. The early passes must stay UNKNOWN so that the pass which
        finally sees the +1 is the one that gets to rule."""
        for age in (39 * 60, 70 * 60):
            self.assertEqual(decide_recheck(137, True, Count(137, True), age)[0],
                             OUTCOME_UNKNOWN)
        self.assertEqual(decide_recheck(137, True, Count(138, True), 3 * 3600)[0],
                         OUTCOME_POSTED)

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

    def test_a_fresh_unmoved_count_leaves_the_clip_blocked(self):
        """The share is minutes old, so absence proves nothing yet. The row is
        re-parked and the clip must still be un-sendable."""
        self._share()
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(41, True), ledger=self.ledger)
        self.assertEqual(tally["unknown"], 1)
        self.assertEqual(tally["failed"], 0)
        self.assertTrue(self.ledger.already_shared("p1", self.clip))
        self.airtable.mark_post_pending_verification.assert_called_once()

    def test_a_failed_recheck_re_opens_the_clip(self):
        """The only path that clears the ledger, and it needs positive evidence
        of absence -- which now includes having waited long enough for absence to
        mean anything."""
        self._share()
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(41, True), ledger=self.ledger,
            now=lambda: time.time() + WELL_AGED)
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
            self.airtable, lambda pid, fields: Count(41, True), ledger=self.ledger,
            now=lambda: time.time() + WELL_AGED)
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
