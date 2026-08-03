"""Automatic retry is the step that lets the cycle run unattended -- and the one
step that can double-post if it is wrong.

The test that carries the design is `test_a_shared_ledger_entry_is_not_requeued`:
Airtable says Failed, the ledger says "we tapped Share and never found out what
happened", and the reel may be live right now. Every other case here is about the
same principle from a different angle -- unknown is never a yes.
"""

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from adb_bot.automation import post_ledger, retry_runner
from adb_bot.automation.post_ledger import PostLedger, STATUS_CONFIRMED, STATUS_DISPROVED
from adb_bot.automation.retry_runner import (
    OUTCOME_BLOCKED,
    OUTCOME_EXHAUSTED,
    OUTCOME_NEEDS_HUMAN,
    OUTCOME_NOT_FAILED,
    OUTCOME_RETRY,
    OUTCOME_UNRESOLVED,
    decide_retry,
    resolve_media,
    resolve_profile_id,
    retry_delay_seconds,
)
from adb_bot.clients import airtable as at

PROFILE_ID = "624354174112432228"       # the 18-digit MLX API ID
NOW = 1_785_000_000.0                   # fixed clock so backoff stamps are exact
FIFTEEN_MIN = 15 * 60


def failed_row(rec_id="recQ1", issue=at.ISSUE_NEEDS_RETRY, retry=1, account="recAcc1",
               profile=None, variant="recVar1", status=at.POST_STATUS_FAILED,
               name="nikki_1 / reel"):
    fields = {at.F_PQ_NAME: name, at.F_PQ_POST_STATUS: status, at.F_PQ_RETRY_COUNT: retry}
    if issue is not None:
        fields[at.F_PQ_ISSUE_TYPE] = issue
    if account is not None:
        fields[at.F_PQ_TARGET_ACCOUNT] = [account]
    if profile is not None:
        fields[at.F_PQ_TARGET_PROFILE] = [profile]
    if variant is not None:
        fields[at.F_PQ_SPOOF_VARIANT] = [variant]
    return {"id": rec_id, "fields": fields}


class BackoffTest(TestCase):
    def test_backoff_grows_with_each_attempt(self):
        # A failure that repeats is not transient; spacing the attempts out is
        # what stops all three retries burning on the same broken minute.
        self.assertEqual(retry_delay_seconds(0), FIFTEEN_MIN)
        self.assertEqual(retry_delay_seconds(1), 2 * FIFTEEN_MIN)
        self.assertEqual(retry_delay_seconds(2), 4 * FIFTEEN_MIN)

    def test_backoff_is_capped(self):
        self.assertEqual(retry_delay_seconds(99), retry_runner.RETRY_BACKOFF_MAX_SECONDS)

    def test_a_junk_retry_count_does_not_explode(self):
        self.assertEqual(retry_delay_seconds(None), FIFTEEN_MIN)
        self.assertEqual(retry_delay_seconds("nope"), FIFTEEN_MIN)


class DecideRetryTest(TestCase):
    """The ruling, with the ledger lookup already done -- no files, no base."""

    def _decide(self, row=None, record=None, profile_id=PROFILE_ID, media_hash="abc123",
                max_retries=3):
        row = row or failed_row()
        return decide_retry(row["fields"], profile_id, media_hash, record,
                            max_retries=max_retries)

    def test_a_clean_needs_retry_row_is_eligible(self):
        outcome, _ = self._decide()
        self.assertEqual(outcome, OUTCOME_RETRY)

    def test_a_row_that_is_not_failed_is_left_alone(self):
        outcome, _ = self._decide(failed_row(status=at.POST_STATUS_VERIFYING))
        self.assertEqual(outcome, OUTCOME_NOT_FAILED)

    def test_banned_is_never_retried(self):
        # Re-posting does not un-ban an account; at best the attempt is wasted.
        outcome, detail = self._decide(failed_row(issue=at.ISSUE_BANNED_BLOCKED))
        self.assertEqual(outcome, OUTCOME_NEEDS_HUMAN)
        self.assertIn(at.ISSUE_BANNED_BLOCKED, detail)

    def test_human_verification_is_never_retried(self):
        outcome, _ = self._decide(failed_row(issue=at.ISSUE_HUMAN_VERIFICATION))
        self.assertEqual(outcome, OUTCOME_NEEDS_HUMAN)

    def test_an_unlabelled_failure_is_never_retried(self):
        # No Issue Type means nobody established *why* it failed.
        outcome, _ = self._decide(failed_row(issue=None))
        self.assertEqual(outcome, OUTCOME_NEEDS_HUMAN)

    def test_the_retry_cap_stops_the_row(self):
        outcome, detail = self._decide(failed_row(retry=3), max_retries=3)
        self.assertEqual(outcome, OUTCOME_EXHAUSTED)
        self.assertIn("limit of 3", detail)

    def test_the_cap_is_caller_overridable(self):
        outcome, _ = self._decide(failed_row(retry=3), max_retries=5)
        self.assertEqual(outcome, OUTCOME_RETRY)

    def test_an_unresolved_shared_entry_blocks(self):
        """The case the whole module is built around: Share was tapped and the
        outcome was never proven. "We don't know" is not "it didn't post"."""
        record = post_ledger.ShareRecord(PROFILE_ID, "abc123")
        outcome, detail = self._decide(record=record)
        self.assertEqual(outcome, OUTCOME_BLOCKED)
        self.assertIn("double post", detail)

    def test_a_confirmed_entry_blocks(self):
        record = post_ledger.ShareRecord(PROFILE_ID, "abc123", status=STATUS_CONFIRMED)
        outcome, _ = self._decide(record=record)
        self.assertEqual(outcome, OUTCOME_BLOCKED)

    def test_a_disproved_entry_clears_the_way(self):
        # Positive evidence the reel is not on the account -- the only thing that
        # re-opens a clip, and the deferred recheck is what writes it.
        record = post_ledger.ShareRecord(PROFILE_ID, "abc123", status=STATUS_DISPROVED)
        outcome, _ = self._decide(record=record)
        self.assertEqual(outcome, OUTCOME_RETRY)

    def test_no_profile_id_is_not_a_yes(self):
        outcome, _ = self._decide(profile_id="")
        self.assertEqual(outcome, OUTCOME_UNRESOLVED)

    def test_no_media_hash_is_not_a_yes(self):
        outcome, _ = self._decide(media_hash="")
        self.assertEqual(outcome, OUTCOME_UNRESOLVED)


class ResolveTest(TestCase):
    def setUp(self):
        self.accounts = {"recAcc1": {at.F_ACC_NAME: "nikki_1", at.F_ACC_PROFILE: ["recProf1"]}}
        self.profiles = {"recProf1": {"launch_id": PROFILE_ID, "name": "Nikki 1"}}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clip = Path(self.tmp.name) / "v1.mp4"
        self.clip.write_bytes(b"reel bytes")
        self.variants = {"recVar1": {"file_path": str(self.clip), "status": "Ready"}}

    def test_account_rows_resolve_through_the_linked_profile(self):
        got = resolve_profile_id(failed_row()["fields"], self.accounts, self.profiles)
        self.assertEqual(got, PROFILE_ID)

    def test_profile_driven_rows_resolve_directly(self):
        row = failed_row(account=None, profile="recProf1")
        self.assertEqual(resolve_profile_id(row["fields"], {}, self.profiles), PROFILE_ID)

    def test_an_unknown_profile_resolves_to_nothing_not_a_guess(self):
        row = failed_row(account=None, profile="recNope")
        self.assertEqual(resolve_profile_id(row["fields"], {}, self.profiles), "")

    def test_the_hash_matches_the_one_the_ledger_stores(self):
        path, digest = resolve_media(failed_row()["fields"], self.variants)
        self.assertEqual(path, str(self.clip))
        self.assertEqual(digest, post_ledger.media_fingerprint(self.clip))

    def test_a_deleted_variant_file_yields_no_hash(self):
        self.clip.unlink()
        _path, digest = resolve_media(failed_row()["fields"], self.variants)
        self.assertEqual(digest, "")

    def test_a_row_with_no_variant_yields_nothing(self):
        self.assertEqual(resolve_media(failed_row(variant=None)["fields"], self.variants),
                         ("", ""))


class RetryPassTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = PostLedger(root / "ledger.jsonl")
        self.clip = root / "v1.mp4"
        self.clip.write_bytes(b"reel bytes")

        self.airtable = MagicMock()
        self.airtable.list_failed_posts.return_value = [failed_row()]
        self.airtable.accounts_by_id.return_value = {
            "recAcc1": {at.F_ACC_NAME: "nikki_1", at.F_ACC_PROFILE: ["recProf1"]}}
        self.airtable.profile_launch_map.return_value = {
            "recProf1": {"launch_id": PROFILE_ID, "name": "Nikki 1"}}
        self.airtable.variants_by_id.return_value = {
            "recVar1": {"file_path": str(self.clip), "status": "Ready"}}
        self.airtable.requeue_post.return_value = True

    def _run(self, **kwargs):
        kwargs.setdefault("dry_run", False)
        return retry_runner.retry_failed_posts(
            self.airtable, ledger=self.ledger, now=lambda: NOW, **kwargs)

    def test_an_eligible_row_is_requeued_with_backoff(self):
        tally = self._run()
        self.assertEqual(tally["requeued"], 1)
        queue_id, due = self.airtable.requeue_post.call_args[0]
        self.assertEqual(queue_id, "recQ1")
        # Retry Count 1 -> second attempt -> 15min * 2**1 = 30 minutes out.
        self.assertEqual(due, retry_runner._iso_at(NOW + 30 * 60))

    def test_the_row_goes_back_to_pending_and_keeps_its_retry_count(self):
        """Retry Count is the attempt budget and only the posting runner bumps
        it -- bumping it here too would halve every row's real budget."""
        import inspect
        src = inspect.getsource(at.AirtableClient.requeue_post)
        self.assertIn("POST_STATUS_PENDING", src)
        self.assertIn("F_PQ_SCHEDULED", src)
        self.assertNotIn("F_PQ_RETRY_COUNT", src)
        self._run()
        self.airtable.mark_post_result.assert_not_called()

    def test_a_shared_ledger_entry_is_not_requeued(self):
        """The critical one. The reel may be live on the account right now: the
        share was recorded and nothing ever resolved it. Airtable's Failed is not
        evidence of absence."""
        self.ledger.record_share(PROFILE_ID, self.clip, queue_id="recQ1")
        tally = self._run()
        self.assertEqual(tally["blocked"], 1)
        self.assertEqual(tally["requeued"], 0)
        self.airtable.requeue_post.assert_not_called()

    def test_a_confirmed_ledger_entry_is_not_requeued(self):
        digest = post_ledger.media_fingerprint(self.clip)
        self.ledger.record_share(PROFILE_ID, self.clip, queue_id="recQ1")
        self.ledger.resolve(PROFILE_ID, digest, STATUS_CONFIRMED, "counted +1")
        tally = self._run()
        self.assertEqual(tally["blocked"], 1)
        self.airtable.requeue_post.assert_not_called()

    def test_a_disproved_ledger_entry_is_requeued(self):
        # The deferred recheck proved the reel never landed, so re-sending it is
        # exactly what should happen.
        digest = post_ledger.media_fingerprint(self.clip)
        self.ledger.record_share(PROFILE_ID, self.clip, queue_id="recQ1")
        self.ledger.resolve(PROFILE_ID, digest, STATUS_DISPROVED, "count never moved")
        tally = self._run()
        self.assertEqual(tally["requeued"], 1)
        self.airtable.requeue_post.assert_called_once()

    def test_banned_rows_are_never_requeued(self):
        self.airtable.list_failed_posts.return_value = [
            failed_row(issue=at.ISSUE_BANNED_BLOCKED)]
        tally = self._run()
        self.assertEqual(tally["needs_human"], 1)
        self.airtable.requeue_post.assert_not_called()

    def test_human_verification_rows_are_never_requeued(self):
        self.airtable.list_failed_posts.return_value = [
            failed_row(issue=at.ISSUE_HUMAN_VERIFICATION)]
        tally = self._run()
        self.assertEqual(tally["needs_human"], 1)
        self.airtable.requeue_post.assert_not_called()

    def test_a_row_at_the_cap_is_not_requeued(self):
        self.airtable.list_failed_posts.return_value = [failed_row(retry=3)]
        tally = self._run()
        self.assertEqual(tally["exhausted"], 1)
        self.airtable.requeue_post.assert_not_called()

    def test_dry_run_writes_nothing(self):
        tally = self._run(dry_run=True)
        self.assertEqual(tally["requeued"], 1)   # reported...
        self.airtable.requeue_post.assert_not_called()   # ...but not written

    def test_dry_run_is_the_default(self):
        tally = retry_runner.retry_failed_posts(self.airtable, ledger=self.ledger)
        self.assertEqual(tally["requeued"], 1)
        self.airtable.requeue_post.assert_not_called()

    def test_an_unresolvable_profile_is_skipped_not_retried(self):
        # No MLX API ID means no ledger key, so the guard cannot be consulted --
        # and a retry we cannot check is worse than a post we don't make.
        self.airtable.profile_launch_map.return_value = {"recProf1": {"launch_id": None}}
        tally = self._run()
        self.assertEqual(tally["unresolved"], 1)
        self.airtable.requeue_post.assert_not_called()

    def test_a_missing_media_file_is_skipped_not_retried(self):
        self.clip.unlink()
        tally = self._run()
        self.assertEqual(tally["unresolved"], 1)
        self.airtable.requeue_post.assert_not_called()

    def test_a_missing_ledger_file_does_not_mean_go_ahead_blindly(self):
        # An empty/absent ledger genuinely means this machine never sent the clip
        # -- that is the normal first-failure case and it must still retry.
        empty = PostLedger(Path(self.tmp.name) / "nothing-here.jsonl")
        tally = retry_runner.retry_failed_posts(
            self.airtable, ledger=empty, now=lambda: NOW, dry_run=False)
        self.assertEqual(tally["requeued"], 1)

    def test_an_airtable_outage_is_survivable(self):
        self.airtable.list_failed_posts.side_effect = RuntimeError("503")
        tally = self._run()
        self.assertEqual(tally["considered"], 0)
        self.assertEqual(tally["requeued"], 0)

    def test_a_write_that_fails_is_counted_not_swallowed(self):
        self.airtable.requeue_post.return_value = False
        tally = self._run()
        self.assertEqual(tally["errors"], 1)
        self.assertEqual(tally["requeued"], 0)

    def test_a_write_that_raises_does_not_stop_the_pass(self):
        self.airtable.list_failed_posts.return_value = [
            failed_row(rec_id="recQ1"), failed_row(rec_id="recQ2")]
        self.airtable.requeue_post.side_effect = [RuntimeError("boom"), True]
        tally = self._run()
        self.assertEqual(tally["errors"], 1)
        self.assertEqual(tally["requeued"], 1)


class LedgerContractTest(TestCase):
    """retry_runner must not grow its own idea of what is safe."""

    def test_the_ledger_is_the_authority_on_reposting(self):
        import inspect
        src = inspect.getsource(retry_runner)
        self.assertIn("blocks_repost", src)
        # already_shared() answers False for an unreadable file ("no opinion"),
        # which is the right default for the posting flow and the wrong one here.
        self.assertNotIn("already_shared", src)

    def test_hashing_is_not_reimplemented(self):
        import inspect
        src = inspect.getsource(retry_runner)
        self.assertIn("media_fingerprint", src)
        # A second hashing implementation would key the ledger differently and
        # every lookup would miss -- which reads as "never posted".
        self.assertNotIn("hashlib", src)
