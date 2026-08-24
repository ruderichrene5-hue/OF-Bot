"""Read the letters and digits off Instagram's image captcha, via 2captcha.
Also solves the *other* shape of captcha this codebase runs into -- Google's
reCAPTCHA v2 image grid ("select all images with a bus") -- via the same
account and the same create-then-poll pattern.

The text captcha is usually the *first* screen a flagged account shows, so a
run that cannot get past it never reaches the phone/code steps at all -- which
is why it is a real integration rather than the placeholder it started as.

2captcha is a human-in-the-loop service: post the image, then poll for an answer
a worker produces. Its `ImageToTextTask` takes a base64 PNG and typically comes
back within 10-20 seconds, so the polling budget here is generous (two minutes)
-- much longer than the SMS wait, because unlike a dead number there is nothing
to fall back to and giving up costs the whole run. `GridTask` (the reCAPTCHA
grid) answers just as fast -- it is the same worker pool, reading a picture --
but the caller there (`recaptcha_grid.py`) has its own, much tighter deadline:
Google expires the on-screen challenge itself if the checkbox tap and the
image selections take too long (confirmed live, 2026-08-24 -- "Verification
challenge expired" on the very first attempt, lost purely to how long it took
to look at the picture and decide).

Two things worth knowing:

- **Answers are charged whether or not they are right** (~$0.0006 each). When
  the flow can see the answer was rejected -- the captcha screen comes back --
  it calls :meth:`report_incorrect`, which refunds that solve and feeds 2captcha's
  own worker scoring. Cheap to do, and it is the only signal that keeps the hit
  rate honest.
- **A wrong answer costs an attempt on an already-flagged account**, so the
  solver never guesses: anything it cannot read comes back as None and the
  profile stays with a human.

The `CaptchaSolver` protocol is the seam the verification flow codes against, so
swapping 2captcha for another service later is one new class and one line in
:func:`build_solver`.
"""

from __future__ import annotations

import base64
import time
from typing import Protocol, runtime_checkable

import requests

# Solver names, so logs and Airtable notes can say which one answered.
SOLVER_NONE = "none"
SOLVER_2CAPTCHA = "2captcha"

API_BASE = "https://api.2captcha.com"
HTTP_TIMEOUT = 30

# A worker has to look at the image and type it. 10-20s is normal; the ceiling
# is for a queue backlog, and is deliberately well past the SMS budget.
SOLVE_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 5
# 2captcha asks callers to wait before the first poll, since nothing can be
# ready sooner and early polls just add load.
INITIAL_DELAY_SECONDS = 5


@runtime_checkable
class CaptchaSolver(Protocol):
    """Turn a captcha image into the characters a human would type."""

    name: str

    def solve_text(self, image_path: str, hint: str = "") -> str | None:
        """Return the characters in the image, or None if it could not be read.

        `image_path` is a PNG on local disk -- the flow screenshots the phone and
        crops the challenge image before calling. `hint` carries any instruction
        text shown next to the image ("enter the letters shown below").

        Returning None must always be an option: every caller has to handle
        "unsolved" anyway, so a solver should never raise for an image it simply
        could not read.
        """

    def report_incorrect(self) -> bool:
        """Tell the service its last answer was rejected, if it supports that."""

    def solve_grid(self, image_path: str, rows: int, columns: int,
                   comment: str = "",
                   solve_timeout: int | None = None) -> list[int] | None:
        """Which cells of an image-grid captcha (e.g. reCAPTCHA) match `comment`.

        `solve_timeout`, if given, overrides the solver's own default poll
        budget -- for a caller racing a clock the captcha service knows
        nothing about (reCAPTCHA's own on-screen challenge expiry).

        Cells are numbered 1..rows*columns, left to right then top to bottom --
        2captcha's own `GridTask` numbering, kept as-is rather than translated,
        so a caller reading their docs and this code side by side sees the same
        numbers.

        An **empty list is a real, meaningful answer**: nothing in the current
        grid matches, which is what "solved" looks like once every matching
        tile has already been clicked away. That must never be confused with
        `None`, which means the service could not answer at all.
        """


class UnconfiguredSolver:
    """The fallback when no captcha credential is set: reads nothing, admits it.

    A flow that gets None back reports the profile as still needing a human,
    which is the state it was already in -- so an unconfigured solver degrades
    the feature instead of breaking the run.
    """

    name = SOLVER_NONE

    def solve_text(self, image_path: str, hint: str = "") -> str | None:
        return None

    def report_incorrect(self) -> bool:
        return False

    def solve_grid(self, image_path: str, rows: int, columns: int,
                   comment: str = "",
                   solve_timeout: int | None = None) -> list[int] | None:
        return None


class TwoCaptchaSolver:
    """2captcha's `ImageToTextTask`, create-then-poll."""

    name = SOLVER_2CAPTCHA

    def __init__(self, api_key: str, api_base: str = API_BASE,
                 timeout: int = HTTP_TIMEOUT,
                 solve_timeout: int = SOLVE_TIMEOUT_SECONDS,
                 poll_interval: float = POLL_INTERVAL_SECONDS,
                 initial_delay: float = INITIAL_DELAY_SECONDS,
                 logger=None, session=None, clock=time.time,
                 sleep=time.sleep) -> None:
        if not api_key:
            raise ValueError("2captcha API key is required")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.solve_timeout = solve_timeout
        self.poll_interval = poll_interval
        self.initial_delay = initial_delay
        self.logger = logger
        self._session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self.last_task_id = None

    # --- HTTP -----------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict | None:
        """POST JSON and return the decoded body, or None if the call failed."""
        try:
            response = self._session.post(f"{self.api_base}{path}", json=payload,
                                          timeout=self.timeout)
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            self._log("warning", "captcha: %s failed (%s)", path, exc)
            return None
        if not isinstance(body, dict):
            self._log("warning", "captcha: %s returned %s", path, type(body).__name__)
            return None
        if body.get("errorId"):
            self._log("warning", "captcha: %s refused (%s: %s)", path,
                      body.get("errorCode"), body.get("errorDescription"))
            return None
        return body

    # --- solver protocol ------------------------------------------------------
    def solve_text(self, image_path: str, hint: str = "") -> str | None:
        try:
            with open(image_path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
        except OSError as exc:
            self._log("warning", "captcha: could not read %s (%s)", image_path, exc)
            return None

        task = {
            "type": "ImageToTextTask",
            "body": encoded,
            # Instagram's captchas mix upper and lower case and are not phrases,
            # so the answer must be taken exactly as the worker typed it.
            "phrase": False,
            "case": True,
            "numeric": 0,
            "math": False,
        }
        if hint:
            task["comment"] = hint[:200]

        created = self._post("/createTask",
                             {"clientKey": self.api_key, "task": task})
        if not created:
            return None
        task_id = created.get("taskId")
        if not task_id:
            self._log("warning", "captcha: createTask gave no task id")
            return None
        self.last_task_id = task_id

        return self._await_result(task_id)

    def _wait_for_ready(self, task_id, solve_timeout: int) -> dict | None:
        """Poll `getTaskResult` until `status == "ready"`, or give up.

        The body behind `solution` differs by task type (`text` vs `click`);
        callers decode that themselves, this only owns the create-then-poll
        shape both share.
        """
        deadline = self._clock() + solve_timeout
        self._sleep(min(self.initial_delay, solve_timeout))

        while True:
            body = self._post("/getTaskResult",
                              {"clientKey": self.api_key, "taskId": task_id})
            if body is None:
                return None

            status = str(body.get("status") or "").lower()
            if status == "ready":
                return body

            remaining = deadline - self._clock()
            if remaining <= 0:
                self._log("warning", "captcha: task %s unsolved after %ss",
                          task_id, solve_timeout)
                return None
            self._sleep(min(self.poll_interval, remaining))

    def _await_result(self, task_id) -> str | None:
        body = self._wait_for_ready(task_id, self.solve_timeout)
        if body is None:
            return None
        text = str((body.get("solution") or {}).get("text") or "").strip()
        if not text:
            self._log("warning", "captcha: task %s came back empty", task_id)
            return None
        self._log("info", "captcha: task %s solved as %r (cost %s)",
                  task_id, text, body.get("cost"))
        return text

    # --- image-grid captchas (reCAPTCHA's "select all images with...") -------
    def solve_grid(self, image_path: str, rows: int, columns: int,
                   comment: str = "", solve_timeout: int | None = None) -> list[int] | None:
        """2captcha's `GridTask`: which of `rows * columns` tiles match `comment`.

        `solve_timeout` defaults to the same budget as `solve_text` but can be
        cut short by the caller -- `recaptcha_grid.py` passes a much smaller one,
        because Google expires the on-screen challenge well before two minutes
        are up, and a slow answer nobody can use is worth abandoning quickly
        rather than sitting out the full poll.
        """
        try:
            with open(image_path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
        except OSError as exc:
            self._log("warning", "captcha: could not read %s (%s)", image_path, exc)
            return None

        task = {
            "type": "GridTask",
            "body": encoded,
            "rows": rows,
            "columns": columns,
        }
        if comment:
            task["comment"] = comment[:200]

        created = self._post("/createTask",
                             {"clientKey": self.api_key, "task": task})
        if not created:
            return None
        task_id = created.get("taskId")
        if not task_id:
            self._log("warning", "captcha: createTask (grid) gave no task id")
            return None
        self.last_task_id = task_id

        body = self._wait_for_ready(task_id, solve_timeout or self.solve_timeout)
        if body is None:
            return None
        solution = body.get("solution") or {}
        clicks = solution.get("click")
        if clicks is None:
            self._log("warning", "captcha: grid task %s came back without a "
                                 "'click' answer (%r)", task_id, solution)
            return None
        try:
            cells = [int(c) for c in clicks]
        except (TypeError, ValueError):
            self._log("warning", "captcha: grid task %s returned an "
                                 "unreadable click list (%r)", task_id, clicks)
            return None
        self._log("info", "captcha: grid task %s solved as cells %s (cost %s)",
                  task_id, cells, body.get("cost"))
        return cells

    def report_incorrect(self) -> bool:
        """Flag the last answer as wrong -- refunds it and scores the worker."""
        if not self.last_task_id:
            return False
        body = self._post("/reportIncorrect",
                          {"clientKey": self.api_key, "taskId": self.last_task_id})
        return body is not None

    def balance(self) -> float:
        body = self._post("/getBalance", {"clientKey": self.api_key})
        if not body:
            return 0.0
        try:
            return float(body.get("balance") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _log(self, level: str, message: str, *args) -> None:
        if self.logger is None:
            return
        handler = getattr(self.logger, level, None)
        if handler:
            handler(message, *args)


def build_solver(logger=None) -> CaptchaSolver:
    """Return the configured solver, or the null one when no key is set.

    Same rule as the SMS providers: a missing credential means the feature is
    absent, not that every call fails.
    """
    from adb_bot.config.settings import get_saved_2captcha_key

    api_key = get_saved_2captcha_key()
    if not api_key:
        if logger:
            logger.warning("captcha: no 2captcha key configured; image "
                           "challenges will be left for a human")
        return UnconfiguredSolver()
    return TwoCaptchaSolver(api_key, logger=logger)
