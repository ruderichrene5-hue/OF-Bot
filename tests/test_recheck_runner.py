"""The deferred pass exists to answer the question the run could not.

The case that carries the design is `test_equal_count_is_a_real_failure`: during
a run, "the count did not move" means nothing, because the counter lags. Fifteen
minutes later it is proof. Everything else here is about not overreaching when
the evidence isn't there.
"""

import tempfile
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

    def test_an_airtable_outage_is_survivable(self):
        self.airtable.list_posts_awaiting_recheck.side_effect = RuntimeError("503")
        tally = recheck_runner.recheck_pending_posts(
            self.airtable, lambda pid, fields: Count(42, True), ledger=self.ledger)
        self.assertEqual(tally["checked"], 0)


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
