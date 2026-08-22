"""Bringing a Geelark phone up, in the one order the API allows.

Every rule pinned here was found by running it against the live API, and each
one fails *silently* if got wrong -- the phone looks broken rather than the
sequence looking wrong:

* ADB cannot be enabled on a stopped phone. Try it and every later call answers
  `42002 phone is not running`, which reads as a sick phone.
* `status: 0` means **started**, not stopped. The enum reads backwards, and
  reading it the obvious way makes a running phone look idle -- while it is
  being billed by the minute.
* Enabling ADB is asynchronous, so the port is not readable straight away.
* A phone that never becomes ready must return None rather than a half-filled
  `Profile`, or the caller will `adb connect` to nothing and blame the phone.
"""

import unittest
from unittest.mock import patch

from adb_bot.clients.geelark import readiness
from adb_bot.clients.geelark.transport import BatchOutcome, GeelarkTransport


class FakeTransport(GeelarkTransport):
    """Records the order of calls, which is the thing under test."""

    def __init__(self, responses):
        super().__init__(app_id="a", api_key="k")
        self.responses = responses
        self.calls = []

    def post(self, path, payload=None):
        self.calls.append(path)
        value = self.responses.get(path)
        if callable(value):
            return value(len([c for c in self.calls if c == path]))
        return value or {}

    @property
    def paths(self):
        return self.calls


def _status(value):
    return {"totalAmount": 1, "successAmount": 1, "failAmount": 0,
            "successDetails": [{"id": "p1", "status": value}]}


def _adb_ready():
    return {"items": [{"id": "p1", "ip": "1.2.3.4", "port": 20211, "pwd": "pw"}]}


def _adb_off():
    return {"items": [{"id": "p1", "code": 49001, "msg": "ADB did not opened"}]}


class _NoSleep(unittest.TestCase):
    """Never really sleep.

    These waits are 3s and 5s against 60s and 180s deadlines, so a test that
    reaches a poll loop with a live `sleep` costs minutes. Patching it per-test
    was already got wrong once here -- the file took three minutes because one
    path still slept -- so it is patched for every test in the class instead.
    """

    def setUp(self):
        patcher = patch.object(readiness.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class ReadinessOrderTest(_NoSleep):
    def test_a_started_phone_is_not_started_again(self):
        """Starting an already-running phone is a wasted call, and on a
        per-minute host wasted calls are wasted money."""
        transport = FakeTransport({
            "/phone/status": _status(0),          # 0 == started
            "/adb/setStatus": {},
            "/adb/getData": _adb_ready(),
        })
        profile = readiness.prepare_geelark_profile_for_adb("p1", transport)
        self.assertIsNotNone(profile)
        self.assertNotIn("/phone/start", transport.paths)

    def test_a_stopped_phone_is_started_before_adb_is_touched(self):
        """The whole ordering rule in one test: start, *then* enable ADB."""
        seen = {"n": 0}

        def status(call_number):
            # First read says stopped, second says started.
            return _status(2) if call_number == 1 else _status(0)

        transport = FakeTransport({
            "/phone/status": status,
            "/phone/start": {"totalAmount": 1, "successAmount": 1, "failAmount": 0,
                             "successDetails": [{"id": "p1"}]},
            "/adb/setStatus": {},
            "/adb/getData": _adb_ready(),
        })
        profile = readiness.prepare_geelark_profile_for_adb("p1", transport)

        self.assertIsNotNone(profile)
        self.assertLess(transport.paths.index("/phone/start"),
                        transport.paths.index("/adb/setStatus"),
                        "ADB was enabled before the phone was started")

    def test_status_two_is_stopped_so_it_gets_started(self):
        """Guards the backwards enum from the other direction: 2 must not be
        mistaken for 'started' and skipped."""
        transport = FakeTransport({
            # Stopped on the first read, started once it has been started.
            # It must flip: a status that stays 2 for ever makes
            # `wait_until_started` spin against its wall-clock deadline, which
            # no-op'd sleep does not shorten.
            "/phone/status": lambda n: _status(2) if n == 1 else _status(0),
            "/phone/start": {"totalAmount": 1, "successAmount": 1, "failAmount": 0,
                             "successDetails": [{"id": "p1"}]},
            "/adb/setStatus": {},
            "/adb/getData": _adb_ready(),
        })
        readiness.prepare_geelark_profile_for_adb("p1", transport)
        self.assertIn("/phone/start", transport.paths)

    def test_a_phone_that_never_starts_gives_up_at_its_deadline(self):
        """A phone stuck 'starting' must not be waited on for ever, and must
        not go on to have ADB enabled on it."""
        transport = FakeTransport({
            "/phone/status": _status(1),          # starting, for ever
            "/phone/start": {"totalAmount": 1, "successAmount": 1, "failAmount": 0,
                             "successDetails": [{"id": "p1"}]},
        })
        ok = readiness.wait_until_started("p1", transport, timeout_seconds=0)
        self.assertFalse(ok)
        self.assertNotIn("/adb/setStatus", transport.paths)

    def test_a_refused_start_gives_up_rather_than_enabling_adb(self):
        """`/phone/start` answers `code: 0` while starting nothing, so the
        refusal is only visible in the details -- and enabling ADB on a phone
        that never started cannot work."""
        transport = FakeTransport({
            "/phone/status": _status(2),
            "/phone/start": {"totalAmount": 1, "successAmount": 0, "failAmount": 1,
                             "failDetails": [{"id": "p1", "code": 42001,
                                              "msg": "env not found"}]},
        })
        profile = readiness.prepare_geelark_profile_for_adb("p1", transport)
        self.assertIsNone(profile)
        self.assertNotIn("/adb/setStatus", transport.paths)

    def test_adb_never_ready_returns_none_not_a_half_profile(self):
        """A Profile without ip/port/pwd would be `adb connect`-ed to nothing."""
        transport = FakeTransport({
            "/phone/status": _status(0),
            "/adb/setStatus": {},
            "/adb/getData": _adb_off(),
        })
        profile = readiness.prepare_geelark_profile_for_adb(
            "p1", transport, timeout_seconds=0)
        self.assertIsNone(profile)

    def test_start_if_stopped_off_never_spends_money(self):
        """A caller that only wants phones already up must not be able to
        start one by accident -- starting is what costs."""
        transport = FakeTransport({"/phone/status": _status(2)})
        profile = readiness.prepare_geelark_profile_for_adb(
            "p1", transport, start_if_stopped=False)
        self.assertIsNone(profile)
        self.assertNotIn("/phone/start", transport.paths)

    def test_run_fields_are_carried_onto_the_profile(self):
        """The flows read these off the Profile; dropping them silently posts
        the wrong clip or to the wrong handle."""
        transport = FakeTransport({
            "/phone/status": _status(0),
            "/adb/setStatus": {},
            "/adb/getData": _adb_ready(),
        })
        profile = readiness.prepare_geelark_profile_for_adb(
            "p1", transport, media_path="/clip.mp4", queue_id="rec1",
            target_handle="someone")
        self.assertEqual(profile.media_path, "/clip.mp4")
        self.assertEqual(profile.queue_id, "rec1")
        self.assertEqual(profile.target_handle, "someone")


class ReleaseTest(_NoSleep):
    def test_stopping_reports_a_refusal(self):
        transport = FakeTransport({
            "/phone/stop": {"totalAmount": 1, "successAmount": 0, "failAmount": 1,
                            "failDetails": [{"id": "p1", "msg": "nope"}]},
        })
        self.assertFalse(readiness.release_geelark_phone("p1", transport))

    def test_stopping_succeeds(self):
        transport = FakeTransport({
            "/phone/stop": {"totalAmount": 1, "successAmount": 1, "failAmount": 0},
        })
        self.assertTrue(readiness.release_geelark_phone("p1", transport))


if __name__ == "__main__":
    unittest.main()
