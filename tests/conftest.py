"""Keep the test run out of the live lock directory.

`~/.adb_bot/locks` belongs to loops that are actually running on this box: the
profile locks they hold, and (since the cross-loop ceiling) the slot files that
say how many phones are open. Tests take and release both -- `test_locks.py`
directly, and every test that drives a runner end to end with fakes. Against the
real directory that is a two-way hazard: a test could take a slot a live posting
run needed, and a live run at the ceiling could make a runner test see its
profiles deferred and fail for no reason.

One session-wide redirect fixes both. `lock_dir()` is looked up on the module at
call time (never imported by name), so patching the attribute covers the slot
directory, `doctor.check_locks`, and everything else.
"""

import tempfile
from pathlib import Path

import pytest

from adb_bot.core import locks


@pytest.fixture(scope="session", autouse=True)
def isolated_lock_dir():
    tmp = Path(tempfile.mkdtemp(prefix="adbbot-test-locks-"))
    original = locks.lock_dir

    def lock_dir():
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

    locks.lock_dir = lock_dir
    try:
        yield tmp
    finally:
        locks.lock_dir = original
