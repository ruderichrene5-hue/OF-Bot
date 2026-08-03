"""Readiness must stop repeating a call that cannot succeed.

From a real 56-profile run: Multilogin answers `42002 profile is not running`
while a profile is still booting -- every profile hit it at least once, and
healthy ones cleared it within 11 attempts. But six profiles never started at
all, and for those the loop kept calling `enable_adb` 15 times over ~4 minutes.
Enabling ADB on a stopped profile can never work and nothing in that loop
restarted it, so the outcome was fixed from the first attempt; the remaining
retries just held a concurrency slot while confirming it.
"""

from unittest import TestCase
from unittest.mock import MagicMock

from adb_bot.automation.workflow import (
    MLX_PROFILE_NOT_RUNNING_CODE,
    _enable_reported_not_running,
    prepare_profile_for_adb,
)

PROFILE = "62642224128176960"


def not_running(profile_id=PROFILE) -> dict:
    return {"data": {"fail_amount": 1, "success_amount": 0,
                     "fail_details": [{"code": MLX_PROFILE_NOT_RUNNING_CODE, "id": profile_id,
                                       "msg": "profile is not running; ADB toggle skipped"}]},
            "status": {"http_code": 200, "message": "success"}}


def enable_ok() -> dict:
    return {"data": {"fail_amount": 0, "success_amount": 1, "fail_details": [],
                     "success_details": [{"id": PROFILE}]},
            "status": {"http_code": 200, "message": "success"}}


class ParseNotRunningTest(TestCase):
    def test_detects_the_documented_code(self):
        self.assertTrue(_enable_reported_not_running(not_running(), PROFILE))

    def test_detects_the_message_without_the_code(self):
        response = {"data": {"fail_details": [{"id": PROFILE, "msg": "Profile is NOT RUNNING"}]}}
        self.assertTrue(_enable_reported_not_running(response, PROFILE))

    def test_a_successful_enable_is_not_not_running(self):
        self.assertFalse(_enable_reported_not_running(enable_ok(), PROFILE))

    def test_another_profiles_failure_is_ignored(self):
        self.assertFalse(_enable_reported_not_running(not_running("someone-else"), PROFILE))

    def test_junk_is_no_opinion_rather_than_a_failure(self):
        for junk in (None, {}, "", {"data": None}, {"data": {"fail_details": ["nope"]}}):
            self.assertFalse(_enable_reported_not_running(junk, PROFILE))


class ReadinessLoopTest(TestCase):
    def setUp(self):
        self.logger = MagicMock()
        self.adb_enable = MagicMock()
        self.api = MagicMock()
        # Credentials never come back ready unless a test says otherwise.
        self.api.fetch_adb_credentials.return_value = {}

    def _prepare(self, **kwargs):
        kwargs.setdefault("max_attempts", 15)
        kwargs.setdefault("wait_seconds", 0)
        kwargs.setdefault("relaunch_after_attempts", 12)
        return prepare_profile_for_adb(
            PROFILE, self.api, self.adb_enable, self.logger, **kwargs)

    def test_a_dead_profile_is_relaunched_rather_than_re_enabled(self):
        self.adb_enable.enable_adb.return_value = not_running()
        launcher = MagicMock()
        self._prepare(launcher_client=launcher)
        launcher.start_profiles.assert_called_once_with([PROFILE])

    def test_the_relaunch_gets_a_fresh_budget(self):
        """A relaunched profile needs the same boot time as a fresh one; giving
        it only the leftover attempts would waste the relaunch."""
        self.adb_enable.enable_adb.return_value = not_running()
        launcher = MagicMock()
        self._prepare(max_attempts=15, relaunch_after_attempts=12, launcher_client=launcher)
        # 12 to trigger the relaunch, then a further 12 before giving up.
        self.assertEqual(self.adb_enable.enable_adb.call_count, 24)

    def test_it_gives_up_early_without_a_launcher(self):
        # No way to fix it, so don't spend the rest of the budget proving it.
        self.adb_enable.enable_adb.return_value = not_running()
        self._prepare(max_attempts=15, relaunch_after_attempts=12)
        self.assertEqual(self.adb_enable.enable_adb.call_count, 12)

    def test_it_relaunches_only_once(self):
        self.adb_enable.enable_adb.return_value = not_running()
        launcher = MagicMock()
        self._prepare(max_attempts=15, relaunch_after_attempts=12, launcher_client=launcher)
        self.assertEqual(launcher.start_profiles.call_count, 1)

    def test_a_slow_but_healthy_boot_is_never_disturbed(self):
        """The threshold is drawn from real data: healthy profiles cleared
        within 11 attempts, so 12 must not touch them."""
        self.adb_enable.enable_adb.side_effect = [not_running()] * 11 + [enable_ok()] * 4
        launcher = MagicMock()
        self._prepare(max_attempts=15, relaunch_after_attempts=12, launcher_client=launcher)
        launcher.start_profiles.assert_not_called()

    def test_a_recovered_profile_resets_the_counter(self):
        # not-running, then running, then not-running again must not add up to
        # a relaunch -- only a sustained run of them means the launch failed.
        self.adb_enable.enable_adb.side_effect = (
            [not_running()] * 8 + [enable_ok()] + [not_running()] * 6)
        launcher = MagicMock()
        self._prepare(max_attempts=15, relaunch_after_attempts=12, launcher_client=launcher)
        launcher.start_profiles.assert_not_called()

    def test_a_relaunch_that_raises_does_not_abort_readiness(self):
        self.adb_enable.enable_adb.return_value = not_running()
        launcher = MagicMock()
        launcher.start_profiles.side_effect = RuntimeError("launcher down")
        self.assertIsNone(self._prepare(launcher_client=launcher))
        self.assertGreater(self.adb_enable.enable_adb.call_count, 12)

    def test_a_ready_profile_returns_immediately(self):
        from unittest.mock import patch
        from adb_bot.core.models import Profile
        self.adb_enable.enable_adb.return_value = enable_ok()
        launcher = MagicMock()
        ready = Profile(id=PROFILE, status="active", ip="1.2.3.4", port="5555", pwd="secret")
        with patch("adb_bot.automation.workflow.parse_profiles_from_response",
                   return_value=[ready]):
            result = self._prepare(launcher_client=launcher)
        self.assertIsNotNone(result)
        self.assertEqual(self.adb_enable.enable_adb.call_count, 1, "did not stop once ready")
        launcher.start_profiles.assert_not_called()


class WiringTest(TestCase):
    """The relaunch only helps if callers actually hand over a launcher."""

    def test_every_caller_passes_a_launcher_client(self):
        import inspect
        from adb_bot.automation import airtable_runner, posting_runner, run_loop
        from adb_bot.ui import ui
        for module in (posting_runner, airtable_runner, run_loop, ui):
            src = inspect.getsource(module)
            head = src.split("run_profile_workflow(", 1)
            self.assertIn("launcher_client=", src,
                          f"{module.__name__} calls run_profile_workflow without a launcher")
            self.assertGreater(len(head), 1, f"{module.__name__} lost its workflow call")
