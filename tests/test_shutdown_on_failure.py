"""Every profile's phone is closed on the way out, whatever the flow did.

This replaces the older policy, which closed a profile only when its flow
SUCCEEDED and left failures, ADB connect errors and ban/verification flags open
"so they can be looked at". That assumed a person was watching. Every caller is
now a headless loop on a systemd timer, so nobody looks -- and on 2026-08-04 it
came to 172 launches against 64 shutdowns. The ~108 phones left behind, at
~150 MB of WebKitWebProcess each, filled a 15 GB box twice and the OOM killer
took out the MultiLogin agent with them.

`shutdown_on_success` still decides whether the *flow* closes its own profile
(the UI flag "Close profile when the flow succeeds" is unchanged). The guard in
`_guarantee_profile_closed` then closes anything the flow left open, so exactly
one shutdown call is made either way. To inspect a flagged account, open its
profile in MultiLogin directly -- the Airtable row records why it was flagged.
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


class EveryProfileGetsClosedTest(unittest.TestCase):
    def test_success_closes_the_profile(self):
        client = RecordingShutdownClient()
        statuses = run_with({"success": True}, client)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("done", statuses)

    def test_failed_flow_still_closes_the_profile(self):
        client = RecordingShutdownClient()
        statuses = run_with({"success": False}, client)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("failed", statuses)

    def test_adb_connect_failure_still_closes_the_profile(self):
        # A phone we cannot even reach over ADB is the least useful one to
        # leave running.
        client = RecordingShutdownClient()
        statuses = run_with({"success": True}, client, connected=False)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("adb_connect_failed", statuses)

    def test_ban_flag_still_closes_the_profile(self):
        client = RecordingShutdownClient()
        statuses = run_with({"account_flag": "banned"}, client)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("banned", statuses)

    def test_human_verification_still_closes_the_profile(self):
        # The case the old policy cared most about. Keeping the phone up did
        # not actually help: these run unattended overnight, so the challenge
        # was never solved on the open profile -- it just leaked. The flag is
        # recorded on the Airtable row, and the profile can be opened by hand
        # in MultiLogin when someone gets to it.
        client = RecordingShutdownClient()
        statuses = run_with({"account_flag": "human_verification"}, client)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("human_verification", statuses)

    def test_legacy_human_verification_key_also_closes_it(self):
        client = RecordingShutdownClient()
        run_with({"human_verification": True}, client)
        self.assertEqual(client.calls, [["profile-1"]])

    def test_action_block_still_closes_the_profile(self):
        client = RecordingShutdownClient()
        run_with({"account_flag": "action_block"}, client)
        self.assertEqual(client.calls, [["profile-1"]])

    def test_already_had_bio_follows_the_setting(self):
        # Not a failure -- the work was simply already done -- so it closes like
        # any other pass when the flag is on...
        client = RecordingShutdownClient()
        statuses = run_with({"already_has_bio": True}, client, shutdown_on_success=True)
        self.assertEqual(client.calls, [["profile-1"]])
        self.assertIn("already_had_bio", statuses)

    def test_already_had_bio_is_closed_by_the_guard_when_setting_is_off(self):
        # The flow itself leaves it open (the flag is off); the guard closes it.
        client = RecordingShutdownClient()
        run_with({"already_has_bio": True}, client, shutdown_on_success=False)
        self.assertEqual(client.calls, [["profile-1"]])

    def test_success_with_setting_off_is_still_closed_by_the_guard(self):
        # `shutdown_on_success=False` means the FLOW does not close it. The
        # guard still does, so no phone outlives its run either way.
        client = RecordingShutdownClient()
        run_with({"success": True}, client, shutdown_on_success=False)
        self.assertEqual(client.calls, [["profile-1"]])


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
