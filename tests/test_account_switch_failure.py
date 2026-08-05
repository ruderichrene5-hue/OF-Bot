"""A failed account switch must reach a person, not the retry loop.

If the second account is logged out or renamed, no retry fixes it: the phone
launches, Instagram opens, the switcher does not offer the handle, and the run
ends the same way every time. Three launches later the row is "Retries
Exhausted" and the real cause -- somebody has to log the account back in -- is
nowhere in the base. So the outcome is terminal, spends no retries, and flags
the profile for a manual check.
"""

from unittest import TestCase

from adb_bot.automation import retry_runner
from adb_bot.automation.posting_runner import (
    _map_post_status, apply_post_result, consumes_retry_budget,
)
from adb_bot.automation.workflow import is_account_switch_failure
from adb_bot.clients import airtable as at


class _Item:
    queue_id = "q1"
    account_id = None          # profile-driven: no Accounts row exists
    account_name = "Jasmin 5 [naughty_jasminn]"
    variant_id = "v1"
    retry_count = 0


class FakeAirtable:
    def __init__(self):
        self.results = []
        self.run_logs = []
        self.incidents = []
        self.variants_used = []

    def create_run_log(self, account_id, name, flow, result, note):
        self.run_logs.append({"name": name, "result": result, "note": note})

    def set_account_result(self, account_id, text):
        pass

    def mark_post_result(self, queue_id, status, issue_type, retry_count=None):
        self.results.append({"queue_id": queue_id, "status": status,
                             "issue": issue_type, "retry_count": retry_count})
        return True

    def mark_variant_used(self, variant_id):
        self.variants_used.append(variant_id)


class MappingTest(TestCase):
    def test_the_row_is_failed_but_not_marked_retryable(self):
        post_status, issue, incident, _result, _note = _map_post_status("account_switch_failed")
        self.assertEqual(post_status, at.POST_STATUS_FAILED)
        self.assertEqual(issue, at.ISSUE_ACCOUNT_SWITCH)
        self.assertNotEqual(issue, at.ISSUE_NEEDS_RETRY)
        self.assertIsNone(incident)   # there is no Accounts row to flag

    def test_it_spends_no_retries(self):
        """The post was never attempted -- the composer never opened."""
        self.assertFalse(consumes_retry_budget("account_switch_failed"))
        # ...unlike a genuine flow failure, which is worth trying again.
        self.assertTrue(consumes_retry_budget("failed"))

    def test_the_write_back_does_not_bump_the_counter(self):
        airtable = FakeAirtable()
        apply_post_result(airtable, _Item(), "account_switch_failed",
                          detail="could not switch to naughty_jasminn")

        self.assertEqual(len(airtable.results), 1)
        self.assertIsNone(airtable.results[0]["retry_count"])
        self.assertEqual(airtable.results[0]["issue"], at.ISSUE_ACCOUNT_SWITCH)

    def test_the_variant_is_not_consumed(self):
        """Nothing was posted, so the clip must stay available for the account
        once a person has logged it back in."""
        airtable = FakeAirtable()
        apply_post_result(airtable, _Item(), "account_switch_failed")
        self.assertEqual(airtable.variants_used, [])

    def test_the_run_log_says_which_account_and_why(self):
        airtable = FakeAirtable()
        apply_post_result(airtable, _Item(), "account_switch_failed",
                          detail="could not switch to naughty_jasminn")
        note = airtable.run_logs[0]["note"]
        self.assertIn("manual check", note)
        self.assertIn("naughty_jasminn", note)
        self.assertIn("naughty_jasminn", airtable.run_logs[0]["name"])


class RetryPassTest(TestCase):
    def _fields(self, issue):
        return {at.F_PQ_POST_STATUS: at.POST_STATUS_FAILED, at.F_PQ_ISSUE_TYPE: issue}

    def test_the_retry_pass_refuses_to_requeue_it(self):
        outcome, detail = retry_runner._row_verdict(
            self._fields(at.ISSUE_ACCOUNT_SWITCH), max_retries=3)
        self.assertEqual(outcome, retry_runner.OUTCOME_NEEDS_HUMAN)
        self.assertIn("not a retryable failure", detail)

    def test_the_profile_is_flagged_for_a_manual_check(self):
        reason = retry_runner._profile_issue_reason(
            retry_runner.OUTCOME_NEEDS_HUMAN, self._fields(at.ISSUE_ACCOUNT_SWITCH))
        self.assertEqual(reason, at.PROFILE_ISSUE_ACCOUNT_SWITCH)

    def test_a_hand_parked_row_still_flags_nothing(self):
        """`Other` is how a person parks a row; that must stay unflagged."""
        reason = retry_runner._profile_issue_reason(
            retry_runner.OUTCOME_NEEDS_HUMAN, self._fields(at.ISSUE_OTHER))
        self.assertIsNone(reason)

    def test_bans_and_verification_still_map_as_before(self):
        for issue, expected in ((at.ISSUE_BANNED_BLOCKED, at.PROFILE_ISSUE_BANNED),
                                (at.ISSUE_HUMAN_VERIFICATION, at.PROFILE_ISSUE_VERIFICATION)):
            self.assertEqual(
                retry_runner._profile_issue_reason(
                    retry_runner.OUTCOME_NEEDS_HUMAN, self._fields(issue)),
                expected)


class WorkflowStatusTest(TestCase):
    """The rule `_run_profile_workflow` uses to route a switch failure."""

    def test_a_switch_failure_is_not_reported_as_a_plain_failure(self):
        self.assertTrue(is_account_switch_failure(
            {"failed": True, "account_switch_failed": True,
             "verify_detail": "could not switch to naughty_jasminn"}))

    def test_an_ordinary_failure_is_unaffected(self):
        self.assertFalse(is_account_switch_failure({"failed": True}))
        self.assertFalse(is_account_switch_failure({"success": False}))

    def test_an_abort_is_not_turned_into_a_switch_failure(self):
        """A user-requested stop says nothing about the account, and flagging a
        healthy profile sends somebody to look at a phone that is fine."""
        self.assertFalse(is_account_switch_failure(
            {"aborted": True, "account_switch_failed": True}))

    def test_a_non_dict_result_is_handled(self):
        self.assertFalse(is_account_switch_failure(None))
        self.assertFalse(is_account_switch_failure("done"))

    def test_the_real_workflow_branch_uses_this_rule(self):
        """Guards against the predicate drifting away from its only caller."""
        import inspect

        from adb_bot.automation import workflow
        source = inspect.getsource(workflow._run_profile_workflow)
        self.assertIn("is_account_switch_failure(flow_result)", source)
        self.assertIn('"account_switch_failed", flow_result', source)
