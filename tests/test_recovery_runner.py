"""Recovery: clearing `Needs Human Check` actually resumes a profile.

The failure this guards against is silent. A person clears the checkbox,
believes the profile is fixed, and nothing happens -- because what stopped it is
on its queue rows, and those rows also hold its clips.
"""

import logging
from unittest import TestCase

from adb_bot.automation import recovery_runner
from adb_bot.automation.recovery_runner import (
    is_dead_row, plan_recovery, rows_for_profile, run_recovery,
)
from adb_bot.clients import airtable as at

LOG = logging.getLogger("test")


def _row(rid="pq1", status=at.POST_STATUS_FAILED, issue=at.ISSUE_RETRIES_EXHAUSTED,
         retry=3, profile_id="recP1", account_id=None):
    fields = {at.F_PQ_POST_STATUS: status, at.F_PQ_ISSUE_TYPE: issue,
              at.F_PQ_RETRY_COUNT: retry}
    if profile_id:
        fields[at.F_PQ_TARGET_PROFILE] = [profile_id]
    if account_id:
        fields[at.F_PQ_TARGET_ACCOUNT] = [account_id]
    return {"id": rid, "fields": fields}


def _profile(record_id="recP1", name="Jil 3", status="Active", reason=at.PROFILE_ISSUE_EXHAUSTED):
    return {"record_id": record_id, "name": name, "status": status,
            "reason": reason, "flagged_at": "2026-08-05T21:00:00+00:00"}


class FakeRecoveryClient:
    def __init__(self, profiles=None, queue_rows=None, accounts=None,
                 reset_fails=(), clear_fails=False):
        self._profiles = profiles or []
        self._queue_rows = queue_rows or []
        self._accounts = accounts or {}
        self._reset_fails = set(reset_fails)
        self._clear_fails = clear_fails
        self.reset_rows, self.cleared = [], []

    def profiles_awaiting_recovery(self):
        return list(self._profiles)

    def list_queue_rows(self, statuses=None):
        return list(self._queue_rows)

    def accounts_by_id(self):
        return dict(self._accounts)

    def reset_row_for_retry(self, record_id):
        if record_id in self._reset_fails:
            return False
        self.reset_rows.append(record_id)
        return True

    def clear_profile_issue(self, record_id, note, when_iso=None):
        if self._clear_fails:
            return False
        self.cleared.append((record_id, note))
        return True


class DeadRowTest(TestCase):
    def test_an_exhausted_row_is_dead(self):
        self.assertTrue(is_dead_row(_row()["fields"]))

    def test_a_retryable_row_mid_ladder_is_left_alone(self):
        """The retry pass still owns this one. Resetting it would hand it a
        fresh set of attempts it has not earned."""
        self.assertFalse(is_dead_row(_row(issue=at.ISSUE_NEEDS_RETRY, retry=1)["fields"]))

    def test_a_retryable_row_at_the_limit_is_dead(self):
        self.assertTrue(is_dead_row(_row(issue=at.ISSUE_NEEDS_RETRY, retry=3)["fields"]))

    def test_rows_a_person_owns_are_dead_too(self):
        """A ban or a verification prompt is exactly what gets fixed by hand,
        and clearing the flag is the person saying they fixed it."""
        for issue in (at.ISSUE_BANNED_BLOCKED, at.ISSUE_HUMAN_VERIFICATION, at.ISSUE_OTHER):
            with self.subTest(issue=issue):
                self.assertTrue(is_dead_row(_row(issue=issue, retry=0)["fields"]))

    def test_a_row_that_did_not_fail_is_not_touched(self):
        for status in (at.POST_STATUS_PENDING, at.POST_STATUS_POSTED, at.POST_STATUS_VERIFYING):
            with self.subTest(status=status):
                self.assertFalse(is_dead_row(_row(status=status)["fields"]))

    def test_a_junk_retry_count_does_not_crash(self):
        fields = _row(issue=at.ISSUE_NEEDS_RETRY)["fields"] | {at.F_PQ_RETRY_COUNT: "three"}
        self.assertFalse(is_dead_row(fields))


class RowLookupTest(TestCase):
    def test_rows_linked_straight_to_the_profile(self):
        rows = [_row("pq1"), _row("pq2", profile_id="recOther")]
        self.assertEqual([r["id"] for r in rows_for_profile(rows, "recP1")], ["pq1"])

    def test_rows_that_reach_the_profile_through_an_account(self):
        """The posting planner accepts both targeting paths, so recovery has to
        see both or an account-driven profile recovers nothing."""
        rows = [_row("pq1", profile_id=None, account_id="recA1")]
        found = rows_for_profile(rows, "recP1", {"recP1": {"recA1"}})
        self.assertEqual([r["id"] for r in found], ["pq1"])

    def test_another_profiles_account_rows_are_not_taken(self):
        rows = [_row("pq1", profile_id=None, account_id="recA9")]
        self.assertEqual(rows_for_profile(rows, "recP1", {"recP1": {"recA1"}}), [])


class PlanTest(TestCase):
    def test_only_the_dead_rows_are_handed_back(self):
        rows = [_row("pq1"),                                            # exhausted
                _row("pq2", issue=at.ISSUE_NEEDS_RETRY, retry=1),       # retry owns it
                _row("pq3", status=at.POST_STATUS_POSTED)]              # landed
        report = plan_recovery([_profile()], rows)
        self.assertEqual(report.recovered[0].row_ids, ["pq1"])

    def test_a_profile_with_nothing_stuck_is_still_closed_out(self):
        """Otherwise it is reconsidered on every tick, forever."""
        report = plan_recovery([_profile()], [])
        self.assertEqual(len(report.recovered), 1)
        self.assertEqual(report.recovered[0].row_ids, [])

    def test_a_parked_profile_is_flagged_as_such(self):
        report = plan_recovery([_profile(status="Inactive")], [])
        self.assertTrue(report.recovered[0].parked)

    def test_an_active_profile_is_not_parked(self):
        self.assertFalse(plan_recovery([_profile()], []).recovered[0].parked)


class RunRecoveryTest(TestCase):
    def test_dead_rows_are_reset_and_the_issue_closed(self):
        client = FakeRecoveryClient(profiles=[_profile()], queue_rows=[_row("pq1"), _row("pq2")])
        report = run_recovery(client, LOG, dry_run=False)
        self.assertEqual(sorted(client.reset_rows), ["pq1", "pq2"])
        self.assertEqual(report.rows_reset, 2)
        self.assertEqual(report.profiles_cleared, 1)
        self.assertEqual([rid for rid, _note in client.cleared], ["recP1"])
        self.assertIn("2 queue row(s)", client.cleared[0][1])

    def test_dry_run_writes_nothing(self):
        client = FakeRecoveryClient(profiles=[_profile()], queue_rows=[_row("pq1")])
        report = run_recovery(client, LOG)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.rows_reset, 1)      # what an --apply run would do
        self.assertEqual(client.reset_rows, [])
        self.assertEqual(client.cleared, [])

    def test_a_partly_reset_profile_stays_flagged_for_the_next_tick(self):
        """Clearing Flagged At is what stops this profile being reconsidered.
        Doing it after a failed write would strand the row nobody reset."""
        client = FakeRecoveryClient(profiles=[_profile()],
                                    queue_rows=[_row("pq1"), _row("pq2")],
                                    reset_fails=["pq2"])
        report = run_recovery(client, LOG, dry_run=False)
        self.assertEqual(client.cleared, [])
        self.assertEqual(report.profiles_cleared, 0)
        self.assertTrue(report.errors)

    def test_a_failed_clear_is_reported(self):
        client = FakeRecoveryClient(profiles=[_profile()], queue_rows=[], clear_fails=True)
        report = run_recovery(client, LOG, dry_run=False)
        self.assertEqual(report.profiles_cleared, 0)
        self.assertTrue(report.errors)

    def test_nothing_waiting_is_a_quiet_no_op(self):
        client = FakeRecoveryClient(profiles=[])
        report = run_recovery(client, LOG, dry_run=False)
        self.assertEqual(report.recovered, [])
        self.assertEqual(report.errors, [])

    def test_an_airtable_failure_is_an_error_not_a_crash(self):
        class Broken(FakeRecoveryClient):
            def list_queue_rows(self, statuses=None):
                raise RuntimeError("Airtable 503")

        report = run_recovery(Broken(profiles=[_profile()]), LOG, dry_run=False)
        self.assertTrue(report.errors)
        self.assertIn("503", report.errors[0][1])

    def test_a_parked_profile_is_still_recovered_but_reported(self):
        """Status is the park switch a person owns -- recovery must not flip it
        back, or clearing one checkbox would un-park a profile somebody
        deliberately switched off."""
        client = FakeRecoveryClient(profiles=[_profile(status="Inactive")],
                                    queue_rows=[_row("pq1")])
        report = run_recovery(client, LOG, dry_run=False)
        self.assertEqual(client.reset_rows, ["pq1"])
        self.assertTrue(report.recovered[0].parked)
        self.assertIn("still_parked=1", report.summary())
        # Nothing in this pass writes Status.
        self.assertFalse(hasattr(client, "status_writes"))


class ScheduleTest(TestCase):
    def test_the_loop_is_wired_into_the_cli_and_the_schedule(self):
        from adb_bot.automation import run_loop, schedule_spec
        self.assertIn("recovery", run_loop.LOOPS)
        self.assertIn("recovery", run_loop._DISPATCH)
        self.assertIn("recovery", schedule_spec.RECOMMENDED_LOOPS)
        self.assertIn("recovery", schedule_spec.RECOMMENDED_INTERVALS)

    def test_it_runs_more_often_than_the_retry_pass_it_feeds(self):
        """A person clearing a checkbox expects the bot to notice within
        minutes, and a revived row should not then wait a whole retry cycle."""
        from adb_bot.automation import schedule_spec
        self.assertLessEqual(schedule_spec.RECOMMENDED_INTERVALS["recovery"],
                             schedule_spec.RECOMMENDED_INTERVALS["retry"])

    def test_the_reset_matches_what_the_retry_pass_will_accept(self):
        """The handover contract: whatever reset_row_for_retry writes has to
        pass retry_runner's own gate, or recovery quietly does nothing."""
        from adb_bot.automation.retry_runner import _row_verdict
        revived = {at.F_PQ_POST_STATUS: at.POST_STATUS_FAILED,
                   at.F_PQ_ISSUE_TYPE: at.ISSUE_NEEDS_RETRY,
                   at.F_PQ_RETRY_COUNT: 0}
        self.assertIsNone(_row_verdict(revived))
        self.assertFalse(recovery_runner.is_dead_row(revived))
