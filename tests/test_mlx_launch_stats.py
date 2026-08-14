"""MultiLogin-side 500s must be counted, rated, and told apart from our own.

The failure being tracked is MultiLogin's own, taken verbatim from
logs/loop_posting.log on 2026-08-04/05:

    {'status': 'error',
     'error': '500 Server Error: Internal Server Error for url: '
              'https://launcher.mlx.yt:45001/api/v1/mobile_phone/launch',
     'response_text': '{"status":{"error_code":"INTERNAL_SERVER_ERROR",'
                      '"http_code":500,'
                      '"message":"failed to get profiles starting urls"}}',
     'status_code': 500}

48 of 289 launch attempts that night (16.6%, 2.9%-50% by hour). In the same
logs a *different* 30 attempts failed with "Connection refused" to the local
launcher -- our side, the MLX agent was down. The two must never share a
counter: one says wait, the other says go fix the box.
"""

import logging
import threading
from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import MagicMock, patch

from adb_bot.automation import posting_runner
from adb_bot.clients import airtable as at
from adb_bot.clients.multilogin.launch_stats import (
    MLX_500, OK, OTHER, CountingLauncherClient, LaunchStats, classify_launch,
    is_mlx_start_urls_failure,
)

LOG = logging.getLogger("test_mlx_launch_stats")
LOG.addHandler(logging.NullHandler())


# --- real answers, copied from the logs --------------------------------------

MLX_500_RESPONSE = {
    "status": "error",
    "error": ("500 Server Error: Internal Server Error for url: "
              "https://launcher.mlx.yt:45001/api/v1/mobile_phone/launch"),
    "response_text": ('{"status":{"error_code":"INTERNAL_SERVER_ERROR","http_code":500,'
                      '"message":"failed to get profiles starting urls"}}'),
    "status_code": 500,
}

# Our side: the launcher process was not listening at all.
CONNECTION_REFUSED_RESPONSE = {
    "status": "error",
    "error": ("HTTPSConnectionPool(host='launcher.mlx.yt', port=45001): Max retries exceeded "
              "with url: /api/v1/mobile_phone/launch (Caused by NewConnectionError("
              "\"HTTPSConnection(host='launcher.mlx.yt', port=45001): Failed to establish a "
              "new connection: [Errno 111] Connection refused\"))"),
    "response_text": None,
    "status_code": None,
}

OK_RESPONSE = {
    "data": {"fail_amount": 0, "success_amount": 1,
             "success_details": [{"id": "628516863629852802", "url": "https://phone.geelark.com/"}],
             "total_amount": 1},
    "status": {"error_code": "", "http_code": 200, "message": ""},
}


class ClassifyLaunchTest(TestCase):
    def test_the_real_500_body_is_theirs(self):
        self.assertEqual(classify_launch(MLX_500_RESPONSE), MLX_500)
        self.assertTrue(is_mlx_start_urls_failure(MLX_500_RESPONSE))

    def test_the_launcher_logs_wording_is_matched_too(self):
        """Their API says "starting urls", their own launcher log says "start
        urls" -- the same failure, and both must land in the same bucket."""
        from_launcher_log = "failed to get profiles start urls: internal server error"
        self.assertTrue(is_mlx_start_urls_failure(from_launcher_log))
        self.assertEqual(classify_launch(None, error=RuntimeError(from_launcher_log)), MLX_500)

    def test_connection_refused_is_ours_not_theirs(self):
        self.assertEqual(classify_launch(CONNECTION_REFUSED_RESPONSE), OTHER)
        self.assertFalse(is_mlx_start_urls_failure(CONNECTION_REFUSED_RESPONSE))

    def test_a_successful_launch_is_ok(self):
        self.assertEqual(classify_launch(OK_RESPONSE), OK)

    def test_any_5xx_from_their_launcher_counts_as_theirs(self):
        other_500 = {"status": "error", "error": "503 Server Error", "status_code": 503}
        self.assertEqual(classify_launch(other_500), MLX_500)

    def test_a_4xx_is_not_charged_to_their_cloud(self):
        bad_token = {"status": "error", "error": "401 Client Error", "status_code": 401}
        self.assertEqual(classify_launch(bad_token), OTHER)

    def test_a_200_that_launched_nothing_is_not_a_cloud_500(self):
        refused = {"data": {"fail_amount": 1, "success_amount": 0, "total_amount": 1},
                   "status": {"error_code": "", "http_code": 200, "message": ""}}
        self.assertEqual(classify_launch(refused), OTHER)


class LaunchStatsTest(TestCase):
    def test_counts_and_rate(self):
        stats = LaunchStats()
        for _ in range(6):
            stats.record(OK_RESPONSE, ["p1"])
        for _ in range(3):
            stats.record(MLX_500_RESPONSE, ["p2"])
        stats.record(CONNECTION_REFUSED_RESPONSE, ["p3"])

        self.assertEqual(stats.attempts, 10)
        self.assertEqual(stats.ok, 6)
        self.assertEqual(stats.mlx_500, 3)
        self.assertEqual(stats.start_urls, 3)
        self.assertEqual(stats.other, 1)
        self.assertAlmostEqual(stats.rate, 0.3)
        self.assertIn("30.0%", stats.summary())

    def test_zero_launches_does_not_divide_by_zero(self):
        stats = LaunchStats()
        self.assertEqual(stats.rate, 0.0)
        self.assertIn("0 attempt(s)", stats.summary())
        self.assertIn("(0.0%)", stats.summary())
        self.assertEqual(stats.as_dict()["mlx_500_rate"], 0.0)

    def test_retries_are_only_attributed_to_profiles_that_500ed(self):
        stats = LaunchStats()
        stats.record(MLX_500_RESPONSE, ["theirs"])
        stats.record(CONNECTION_REFUSED_RESPONSE, ["ours"])
        self.assertTrue(stats.note_retry_consumed("theirs"))
        self.assertFalse(stats.note_retry_consumed("ours"))
        self.assertFalse(stats.note_retry_consumed("never-launched"))
        self.assertEqual(stats.retries_consumed, 1)

    def test_as_dict_carries_the_numbers_for_the_run_result(self):
        stats = LaunchStats()
        stats.record(OK_RESPONSE, ["p1"])
        stats.record(MLX_500_RESPONSE, ["p2"])
        stats.note_retry_consumed("p2")
        self.assertEqual(stats.as_dict(), {
            "launch_attempts": 2, "launch_ok": 1, "mlx_500": 1, "mlx_500_rate": 0.5,
            "launch_failures_other": 0, "mlx_500_retries": 1,
        })

    def test_it_is_safe_across_threads(self):
        """Relaunches come from the worker threads, not only the launch gate."""
        stats = LaunchStats()

        def hammer():
            for _ in range(200):
                stats.record(MLX_500_RESPONSE, ["p"])

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(stats.attempts, 800)
        self.assertEqual(stats.mlx_500, 800)


class CountingLauncherClientTest(TestCase):
    def test_it_passes_the_answer_through_and_counts_it(self):
        inner = MagicMock()
        inner.start_profiles.return_value = MLX_500_RESPONSE
        client = CountingLauncherClient(inner)
        self.assertEqual(client.start_profiles(["p1"]), MLX_500_RESPONSE)
        inner.start_profiles.assert_called_once_with(["p1"])
        self.assertEqual(client.stats.mlx_500, 1)

    def test_a_raised_error_is_counted_and_re_raised(self):
        inner = MagicMock()
        inner.start_profiles.side_effect = RuntimeError("boom")
        client = CountingLauncherClient(inner)
        with self.assertRaises(RuntimeError):
            client.start_profiles(["p1"])
        self.assertEqual(client.stats.attempts, 1)
        self.assertEqual(client.stats.other, 1)

    def test_other_attributes_still_reach_the_real_client(self):
        inner = MagicMock(bearer_token="tok", base_url="https://launcher.mlx.yt:45001/x")
        client = CountingLauncherClient(inner)
        self.assertEqual(client.bearer_token, "tok")
        self.assertEqual(client.base_url, "https://launcher.mlx.yt:45001/x")

    def test_relaunches_land_on_the_same_tally(self):
        """Readiness relaunches through the same object, so a profile launched
        twice is counted twice -- that second attempt IS the spent budget."""
        inner = MagicMock()
        inner.start_profiles.side_effect = [MLX_500_RESPONSE, MLX_500_RESPONSE, OK_RESPONSE]
        client = CountingLauncherClient(inner)
        for _ in range(3):
            client.start_profiles(["p1"])
        self.assertEqual(client.stats.attempts, 3)
        self.assertEqual(client.stats.mlx_500, 2)
        self.assertAlmostEqual(client.stats.rate, 2 / 3)


class ConsumesRetryBudgetTest(TestCase):
    """Which outcomes actually spend one of a queue row's retries -- the thing
    an MLX 500 costs a row on its way to 'Retries Exhausted'."""

    def test_retryable_failures_spend_a_retry(self):
        for status in ("failed", "adb_connect_failed", "heartbeat_lost"):
            self.assertTrue(posting_runner.consumes_retry_budget(status), status)

    def test_success_and_verifying_do_not(self):
        for status in ("done", "uncertain"):
            self.assertFalse(posting_runner.consumes_retry_budget(status), status)

    def test_incidents_and_already_shared_do_not(self):
        for status in ("banned", "action_block", "human_verification", "already_shared"):
            self.assertFalse(posting_runner.consumes_retry_budget(status), status)

    def test_it_matches_what_apply_post_result_writes(self):
        """Guards against the predicate drifting from the write-back it mirrors."""
        for status in ("done", "uncertain", "failed", "adb_connect_failed", "heartbeat_lost",
                       "banned", "action_block", "human_verification", "already_shared"):
            client = MagicMock()
            item = MagicMock(queue_id="recQ", account_id="recAcc", account_name="n",
                             variant_id="recV", retry_count=1, launch_id="LID")
            posting_runner.apply_post_result(client, item, status, logger=LOG)
            bumped = any(call.kwargs.get("retry_count") is not None
                         for call in client.mark_post_result.call_args_list)
            self.assertEqual(posting_runner.consumes_retry_budget(status), bumped, status)


@contextmanager
def _always_a_slot(*args, **kwargs):
    """Stand in for the global phone-ceiling slot so these tests measure launch
    accounting, not whatever else on this box is holding slots."""
    yield "slot"


class PostingRunLaunchSummaryTest(TestCase):
    """A whole run: successes, MLX 500s and our own failures mixed together."""

    def _plan(self, launch_ids):
        items = [MagicMock(launch_id=lid, account_id=f"acc-{lid}", account_name=f"Acct {lid}",
                           queue_id=f"q-{lid}", caption="c", video_path="/v.mp4",
                           variant_id="var", retry_count=0)
                 for lid in launch_ids]
        return MagicMock(to_post=items, skipped=[])

    def _run(self, responses, workflow_status=None, should_stop=None):
        """`responses` maps launch id -> the launcher's answer for it."""
        launch_ids = list(responses)
        launcher = MagicMock()
        launcher.start_profiles.side_effect = lambda ids: responses[ids[0]]
        logged = []

        def fake_workflow(launch_id, *a, **k):
            status = (workflow_status or {}).get(launch_id)
            if status and callable(k.get("status_callback")):
                k["status_callback"](launch_id, status, "")

        with patch.object(posting_runner, "run_profile_workflow", side_effect=fake_workflow), \
             patch.object(posting_runner, "apply_post_result"), \
             patch.object(posting_runner, "live_profile_slot", _always_a_slot), \
             patch.object(LOG, "info", side_effect=lambda msg, *a: logged.append(msg % a)):
            result = posting_runner._launch_and_post(
                self._plan(launch_ids), launch_ids, MagicMock(), launcher, MagicMock(),
                MagicMock(), MagicMock(), MagicMock(), LOG, 0, 1, 0, should_stop, None, None,
                "flow", max_concurrent_profiles=1,
            )
        return result, logged

    def test_mixed_run_reports_the_right_counts_and_rate(self):
        result, logged = self._run({
            "p1": OK_RESPONSE,
            "p2": OK_RESPONSE,
            "p3": MLX_500_RESPONSE,
            "p4": CONNECTION_REFUSED_RESPONSE,
        })
        self.assertEqual(result["launch_attempts"], 4)
        self.assertEqual(result["launch_ok"], 2)
        self.assertEqual(result["mlx_500"], 1)
        self.assertEqual(result["launch_failures_other"], 1)
        self.assertEqual(result["mlx_500_rate"], 0.25)

        summary = [line for line in logged if line.startswith("Posting run complete")]
        self.assertEqual(len(summary), 1, logged)          # one summary, not two
        self.assertIn("4 attempt(s)", summary[0])
        self.assertIn("1 MLX-side 500 (25.0%)", summary[0])
        self.assertIn("1 our-side/other failure(s)", summary[0])

    def test_a_500_that_costs_a_row_a_retry_is_counted(self):
        result, logged = self._run(
            {"p1": MLX_500_RESPONSE, "p2": CONNECTION_REFUSED_RESPONSE, "p3": OK_RESPONSE},
            workflow_status={"p1": "failed", "p2": "failed", "p3": "done"},
        )
        # Only p1's retry is chargeable to MultiLogin: p2 failed on our side and
        # p3 succeeded.
        self.assertEqual(result["mlx_500_retries"], 1)
        self.assertIn("1 queue retry(s) burned by MLX 500s",
                      [line for line in logged if line.startswith("Posting run complete")][0])

    def test_an_incident_does_not_count_as_a_burned_retry(self):
        result, _logged = self._run({"p1": MLX_500_RESPONSE},
                                    workflow_status={"p1": "banned"})
        self.assertEqual(result["mlx_500"], 1)
        self.assertEqual(result["mlx_500_retries"], 0)

    def test_a_run_that_launches_nothing_reports_zero_not_a_crash(self):
        result, logged = self._run({"p1": OK_RESPONSE}, should_stop=lambda: True)
        self.assertTrue(result["aborted"])
        self.assertEqual(result["launch_attempts"], 0)
        self.assertEqual(result["mlx_500_rate"], 0.0)
        self.assertTrue(any("0 MLX-side 500 (0.0%)" in line for line in logged), logged)

    def test_a_clean_run_reports_a_zero_percent_rate(self):
        result, logged = self._run({"p1": OK_RESPONSE, "p2": OK_RESPONSE})
        self.assertEqual(result["mlx_500"], 0)
        self.assertEqual(result["mlx_500_rate"], 0.0)
        self.assertTrue(any("2 attempt(s), 2 ok, 0 MLX-side 500 (0.0%)" in line
                            for line in logged), logged)


class WarmupRunLaunchSummaryTest(TestCase):
    """The warmup loop launches the same way and must report the same numbers."""

    def test_summary_and_counters_on_the_warmup_run(self):
        from adb_bot.automation import airtable_runner

        responses = {"w1": MLX_500_RESPONSE, "w2": OK_RESPONSE}
        launcher = MagicMock()
        launcher.start_profiles.side_effect = lambda ids: responses[ids[0]]
        plans = [MagicMock(launch_id=lid, account_id=f"acc-{lid}", account_name=lid,
                           runs=[MagicMock(flow="warmup_scroll")]) for lid in responses]
        plan = MagicMock(plans=plans, skipped=[])
        logged = []

        with patch.object(airtable_runner, "run_profile_workflow"), \
             patch.object(airtable_runner, "live_profile_slot", _always_a_slot), \
             patch.object(LOG, "info", side_effect=lambda msg, *a: logged.append(msg % a)):
            result = airtable_runner._launch_and_run_flows(
                plan, list(responses), MagicMock(), launcher, MagicMock(), MagicMock(),
                MagicMock(), MagicMock(), LOG, 0, 1, 0, None, None, None, False,
                max_concurrent_profiles=1,
            )

        self.assertEqual(result["launch_attempts"], 2)
        self.assertEqual(result["mlx_500"], 1)
        self.assertEqual(result["mlx_500_rate"], 0.5)
        summary = [line for line in logged if line.startswith("Airtable run complete")]
        self.assertEqual(len(summary), 1, logged)
        self.assertIn("1 MLX-side 500 (50.0%)", summary[0])


class QueueRowCostTest(TestCase):
    """The reason any of this is worth counting: these 500s are what walk a row
    to 'Retries Exhausted' while nothing is wrong with the bot."""

    def test_a_500_failure_still_bumps_the_rows_retry_count(self):
        client = MagicMock()
        item = MagicMock(queue_id="recQ", account_id="recAcc", account_name="n",
                         variant_id="recV", retry_count=2, launch_id="LID")
        posting_runner.apply_post_result(client, item, "failed", logger=LOG)
        client.mark_post_result.assert_called_with(
            "recQ", at.POST_STATUS_FAILED, at.ISSUE_NEEDS_RETRY, retry_count=3)
