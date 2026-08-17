"""A failed post must say what failed, in the place a person reads.

From 2026-08-17: profile "nikki 5" (@nikki.kie20) failed four posts in one day
for four unrelated reasons -- MultiLogin refused the launch on a dead proxy, the
account switcher would not open, ADB would not connect, and MultiLogin's cloud
threw a 500 -- and every one of them landed in Airtable as the identical
sentence "flow reported a failure (see app logs)". Five retries later the row
was parked as "Retries Exhausted", a counter wearing the label of a diagnosis.

The reason was in the app log the whole time; nothing carried it the last inch.
These tests pin that inch: the readiness wait reports why it gave up, the flow
returns say which step lost it, and the generic placeholder steps aside once a
real reason exists rather than being stapled in front of it.
"""

from unittest import TestCase
from unittest.mock import MagicMock

from adb_bot.automation.posting_runner import GENERIC_FAILURE_NOTE, apply_post_result
from adb_bot.automation.workflow import (
    MLX_PROFILE_NOT_RUNNING_CODE,
    prepare_profile_for_adb,
    status_detail,
)
from adb_bot.clients.multilogin import describe_launch_failure
from adb_bot.clients import airtable as at

from tests.test_posting_queue import ITEM, FakePostClient

PROFILE = "625105025496056151"


def not_running() -> dict:
    return {"data": {"fail_amount": 1, "success_amount": 0,
                     "fail_details": [{"code": MLX_PROFILE_NOT_RUNNING_CODE, "id": PROFILE,
                                       "msg": "profile is not running; ADB toggle skipped"}]},
            "status": {"http_code": 200, "message": "success"}}


# The two answers that actually stopped posts on 2026-08-17, verbatim in shape.
MLX_500 = {"status": "error",
           "error": "500 Server Error: Internal Server Error for url: "
                    "https://launcher.mlx.yt:45001/api/v1/mobile_phone/launch",
           "response_text": '{"status":{"error_code":"INTERNAL_SERVER_ERROR","http_code":500,'
                            '"message":"failed to get profiles starting urls"}}',
           "status_code": 500}

DEAD_PROXY = {"data": {"fail_amount": 1, "success_amount": 0,
                       "fail_details": [{"code": 45010, "id": PROFILE,
                                         "msg": "Proxy connection failed"}]},
              "status": {"http_code": 200, "message": "success"}}


class DescribeLaunchFailureTest(TestCase):
    def test_their_500_is_named_as_theirs(self):
        described = describe_launch_failure(MLX_500)
        self.assertIn("failed to get profiles start urls", described)
        self.assertIn("their side", described)

    def test_a_dead_proxy_quotes_multilogins_own_words(self):
        self.assertIn("Proxy connection failed", describe_launch_failure(DEAD_PROXY))

    def test_a_real_launch_describes_nothing(self):
        ok = {"data": {"fail_amount": 0, "success_amount": 1, "fail_details": []},
              "status": {"http_code": 200, "message": "success"}}
        self.assertEqual(describe_launch_failure(ok), "")

    def test_a_raised_error_is_described_rather_than_swallowed(self):
        described = describe_launch_failure(None, error=ConnectionRefusedError("no launcher"))
        self.assertIn("ConnectionRefusedError", described)
        self.assertIn("no launcher", described)


class ReadinessFailureReasonTest(TestCase):
    def setUp(self):
        self.logger = MagicMock()
        self.adb_enable = MagicMock()
        self.api = MagicMock()
        self.api.fetch_adb_credentials.return_value = {}
        self.failure: dict = {}

    def _prepare(self, **kwargs):
        kwargs.setdefault("max_attempts", 4)
        kwargs.setdefault("wait_seconds", 0)
        return prepare_profile_for_adb(
            PROFILE, self.api, self.adb_enable, self.logger,
            failure_out=self.failure, **kwargs)

    def test_never_running_says_so_and_says_no_post_was_attempted(self):
        self.adb_enable.enable_adb.return_value = not_running()
        self.assertIsNone(self._prepare())
        reason = self.failure["reason"]
        self.assertIn("never reported the phone running", reason)
        self.assertIn("no post was attempted", reason.lower())

    def test_the_relaunch_answer_is_carried_into_the_reason(self):
        """The whole point: 'not running' fourteen times looks the same whether
        their cloud threw a 500 or the profile's proxy is dead, and the two have
        opposite fixes."""
        self.adb_enable.enable_adb.return_value = not_running()
        launcher = MagicMock()
        launcher.start_profiles.return_value = MLX_500
        self.assertIsNone(self._prepare(launcher_client=launcher))
        reason = self.failure["reason"]
        self.assertIn("a relaunch did not take", reason)
        self.assertIn("failed to get profiles start urls", reason)

    def test_a_dead_proxy_is_distinguishable_from_their_500(self):
        self.adb_enable.enable_adb.return_value = not_running()
        launcher = MagicMock()
        launcher.start_profiles.return_value = DEAD_PROXY
        self.assertIsNone(self._prepare(launcher_client=launcher))
        self.assertIn("Proxy connection failed", self.failure["reason"])

    def test_running_but_no_credentials_is_a_different_sentence(self):
        """The phone came up, so this is not a launch problem -- reporting it as
        one sends someone to look at MultiLogin for nothing."""
        self.adb_enable.enable_adb.return_value = {
            "data": {"fail_amount": 0, "success_amount": 1, "fail_details": []},
            "status": {"http_code": 200, "message": "success"}}
        self.assertIsNone(self._prepare())
        reason = self.failure["reason"]
        self.assertIn("never handed over ADB credentials", reason)
        self.assertNotIn("never reported the phone running", reason)

    def test_a_ready_profile_leaves_no_reason_behind(self):
        self.adb_enable.enable_adb.return_value = not_running()
        # Set on parse_profiles, not on the raw response: a MagicMock api answers
        # parse_profiles with an empty-iterating mock, which would shadow it.
        self.api.parse_profiles.return_value = [{"id": PROFILE, "status": "ready"}]
        prepared = self._prepare()
        self.assertIsNotNone(prepared)
        self.assertEqual(self.failure, {})

    def test_the_out_dict_is_optional(self):
        """Callers that do not care -- and the tests that patch this wholesale --
        must keep working."""
        self.adb_enable.enable_adb.return_value = not_running()
        self.assertIsNone(prepare_profile_for_adb(
            PROFILE, self.api, self.adb_enable, self.logger,
            max_attempts=2, wait_seconds=0))


class StatusDetailTest(TestCase):
    def test_a_failure_reason_becomes_the_detail(self):
        self.assertEqual(
            status_detail({"failure_reason": "the reel composer would not open"}),
            "the reel composer would not open")

    def test_it_does_not_displace_a_verification_signal(self):
        detail = status_detail({"verify_method": "post_count", "verify_strength": "strong",
                                "verify_detail": "post count 99 -> 100",
                                "failure_reason": "something else"})
        self.assertIn("via post_count [strong]: post count 99 -> 100", detail)
        self.assertIn("something else", detail)

    def test_no_reason_still_renders_empty(self):
        self.assertEqual(status_detail({"aborted": False, "success": False}), "")


class FailureNoteTest(TestCase):
    def test_a_real_reason_replaces_the_placeholder_rather_than_trailing_it(self):
        client = FakePostClient()
        apply_post_result(client, ITEM, "failed",
                          detail="adb push of nikki_5.mp4 to the phone failed")
        note = client.run_logs[0][3]
        self.assertEqual(note, "adb push of nikki_5.mp4 to the phone failed")
        self.assertNotIn(GENERIC_FAILURE_NOTE, note)

    def test_without_a_reason_the_placeholder_still_says_something(self):
        client = FakePostClient()
        apply_post_result(client, ITEM, "failed")
        self.assertEqual(client.run_logs[0][3], GENERIC_FAILURE_NOTE)

    def test_a_non_placeholder_note_still_keeps_its_detail_in_brackets(self):
        """`done` and `uncertain` notes read as "<what> (<how>)" and existing
        Airtable rows are full of them -- that shape must not change."""
        client = FakePostClient()
        apply_post_result(client, ITEM, "done",
                          detail="via post_count [strong]: post count 99 -> 100")
        note = client.run_logs[0][3]
        self.assertTrue(note.startswith("posted ("))
        self.assertIn("post count 99 -> 100", note)

    def test_the_failure_still_bumps_the_retry_count(self):
        """A better note must not quietly change what the row does next."""
        client = FakePostClient()
        apply_post_result(client, ITEM, "failed", detail="the reel composer would not open")
        _qid, status, issue, retry = client.post_marks[0]
        self.assertEqual(status, at.POST_STATUS_FAILED)
        self.assertEqual(issue, at.ISSUE_NEEDS_RETRY)
        self.assertEqual(retry, 2)
