"""Geelark's own RPA automation tasks.

Geelark can drive its own cloud phones directly -- `instagramEdit` sets Bio,
Link and Profile Picture (from a URL) on the device through Geelark's own
automation engine, no ADB or UI-dump reading from us required. Found via
Geelark's public API docs (cached locally under `/tmp/geelark-cli`), not
previously wrapped in this repo.

Every task is asynchronous: triggering one returns a `taskId`, and the
actual work happens on Geelark's side over the following seconds to
minutes. `task_detail` is the only way to know whether it worked.
"""

from __future__ import annotations

import time

from .transport import GeelarkTransport

INSTAGRAM_EDIT_PATH = "/rpa/task/instagramEdit"
TASK_DETAIL_PATH = "/task/detail"
TASK_QUERY_PATH = "/task/query"

# From Geelark's task-detail docs. Waiting/In progress are not terminal --
# `wait_for_task` polls past them; the other three are.
STATUS_WAITING = 1
STATUS_IN_PROGRESS = 2
STATUS_COMPLETED = 3
STATUS_FAILED = 4
STATUS_CANCELLED = 7

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})


def trigger_instagram_edit_profile(
    phone_id: str, *, schedule_at: int | None = None, biography: str = "",
    link_url: str = "", link_title: str = "", profile_picture: str = "",
    nickname: str = "", username: str = "", name: str = "", remark: str = "",
    transport: GeelarkTransport | None = None,
) -> str:
    """Ask Geelark to edit `phone_id`'s Instagram profile. Returns the taskId.

    Every field is optional and only sent if non-empty -- a field left out
    is left alone on the device, it is not cleared. `schedule_at` defaults
    to now (a second-level timestamp); Geelark requires it even for an
    immediate run.
    """
    transport = transport or GeelarkTransport()
    body: dict = {"id": str(phone_id),
                 "scheduleAt": schedule_at or int(time.time())}
    if biography:
        body["biography"] = biography
    if link_url:
        body["linkURL"] = link_url
    if link_title:
        body["linkTitle"] = link_title
    if profile_picture:
        body["profilePicture"] = [profile_picture]
    if nickname:
        body["nickname"] = nickname
    if username:
        body["username"] = username
    if name:
        body["name"] = name
    if remark:
        body["remark"] = remark

    data = transport.post(INSTAGRAM_EDIT_PATH, body)
    return str(data.get("taskId") or "")


def task_detail(task_id: str, transport: GeelarkTransport | None = None,
                search_after=None) -> dict:
    """One task's full detail, including `status`, `failDesc`, `resultImages`."""
    transport = transport or GeelarkTransport()
    body: dict = {"id": str(task_id)}
    if search_after is not None:
        body["searchAfter"] = search_after
    return transport.post(TASK_DETAIL_PATH, body)


def wait_for_task(task_id: str, transport: GeelarkTransport | None = None, *,
                  timeout_seconds: int = 300, poll_interval: float = 8,
                  sleep=time.sleep, clock=time.monotonic) -> dict:
    """Poll `task_detail` until it leaves Waiting/In progress, or times out.

    Returns whatever the last poll saw -- including a still-non-terminal
    `status` if `timeout_seconds` ran out, which the caller must check for:
    this never raises on a timeout, since a slow task is not the same
    failure as one Geelark actually marked failed.
    """
    transport = transport or GeelarkTransport()
    deadline = clock() + timeout_seconds
    detail: dict = {}
    while True:
        detail = task_detail(task_id, transport=transport)
        if detail.get("status") in TERMINAL_STATUSES:
            return detail
        if clock() >= deadline:
            return detail
        sleep(poll_interval)
