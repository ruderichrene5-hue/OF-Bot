from unittest import TestCase

from adb_bot.automation.heartbeat import MAX_CONSECUTIVE_API_ERRORS, ProfileHeartbeat
from adb_bot.core.models import Profile

PROFILE_ID = "123456789"
TARGET = "10.0.0.1:5555"
OTHER_TARGET = "10.0.0.1:6666"


class FakeAdb:
    def __init__(self, state="device"):
        self.state = state

    def get_state(self, target):
        return self.state


class FakeApi:
    """Stands in for MultiloginApiClient. `parse_profiles` is the hook
    parse_profiles_from_response uses, so returning Profiles here exercises the
    real helper chain."""

    def __init__(self, profiles=None, raises=False):
        self.profiles = profiles if profiles is not None else [_ready_profile()]
        self.raises = raises
        self.calls = 0

    def fetch_adb_credentials(self, ids):
        self.calls += 1
        if self.raises:
            raise RuntimeError("multilogin unreachable")
        return {"data": {}}

    def parse_profiles(self, response):
        return self.profiles


class FakeShutdown:
    def __init__(self):
        self.calls = []

    def shutdown_profiles(self, ids):
        self.calls.append(list(ids))
        return {"status": "ok"}


def _ready_profile(target=TARGET):
    ip, port = target.split(":")
    return Profile(id=PROFILE_ID, status="active", ip=ip, port=port, pwd="pw")


def _build(adb=None, api=None, shutdown=None, interval=60, inner=None, shutdown_on_failure=True):
    return ProfileHeartbeat(
        PROFILE_ID, TARGET,
        api or FakeApi(),
        adb or FakeAdb(),
        shutdown or FakeShutdown(),
        logger=None,
        interval_seconds=interval,
        inner_should_stop=inner,
        shutdown_on_failure=shutdown_on_failure,
    )


class ThrottlingTest(TestCase):
    def test_does_not_check_before_the_interval_elapses(self):
        api = FakeApi()
        hb = _build(api=api, interval=60)
        for _ in range(50):
            self.assertFalse(hb())
        # The flows poll should_stop constantly; that must not become 50 API calls.
        self.assertEqual(api.calls, 0)

    def test_checks_once_the_interval_has_elapsed(self):
        api = FakeApi()
        hb = _build(api=api, interval=60)
        hb._last_check -= 61
        self.assertFalse(hb())
        self.assertEqual(api.calls, 1)


class InnerStopTest(TestCase):
    def test_inner_stop_wins_immediately(self):
        api = FakeApi()
        hb = _build(api=api, inner=lambda: True)
        self.assertTrue(hb())
        # No need to touch the network to honour the caller's own stop signal.
        self.assertEqual(api.calls, 0)

    def test_broken_inner_callback_does_not_wedge_the_run(self):
        def boom():
            raise RuntimeError("bad callback")

        hb = _build(inner=boom)
        self.assertFalse(hb())


class AdbCheckTest(TestCase):
    def test_offline_device_trips_and_shuts_down(self):
        shutdown = FakeShutdown()
        hb = _build(adb=FakeAdb("offline"), shutdown=shutdown)
        self.assertTrue(hb.check_now())
        self.assertTrue(hb.stopped)
        self.assertIn("offline", hb.reason)
        self.assertEqual(shutdown.calls, [[PROFILE_ID]])

    def test_missing_device_trips(self):
        hb = _build(adb=FakeAdb(""))
        self.assertTrue(hb.check_now())
        self.assertIn("gone", hb.reason)

    def test_shutdown_can_be_disabled(self):
        shutdown = FakeShutdown()
        hb = _build(adb=FakeAdb("offline"), shutdown=shutdown, shutdown_on_failure=False)
        self.assertTrue(hb.check_now())
        self.assertEqual(shutdown.calls, [])


class OwnershipTest(TestCase):
    def test_endpoint_change_trips(self):
        # The exact takeover case: the phone came back at a new address because
        # somebody opened the profile elsewhere.
        api = FakeApi(profiles=[_ready_profile(OTHER_TARGET)])
        shutdown = FakeShutdown()
        hb = _build(api=api, shutdown=shutdown)
        self.assertTrue(hb.check_now())
        self.assertIn("taken it over", hb.reason)
        self.assertEqual(shutdown.calls, [[PROFILE_ID]])

    def test_profile_no_longer_listed_trips(self):
        hb = _build(api=FakeApi(profiles=[]))
        self.assertTrue(hb.check_now())
        self.assertIn("no longer lists", hb.reason)

    def test_profile_not_ready_trips(self):
        stopped = Profile(id=PROFILE_ID, status="stopped", ip="10.0.0.1", port="5555", pwd="pw")
        hb = _build(api=FakeApi(profiles=[stopped]))
        self.assertTrue(hb.check_now())
        self.assertIn("no longer running", hb.reason)

    def test_same_endpoint_and_running_passes(self):
        hb = _build()
        self.assertFalse(hb.check_now())
        self.assertFalse(hb.stopped)


class ApiErrorToleranceTest(TestCase):
    def test_single_api_error_does_not_trip(self):
        hb = _build(api=FakeApi(raises=True))
        self.assertFalse(hb.check_now())
        self.assertFalse(hb.stopped)

    def test_sustained_api_errors_trip(self):
        hb = _build(api=FakeApi(raises=True))
        for _ in range(MAX_CONSECUTIVE_API_ERRORS - 1):
            hb.check_now()
        self.assertTrue(hb.check_now())
        self.assertIn("could not be reached", hb.reason)

    def test_error_streak_resets_after_a_good_check(self):
        api = FakeApi(raises=True)
        hb = _build(api=api)
        hb.check_now()
        api.raises = False
        self.assertFalse(hb.check_now())
        api.raises = True
        # Streak restarted, so one more failure must not trip it.
        self.assertFalse(hb.check_now())


class LatchTest(TestCase):
    def test_stays_stopped_and_shuts_down_only_once(self):
        shutdown = FakeShutdown()
        api = FakeApi(profiles=[])
        hb = _build(api=api, shutdown=shutdown)
        self.assertTrue(hb.check_now())
        calls_after_trip = api.calls
        for _ in range(10):
            self.assertTrue(hb())
        # Latched: no further probing, and the phone is not shut down repeatedly.
        self.assertEqual(api.calls, calls_after_trip)
        self.assertEqual(len(shutdown.calls), 1)


class TerminalStatusMappingTest(TestCase):
    """A new terminal status that no mapper knows about produces NO Airtable
    write-back at all -- a silent failure. Both runners must handle it."""

    def test_airtable_runner_maps_heartbeat_lost_to_failed(self):
        from adb_bot.automation.airtable_runner import _map_terminal_status, _status_to_airtable_fields

        mapped = _map_terminal_status("heartbeat_lost")
        self.assertIsNotNone(mapped)
        result, note, incident = mapped
        self.assertIn("heartbeat", note)
        # Not an Instagram incident -- the account did nothing wrong.
        self.assertIsNone(incident)
        self.assertNotEqual(_status_to_airtable_fields("heartbeat_lost"), {})

    def test_posting_runner_maps_heartbeat_lost_to_retryable(self):
        from adb_bot.automation.posting_runner import _map_post_status
        from adb_bot.clients import airtable as at

        mapped = _map_post_status("heartbeat_lost")
        self.assertIsNotNone(mapped)
        post_status, issue, incident, run_result, note = mapped
        self.assertEqual(post_status, at.POST_STATUS_FAILED)
        self.assertEqual(issue, at.ISSUE_NEEDS_RETRY)
        self.assertIsNone(incident)
