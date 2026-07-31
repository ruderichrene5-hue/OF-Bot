"""A profile is closed only when its flow SUCCEEDS.

The UI flag reads "Close profile when the flow succeeds". Anything that isn't a
success -- a failed flow, an ADB connect failure, an Instagram ban/verification
flag -- must leave the profile open so it can be looked at (and, for a
verification prompt, solved by hand).
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from adb_bot.automation.workflow import run_profile_workflow
from adb_bot.core.models import Profile


class DummyLogger:
    def info(self, *a, **k):
        return None

    def warning(self, *a, **k):
        return None

    def exception(self, *a, **k):
        return None


class RecordingShutdownClient:
    def __init__(self):
        self.calls = []

    def shutdown_profiles(self, profile_ids):
        self.calls.append(list(profile_ids))
        return {"status": "ok"}


def run_with(flow_result, shutdown_client, shutdown_on_success=True, connected=True):
    class DummyFlow:
        def run(self, *a, **k):
            return flow_result

    class DummyAutomation:
        def __init__(self):
            self.flows = {"instagram_scroll": DummyFlow()}

    statuses = []

    with patch("adb_bot.automation.workflow.prepare_profile_for_adb",
               return_value=Profile(id="profile-1", status="ready")), \
         patch("adb_bot.automation.workflow.connect_with_retries",
               return_value="device-1" if connected else None), \
         patch("adb_bot.automation.workflow.ADBClient") as mock_adb:
        mock_adb.return_value.run_command.return_value = ""
        run_profile_workflow(
            "profile-1", "token", SimpleNamespace(), SimpleNamespace(),
            shutdown_client, DummyAutomation(), DummyLogger(),
            flow_name="instagram_scroll",
            shutdown_on_success=shutdown_on_success,
            status_callback=lambda pid, status: statuses.append(status),
        )
    return statuses


class KeepFailedProfilesOpenTest(unittest.TestCase):
    def test_success_closes_the_profile(self):
        client = RecordingShutdownClient()
        statuses = run_with({"success": True}, client)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("done", statuses)

    def test_failed_flow_keeps_the_profile_open(self):
        client = RecordingShutdownClient()
        statuses = run_with({"success": False}, client)
        self.assertEqual(client.calls, [])
        self.assertIn("failed", statuses)

    def test_adb_connect_failure_keeps_the_profile_open(self):
        # Previously closed unconditionally -- now kept so the connection
        # problem can be diagnosed on the running profile.
        client = RecordingShutdownClient()
        statuses = run_with({"success": True}, client, connected=False)
        self.assertEqual(client.calls, [])
        self.assertIn("adb_connect_failed", statuses)

    def test_ban_flag_keeps_the_profile_open(self):
        client = RecordingShutdownClient()
        statuses = run_with({"account_flag": "banned"}, client)
        self.assertEqual(client.calls, [])
        self.assertIn("banned", statuses)

    def test_human_verification_keeps_the_profile_open(self):
        # The most important case: a human has to solve the challenge, which
        # they can't do on a profile the bot just closed.
        client = RecordingShutdownClient()
        statuses = run_with({"account_flag": "human_verification"}, client)
        self.assertEqual(client.calls, [])
        self.assertIn("human_verification", statuses)

    def test_legacy_human_verification_key_also_keeps_it_open(self):
        client = RecordingShutdownClient()
        run_with({"human_verification": True}, client)
        self.assertEqual(client.calls, [])

    def test_action_block_keeps_the_profile_open(self):
        client = RecordingShutdownClient()
        run_with({"account_flag": "action_block"}, client)
        self.assertEqual(client.calls, [])

    def test_already_had_bio_follows_the_setting(self):
        # Not a failure -- the work was simply already done -- so it closes like
        # any other pass when the flag is on...
        client = RecordingShutdownClient()
        statuses = run_with({"already_has_bio": True}, client, shutdown_on_success=True)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("already_had_bio", statuses)

    def test_already_had_bio_stays_open_when_setting_is_off(self):
        # ...and respects the flag being off, which it used to ignore.
        client = RecordingShutdownClient()
        run_with({"already_has_bio": True}, client, shutdown_on_success=False)
        self.assertEqual(client.calls, [])

    def test_success_with_setting_off_keeps_it_open(self):
        client = RecordingShutdownClient()
        run_with({"success": True}, client, shutdown_on_success=False)
        self.assertEqual(client.calls, [])


class RunnerLevelShutdownTest(unittest.TestCase):
    """The lifecycle runner closes an account's profile after its flows -- but
    only when none of them failed."""

    def _source(self):
        import inspect
        from adb_bot.automation import airtable_runner
        return inspect.getsource(airtable_runner)

    def test_runner_tracks_failures_before_closing(self):
        src = self._source()
        self.assertIn("had_failure", src)
        # The close must be guarded by the failure flag, not just the setting.
        self.assertIn("if had_failure:", src)

    def test_failed_status_sets_the_flag(self):
        src = self._source()
        self.assertIn("if result == RESULT_FAILED:", src)
        self.assertIn("had_failure = True", src)
