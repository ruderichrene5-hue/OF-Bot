import unittest
from types import SimpleNamespace
from unittest.mock import patch

from adb_bot.automation.workflow import run_profile_workflow
from adb_bot.core.models import Profile


class DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


class WorkflowShutdownOnSuccessTest(unittest.TestCase):
    def test_shuts_down_profile_after_success_when_enabled(self):
        shutdown_calls = []

        class DummyShutdownClient:
            def shutdown_profiles(self, profile_ids):
                shutdown_calls.append(list(profile_ids))
                return {"status": "ok"}

        class DummyFlow:
            def run(self, *args, **kwargs):
                return {"ok": True}

        class DummyAutomation:
            def __init__(self):
                self.flows = {"instagram_scroll": DummyFlow()}

        with patch("adb_bot.automation.workflow.prepare_profile_for_adb", return_value=Profile(id="profile-1", status="ready")), \
             patch("adb_bot.automation.workflow.connect_with_retries", return_value="device-1"), \
             patch("adb_bot.automation.workflow.ADBClient") as mock_adb_client:
            mock_adb_client.return_value.run_command.return_value = ""

            run_profile_workflow(
                "profile-1",
                "token",
                SimpleNamespace(),
                SimpleNamespace(),
                DummyShutdownClient(),
                DummyAutomation(),
                DummyLogger(),
                flow_name="instagram_scroll",
                shutdown_on_success=True,
            )

        self.assertEqual(shutdown_calls, [["profile-1"]])

    def test_does_not_shut_down_profile_after_success_when_disabled(self):
        shutdown_calls = []

        class DummyShutdownClient:
            def shutdown_profiles(self, profile_ids):
                shutdown_calls.append(list(profile_ids))
                return {"status": "ok"}

        class DummyFlow:
            def run(self, *args, **kwargs):
                return {"ok": True}

        class DummyAutomation:
            def __init__(self):
                self.flows = {"instagram_scroll": DummyFlow()}

        with patch("adb_bot.automation.workflow.prepare_profile_for_adb", return_value=Profile(id="profile-1", status="ready")), \
             patch("adb_bot.automation.workflow.connect_with_retries", return_value="device-1"), \
             patch("adb_bot.automation.workflow.ADBClient") as mock_adb_client:
            mock_adb_client.return_value.run_command.return_value = ""

            run_profile_workflow(
                "profile-1",
                "token",
                SimpleNamespace(),
                SimpleNamespace(),
                DummyShutdownClient(),
                DummyAutomation(),
                DummyLogger(),
                flow_name="instagram_scroll",
                shutdown_on_success=False,
            )

        self.assertEqual(shutdown_calls, [])


if __name__ == "__main__":
    unittest.main()
