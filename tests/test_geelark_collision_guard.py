"""Catching two Geelark phones running on the same real proxy port at once
-- only possible via a manual Geelark-UI start, since our own launches
already lease the port exclusively. Confirmed real incident 2026-08-29:
Nikki 2 and Nikki 26 both ran on port 54021 simultaneously.
"""

import unittest
from unittest.mock import patch

from adb_bot.automation import geelark_collision_guard as guard


class FakeTransport:
    def __init__(self):
        self.stop_calls: list[list[str]] = []

    def post(self, path, payload):
        if path == "/phone/stop":
            self.stop_calls.append(list(payload["ids"]))
        return {}


def _phone(id_, port, status=0, name=None):
    return {"id": id_, "serialName": name or f"P{id_}", "status": status,
           "proxy": {"port": port}}


class CheckOnceTest(unittest.TestCase):
    def setUp(self):
        guard._first_seen.clear()
        self.addCleanup(guard._first_seen.clear)

    def _run(self, phones, now=0.0):
        transport = FakeTransport()
        fake_client = type("FakeClient", (), {
            "list_phones": lambda self: phones,
            "transport": transport,
        })()
        with patch.object(guard, "GeelarkPhoneClient", lambda t: fake_client):
            stopped = guard.check_once(transport=transport, _now=now)
        return stopped, transport

    def test_no_collision_when_every_port_has_one_phone(self):
        phones = [_phone("1", 54015), _phone("2", 54018)]
        stopped, transport = self._run(phones)
        self.assertEqual(stopped, [])
        self.assertEqual(transport.stop_calls, [])

    def test_stopped_phones_are_excluded_from_collision_checks(self):
        phones = [_phone("1", 54015, status=2)]  # stopped
        stopped, _ = self._run(phones)
        self.assertEqual(stopped, [])

    def test_two_phones_on_the_same_port_stops_the_newer_one(self):
        # id "1" seen first (tick 0), id "2" arrives later (tick 5) -- same
        # port, so "2" is the collision and gets stopped.
        phones_at_t0 = [_phone("1", 54021)]
        self._run(phones_at_t0, now=0.0)

        phones_at_t5 = [_phone("1", 54021), _phone("2", 54021)]
        stopped, transport = self._run(phones_at_t5, now=5.0)

        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["id"], "2")
        self.assertEqual(transport.stop_calls, [["2"]])

    def test_three_way_collision_keeps_only_the_oldest(self):
        self._run([_phone("1", 54021)], now=0.0)
        self._run([_phone("1", 54021), _phone("2", 54021)], now=1.0)
        stopped, _ = self._run(
            [_phone("1", 54021), _phone("2", 54021), _phone("3", 54021)], now=2.0)
        stopped_ids = {s["id"] for s in stopped}
        self.assertEqual(stopped_ids, {"2", "3"})

    def test_a_phone_that_stops_and_restarts_is_treated_as_new(self):
        """If this process's memory of a phone is cleared because it briefly
        stopped showing as running, a later start on the same port must be
        judged fresh, not as if it had been running the whole time."""
        self._run([_phone("1", 54021)], now=0.0)
        self._run([], now=1.0)  # "1" stopped -- forgotten
        # "1" restarts fresh at tick 10, "2" shows up right after at tick 10.1
        stopped, _ = self._run([_phone("1", 54021)], now=10.0)
        self.assertEqual(stopped, [])
        stopped2, transport = self._run(
            [_phone("1", 54021), _phone("2", 54021)], now=10.1)
        self.assertEqual(stopped2[0]["id"], "2")

    def test_different_ports_never_collide_with_each_other(self):
        phones = [_phone("1", 54015), _phone("2", 54018),
                 _phone("3", 54021), _phone("4", 54028)]
        stopped, transport = self._run(phones)
        self.assertEqual(stopped, [])
        self.assertEqual(transport.stop_calls, [])


if __name__ == "__main__":
    unittest.main()
