"""Who may use an iPhone right now: signups go first, posting yields between posts (10.10, Rene:
"der Posting-Prozess macht den aktuellen Post fertig, dann vor dem nächsten Kurzbefehl erst den
Sign-up -- der findet nur alle paar Stunden statt und ist wichtig").

Per phone letter (A-E) two things under /run/lock/adbbot-iphone/:
  <L>.lock            fcntl lock held while a flow is actually driving the phone (one post + its
                      scroll, or one whole signup) -- released by the kernel if the process dies
  <L>.signup-wanted   "a signup is waiting for / using this phone" (pid inside; a dead pid = stale)

Signup (signup_planner): want(L) -> hold(L, wait) blocks until the current post is done -> signup ->
release -> unwant(L).
Posting (iphone_xx_post_batch): before each post: if signup_wanted(L) -> stop the batch here (the rest
stays queued, the account is due again right away); else try_hold(L) for exactly one post + scroll.
Before opening a container it waits until no signup wants the phone (wait_free).
"""

from __future__ import annotations

import fcntl
import os
import time

DIR = "/run/lock/adbbot-iphone"


def _path(letter: str, kind: str) -> str:
    os.makedirs(DIR, exist_ok=True)
    return os.path.join(DIR, f"{letter.upper()}.{kind}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


KINDS = ("signup", "warmup", "setup")    # 22.10: warm-up and setup announce themselves too, so posting yields to them


def wanted(letter: str, kind: str = "signup") -> bool:
    p = _path(letter, f"{kind}-wanted")
    try:
        pid = int(open(p).read().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    if _alive(pid):
        return True
    try:
        os.remove(p)                       # left behind by a dead process
    except OSError:
        pass
    return False


def signup_wanted(letter: str) -> bool:
    return wanted(letter, "signup")


def any_wanted(letter: str) -> str | None:
    """Which flow kind is waiting for the phone (signup first), or None."""
    for k in KINDS:
        if wanted(letter, k):
            return k
    return None


def want(letter: str, note: str = "", kind: str = "signup") -> None:
    with open(_path(letter, f"{kind}-wanted"), "w") as f:
        f.write(f"{os.getpid()} {note}\n")


def unwant(letter: str, kind: str = "signup") -> None:
    try:
        os.remove(_path(letter, f"{kind}-wanted"))
    except OSError:
        pass


class Hold:
    """The phone's lock, held. Use as a context manager or call release()."""

    def __init__(self, fd: int):
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def try_hold(letter: str) -> Hold | None:
    """The lock right now, or None if another flow is driving the phone."""
    fd = os.open(_path(letter, "lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return Hold(fd)
    except BlockingIOError:
        os.close(fd)
        return None


def hold(letter: str, timeout_s: float = 1800, poll_s: float = 5) -> Hold | None:
    """Wait (up to timeout_s) until the phone is free, then hold it. None on timeout."""
    end = time.time() + timeout_s
    while True:
        h = try_hold(letter)
        if h or time.time() >= end:
            return h
        time.sleep(poll_s)


def wait_free(letter: str, log=None, poll_s: float = 15) -> float:
    """Block while a signup / warm-up / setup wants or holds the phone. Returns the seconds waited."""
    t0, told = time.time(), False
    while True:
        k = any_wanted(letter)
        if not k:
            h = try_hold(letter)
            if h:
                h.release()
                return time.time() - t0
        if log and not told:
            log(f"waiting -- a {k or 'flow'} has this phone first")
            told = True
        time.sleep(poll_s)
