"""Triggering Geelark's own instagramEdit RPA task, and reading its status.

Geelark does the actual bio/link/picture-setting on its side; everything
here is just the request shape going out and the status coming back --
nothing here drives a phone directly.
"""

import unittest
from unittest import mock

from adb_bot.clients.geelark import rpa


class _FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def post(self, path, body):
        self.calls.append((path, body))
        if self._responses:
            return self._responses.pop(0)
        return {}


class TriggerInstagramEditTest(unittest.TestCase):
    def test_only_non_empty_fields_are_sent(self):
        """A field left out must not clear it on the device -- Geelark's own
        docs describe each field as optional for exactly this reason."""
        transport = _FakeTransport(responses=[{"taskId": "t1"}])

        task_id = rpa.trigger_instagram_edit_profile(
            "555", biography="hi", transport=transport)

        self.assertEqual(task_id, "t1")
        path, body = transport.calls[0]
        self.assertEqual(path, rpa.INSTAGRAM_EDIT_PATH)
        self.assertEqual(body["id"], "555")
        self.assertEqual(body["biography"], "hi")
        self.assertNotIn("linkURL", body)
        self.assertNotIn("profilePicture", body)
        self.assertNotIn("nickname", body)

    def test_a_missing_schedule_at_defaults_to_now(self):
        transport = _FakeTransport(responses=[{"taskId": "t1"}])

        rpa.trigger_instagram_edit_profile("555", transport=transport)

        _path, body = transport.calls[0]
        self.assertIn("scheduleAt", body)
        self.assertGreater(body["scheduleAt"], 0)

    def test_the_profile_picture_is_sent_as_a_list(self):
        """Geelark's `profilePicture` field is an array of URLs even for a
        single picture -- sending a bare string would not match its shape."""
        transport = _FakeTransport(responses=[{"taskId": "t1"}])

        rpa.trigger_instagram_edit_profile(
            "555", profile_picture="https://example.com/a.jpg",
            transport=transport)

        _path, body = transport.calls[0]
        self.assertEqual(body["profilePicture"], ["https://example.com/a.jpg"])

    def test_no_task_id_in_the_response_comes_back_empty_not_none(self):
        transport = _FakeTransport(responses=[{}])

        task_id = rpa.trigger_instagram_edit_profile("555", transport=transport)

        self.assertEqual(task_id, "")


class TaskDetailTest(unittest.TestCase):
    def test_asks_for_exactly_the_task_id_given(self):
        transport = _FakeTransport(responses=[{"status": rpa.STATUS_COMPLETED}])

        detail = rpa.task_detail("t1", transport=transport)

        self.assertEqual(transport.calls[0], (rpa.TASK_DETAIL_PATH, {"id": "t1"}))
        self.assertEqual(detail["status"], rpa.STATUS_COMPLETED)


class WaitForTaskTest(unittest.TestCase):
    def test_stops_polling_once_a_terminal_status_arrives(self):
        transport = _FakeTransport(responses=[
            {"status": rpa.STATUS_WAITING},
            {"status": rpa.STATUS_IN_PROGRESS},
            {"status": rpa.STATUS_COMPLETED},
        ])
        slept = []

        detail = rpa.wait_for_task("t1", transport=transport,
                                   sleep=slept.append, clock=iter(range(10)).__next__)

        self.assertEqual(detail["status"], rpa.STATUS_COMPLETED)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(slept), 2, "should sleep once per non-terminal poll")

    def test_a_failed_task_is_terminal_too_not_retried(self):
        transport = _FakeTransport(responses=[{"status": rpa.STATUS_FAILED,
                                               "failDesc": "no such user"}])

        detail = rpa.wait_for_task("t1", transport=transport,
                                   sleep=lambda _s: None,
                                   clock=iter(range(10)).__next__)

        self.assertEqual(detail["status"], rpa.STATUS_FAILED)
        self.assertEqual(len(transport.calls), 1)

    def test_a_task_still_running_at_the_deadline_is_returned_not_raised(self):
        """A slow task is not the same failure as one Geelark actually
        marked failed -- the caller decides what a timeout means."""
        transport = _FakeTransport(responses=[
            {"status": rpa.STATUS_IN_PROGRESS} for _ in range(50)])
        clock = iter([0, 1, 2, 400])  # crosses a small timeout on the 4th look

        detail = rpa.wait_for_task("t1", transport=transport,
                                   timeout_seconds=300, sleep=lambda _s: None,
                                   clock=lambda: next(clock))

        self.assertEqual(detail["status"], rpa.STATUS_IN_PROGRESS)


if __name__ == "__main__":
    unittest.main()
