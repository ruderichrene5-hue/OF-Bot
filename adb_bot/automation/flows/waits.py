"""Adaptive waits: stop paying a fixed sleep for work that's already finished.

The flows were written with blind `time.sleep(3)` after every tap -- a number
picked to be safe on the slowest device. On a responsive phone the next screen is
usually up in a few hundred milliseconds, so most of that is dead time, and it
adds up: the reel flow alone spends ~80 s in fixed sleeps.

:func:`settle` replaces those sleeps. Given a `ready` predicate it polls and
returns **as soon as the next screen is actually there**, and only falls back to
waiting the full original duration when it can't confirm anything.

The safety rule that makes this a drop-in: **the ceiling is never shorter than
the sleep it replaced**, and we only return early on a *positive* confirmation.
A slow device therefore behaves exactly as it did before -- this can speed a run
up, never rush it. A `ready` predicate that's wrong or flaky just degrades to the
old fixed-sleep behaviour.

`flow_speed` in settings scales the fallback sleeps (`fast` 0.5x / `normal` 1x /
`slow` 1.5x) so a particular server or phone can be dialled in without code
changes. Scaling never applies to a confirmed-ready early return, which is
already as fast as the device allows.
"""

from __future__ import annotations

import time

# Multiplier applied to fallback sleeps and ceilings.
SPEED_PROFILES = {"fast": 0.5, "normal": 1.0, "slow": 1.5}
DEFAULT_SPEED = "normal"

# How often to re-check a readiness predicate. Each check is a cheap RPC/dump,
# so this trades a little chatter for a lot of saved wall-clock.
DEFAULT_POLL_SECONDS = 0.25

# Never scale a wait below this -- some animations genuinely need a moment.
MIN_SLEEP_SECONDS = 0.15

_speed_override: float | None = None


def set_speed(profile_or_factor) -> None:
    """Override the speed profile for this process (tests, or the UI applying a
    setting). Accepts a profile name or a raw multiplier."""
    global _speed_override
    if profile_or_factor is None:
        _speed_override = None
        return
    if isinstance(profile_or_factor, (int, float)):
        _speed_override = float(profile_or_factor)
        return
    _speed_override = SPEED_PROFILES.get(str(profile_or_factor).lower().strip(), 1.0)


def speed_factor() -> float:
    """Current multiplier: explicit override, else the saved setting, else 1.0."""
    if _speed_override is not None:
        return _speed_override
    try:
        from adb_bot.config import settings
        name = (settings.get_saved_flow_speed() or DEFAULT_SPEED).lower().strip()
    except Exception:
        return 1.0
    return SPEED_PROFILES.get(name, 1.0)


def scaled(seconds: float) -> float:
    """`seconds` adjusted by the speed profile, floored so nothing hits zero."""
    if seconds <= 0:
        return 0.0
    return max(MIN_SLEEP_SECONDS, float(seconds) * speed_factor())


def wait_for(predicate, timeout: float, poll: float = DEFAULT_POLL_SECONDS,
             now=time.time, sleep=time.sleep) -> bool:
    """Poll `predicate` until it's truthy. True if it became true within
    `timeout`, False otherwise. A predicate that raises counts as 'not yet' --
    a transient dump/RPC failure must not abort the wait."""
    deadline = now() + max(0.0, timeout)
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if now() >= deadline:
            return False
        sleep(poll)


def settle(seconds: float, ready=None, timeout: float | None = None,
           poll: float = DEFAULT_POLL_SECONDS, logger=None, what: str = "",
           now=time.time, sleep=time.sleep) -> bool:
    """Drop-in for `time.sleep(seconds)` after an action.

    Without `ready`: a plain (speed-scaled) sleep.
    With `ready`: polls and returns as soon as the screen is confirmed ready,
    otherwise waits up to `timeout` (default `seconds`, speed-scaled) -- i.e.
    never less patient than the sleep it replaced.

    Returns True when readiness was confirmed, False when it fell back to
    waiting out the ceiling (or no predicate was given).
    """
    ceiling = scaled(seconds if timeout is None else timeout)

    if ready is None:
        if ceiling > 0:
            sleep(ceiling)
        return False

    started = now()
    confirmed = wait_for(ready, timeout=ceiling, poll=poll, now=now, sleep=sleep)
    if logger is not None and what:
        elapsed = now() - started
        if confirmed:
            logger.info("Ready: %s after %.2fs (cap %.2fs)", what, elapsed, ceiling)
        else:
            logger.info("Not confirmed: %s within %.2fs; continuing", what, ceiling)
    return confirmed


def any_exists(d, *selectors) -> bool:
    """True if any of the uiautomator2 selectors matches right now.

    `.exists` is an immediate RPC (it does NOT honour implicitly_wait), so this
    is cheap enough to poll -- which is exactly what makes `settle` viable.
    """
    for kwargs in selectors:
        try:
            if d(**kwargs).exists:
                return True
        except Exception:
            continue
    return False


def u2_ready(d, *selectors):
    """A `ready` predicate for :func:`settle` from uiautomator2 selectors."""
    def predicate():
        return any_exists(d, *selectors)
    return predicate
