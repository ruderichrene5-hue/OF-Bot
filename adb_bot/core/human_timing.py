"""Human-like timing and pixel jitter for adb input, generated in one seam.

`adb shell input tap` is a single, zero-duration event landing on an exact
pixel -- a signature no real touch has. `AdbChallengeDriver._tap()` is the one
place every flow's taps already funnel through (`tap_label`, `fill`, every
individual tap in the verification chain), so this is built as a seam that
plugs in there rather than something each flow has to remember to call.

The numbers below are not measured off a real device -- there is no published
reference for what Instagram/Google's fraud models actually key on. They are a
deliberately conservative starting point, in the direction already proven to
matter live: Instagram's own web login needed roughly a 120ms hold before it
would accept a tap at all (`geelark-android16.md`). Real taps also do not all
take the identical number of milliseconds, and do not all land on the exact
same pixel of a button twice -- both are addressed here, not the guessed
duration alone.
"""

from __future__ import annotations

import math
import random

# Tap dwell (down -> up) for an ordinary tap. Centred near the one duration
# already confirmed to matter, with spread rather than a single fixed number.
DWELL_MEAN_MS = 115
DWELL_STDDEV_MS = 25
DWELL_MIN_MS = 60
DWELL_MAX_MS = 220

# A "held" tap (`press=True`) exists for a different, already-proven reason --
# a RecyclerView row whose ripple/click machinery needs the touch to sit still
# past its gesture-detector's touch-slop window (confirmed live, 2026-08-23:
# the "Choose From Gallery" row did not register a plain tap most of the time
# even though the coordinate and focused window were both correct). That fix
# needed *at least* ~150ms; kept as a floor here, just no longer one fixed
# number every time.
HELD_MEAN_MS = 190
HELD_STDDEV_MS = 30
HELD_MIN_MS = 150
HELD_MAX_MS = 320

# How far off dead-centre a tap can land. Bounded by a fraction of the
# target's own size *and* a hard pixel ceiling, so a jitter proven safe on a
# big card is never blindly reused on a small icon.
JITTER_MAX_FRACTION = 0.28
JITTER_MAX_PX = 40
# Used when no bounds are known at all -- a raw coordinate with no UI-dump
# node behind it (`tap_xy`, e.g. a reCAPTCHA grid cell). Deliberately smaller:
# there is no element size here to prove a bigger offset stays "inside"
# anything.
JITTER_NO_BOUNDS_PX = 8


def _rand(rand: random.Random | None) -> random.Random:
    return rand if rand is not None else random.Random()


def _clamped_gauss(mean: float, stddev: float, low: float, high: float,
                   rand: random.Random) -> int:
    return int(max(low, min(high, rand.gauss(mean, stddev))))


def dwell_ms(rand: random.Random | None = None, held: bool = False) -> int:
    """A tap's down-to-up duration, in ms -- never the identical number twice."""
    r = _rand(rand)
    if held:
        return _clamped_gauss(HELD_MEAN_MS, HELD_STDDEV_MS, HELD_MIN_MS,
                              HELD_MAX_MS, r)
    return _clamped_gauss(DWELL_MEAN_MS, DWELL_STDDEV_MS, DWELL_MIN_MS,
                          DWELL_MAX_MS, r)


def jitter_point(x: int, y: int,
                 bounds: tuple[int, int, int, int] | None = None,
                 rand: random.Random | None = None) -> tuple[int, int]:
    """`(x, y)` nudged a plausible few pixels off centre.

    `bounds` (x1, y1, x2, y2), when given, is the tapped element's own
    on-screen box -- the jitter is clamped to a safe fraction of it, so a
    small icon is never pushed toward its edge. Without bounds, a smaller
    fixed radius is used instead, since there is no element size to prove a
    bigger offset still lands "inside" anything.
    """
    r = _rand(rand)
    if bounds is not None:
        x1, y1, x2, y2 = bounds
        half_w, half_h = (x2 - x1) / 2, (y2 - y1) / 2
        max_dx = min(half_w * JITTER_MAX_FRACTION, JITTER_MAX_PX)
        max_dy = min(half_h * JITTER_MAX_FRACTION, JITTER_MAX_PX)
    else:
        max_dx = max_dy = JITTER_NO_BOUNDS_PX

    dx = r.uniform(-max_dx, max_dx) if max_dx > 0 else 0
    dy = r.uniform(-max_dy, max_dy) if max_dy > 0 else 0
    return int(round(x + dx)), int(round(y + dy))


def swipe_duration_ms(base_ms: int, spread_frac: float = 0.15,
                      rand: random.Random | None = None) -> int:
    """`base_ms` plus or minus up to `spread_frac` of itself.

    Kept as a distinct helper from `dwell_ms` -- a swipe's whole point is
    covering a distance, not holding still, so it needs its own base duration
    per caller rather than the tap distribution's numbers.
    """
    r = _rand(rand)
    spread = base_ms * spread_frac
    return int(max(1, base_ms + r.uniform(-spread, spread)))


# How far the path bows off the straight line between its endpoints, as a
# fraction of the line's own length. `adb shell input swipe` cannot bow at
# all -- it is architecturally a straight 2-point line with no waypoint
# parameter -- so this only matters to a caller that can actually drive a
# multi-point gesture (confirmed live 2026-08-24: uiautomator2's
# `swipe_points()`, coexisting cleanly with the plain `uiautomator dump`
# every other flow reads screens with -- 6 stress-test rounds, connect ->
# curved gesture -> release -> dump, no degradation, no lockups).
BOW_MIN_FRACTION = 0.03
BOW_MAX_FRACTION = 0.09


def curved_swipe_points(x1: int, y1: int, x2: int, y2: int,
                        rand: random.Random | None = None,
                        segments: int = 4) -> list[tuple[int, int]]:
    """A plausible curved path from (x1, y1) to (x2, y2), as waypoints for a
    real multi-point gesture (e.g. `uiautomator2.Device.swipe_points`).

    Two things a straight line never has, both approximated here:

    * **A bow.** The path drifts a few percent of its own length off the
      straight line, perpendicular to the direction of travel, peaking at
      the midpoint and fading to zero at both ends (so it still starts and
      ends exactly on the requested points).
    * **Acceleration/deceleration.** Waypoints are spaced with an
      ease-in/ease-out curve -- denser near both ends, sparser in the middle
      -- rather than evenly. A gesture driver that times each waypoint about
      equally then plays back as fast-slow-fast rather than one constant
      velocity for the whole distance, which is what a real swipe of any
      real distance actually looks like and a single `input swipe` command
      can never produce.

    Degrades to the two bare endpoints for a zero-length request -- there is
    no direction to bow perpendicular to, and nothing to accelerate through.
    """
    r = _rand(rand)
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0 or segments < 2:
        return [(x1, y1), (x2, y2)]

    perp_x, perp_y = -dy / length, dx / length
    bow = length * r.uniform(BOW_MIN_FRACTION, BOW_MAX_FRACTION) * r.choice((-1, 1))

    points = [(x1, y1)]
    for i in range(1, segments):
        t = i / segments
        eased = (1 - math.cos(t * math.pi)) / 2
        px = x1 + dx * eased
        py = y1 + dy * eased
        wobble = math.sin(t * math.pi)   # peaks mid-path, zero at both ends
        px += perp_x * bow * wobble
        py += perp_y * bow * wobble
        points.append((int(round(px)), int(round(py))))
    points.append((x2, y2))
    return points
