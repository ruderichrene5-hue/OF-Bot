"""The 2captcha client: create a task, poll it, and never guess.

Runs against a scripted session and a fake clock -- a real solve takes 10-20
seconds of a human's time and costs money.
"""

import tempfile
from pathlib import Path
from unittest import TestCase

from adb_bot.clients.captcha import (
    SOLVER_2CAPTCHA,
    TwoCaptchaSolver,
    UnconfiguredSolver,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    """Answers each endpoint from a scripted queue."""

    def __init__(self, script):
        self.script = {path: list(items) for path, items in script.items()}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        path = "/" + url.rstrip("/").rsplit("/", 1)[-1]
        self.calls.append((path, json))
        queue = self.script.get(path)
        if not queue:
            raise AssertionError(f"unexpected call to {path}")
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResponse(payload)


class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SolverTestCase(TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="adbbot-captcha-"))
        self.image = tmp / "captcha.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n fake image bytes")
        self.clock = FakeClock()

    def build(self, script):
        self.session = FakeSession(script)
        return TwoCaptchaSolver("test-key", session=self.session,
                                clock=self.clock.time, sleep=self.clock.sleep)


class SolveTest(SolverTestCase):
    def test_a_ready_answer_is_returned(self):
        solver = self.build({
            "/createTask": [{"errorId": 0, "taskId": 77}],
            "/getTaskResult": [{"errorId": 0, "status": "ready",
                                "solution": {"text": "7F3KQ"}, "cost": "0.0006"}],
        })
        self.assertEqual(solver.solve_text(str(self.image)), "7F3KQ")
        self.assertEqual(solver.last_task_id, 77)

    def test_it_polls_until_the_worker_answers(self):
        solver = self.build({
            "/createTask": [{"errorId": 0, "taskId": 77}],
            "/getTaskResult": [
                {"errorId": 0, "status": "processing"},
                {"errorId": 0, "status": "processing"},
                {"errorId": 0, "status": "ready", "solution": {"text": "ABC12"}},
            ],
        })
        self.assertEqual(solver.solve_text(str(self.image)), "ABC12")

    def test_the_image_is_sent_base64_encoded(self):
        solver = self.build({
            "/createTask": [{"errorId": 0, "taskId": 1}],
            "/getTaskResult": [{"errorId": 0, "status": "ready",
                                "solution": {"text": "X"}}],
        })
        solver.solve_text(str(self.image), hint="enter the letters shown")
        task = self.session.calls[0][1]["task"]
        self.assertEqual(task["type"], "ImageToTextTask")
        self.assertTrue(task["body"], "the image must be sent")
        self.assertTrue(task["case"], "Instagram captchas are case sensitive")
        self.assertEqual(task["comment"], "enter the letters shown")

    def test_it_gives_up_rather_than_guess(self):
        """A wrong answer costs an attempt on an already-flagged account."""
        solver = self.build({
            "/createTask": [{"errorId": 0, "taskId": 77}],
            "/getTaskResult": [{"errorId": 0, "status": "processing"}],
        })
        started = self.clock.now
        self.assertIsNone(solver.solve_text(str(self.image)))
        self.assertLessEqual(self.clock.now - started, solver.solve_timeout + 10)

    def test_an_api_error_is_not_an_exception(self):
        solver = self.build({
            "/createTask": [{"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
                             "errorDescription": "bad key"}],
        })
        self.assertIsNone(solver.solve_text(str(self.image)))

    def test_an_empty_solution_is_not_an_answer(self):
        solver = self.build({
            "/createTask": [{"errorId": 0, "taskId": 3}],
            "/getTaskResult": [{"errorId": 0, "status": "ready",
                                "solution": {"text": "   "}}],
        })
        self.assertIsNone(solver.solve_text(str(self.image)))

    def test_a_missing_image_file_is_not_an_exception(self):
        solver = self.build({"/createTask": [{"errorId": 0, "taskId": 1}]})
        self.assertIsNone(solver.solve_text("/nonexistent/captcha.png"))


class ReportTest(SolverTestCase):
    def test_a_bad_answer_can_be_reported(self):
        solver = self.build({
            "/createTask": [{"errorId": 0, "taskId": 90}],
            "/getTaskResult": [{"errorId": 0, "status": "ready",
                                "solution": {"text": "WR0NG"}}],
            "/reportIncorrect": [{"errorId": 0, "status": "success"}],
        })
        solver.solve_text(str(self.image))
        self.assertTrue(solver.report_incorrect())
        self.assertEqual(self.session.calls[-1][1]["taskId"], 90)

    def test_nothing_is_reported_before_a_solve(self):
        solver = self.build({"/reportIncorrect": [{"errorId": 0}]})
        self.assertFalse(solver.report_incorrect())


class UnconfiguredTest(TestCase):
    def test_it_reads_nothing_and_says_so(self):
        solver = UnconfiguredSolver()
        self.assertIsNone(solver.solve_text("/any/path.png"))
        self.assertFalse(solver.report_incorrect())

    def test_the_real_solver_is_named(self):
        self.assertEqual(TwoCaptchaSolver("k").name, SOLVER_2CAPTCHA)
