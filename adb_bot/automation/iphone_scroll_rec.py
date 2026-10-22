"""Scroll via Rene's own finger: replay random slices of a cached XXTouch recording.

17.10 Rene: synthetic scroll_feed is pointless when we have an actual recording of his finger
scrolling (/var/mobile/Media/1ferver/lua/scripts/rec_20261009042136.lua, pulled 17.10 to
/opt/adbbot/rec_cache/rene_scroll.lua, ~711 KB / 20 512 touch events / 422 s long). The
recording keeps real-finger pressure/size variations that remote-tap flows can never fake --
exactly the biometric fingerprint IG looks at.

build_scroll_lua(seconds) returns a self-contained Lua script that plays a random continuous
slice of the recording whose duration is close to `seconds`. Each call picks a different start
offset so two consecutive calls never replay the same stretch. Timestamps are shifted so the
slice starts at ms=0 (otherwise the player would sleep until the original absolute timestamp).

Grouping: slices start/end on gesture boundaries (touch.off for single-finger, touch.hand_end
for multi-finger batches) so we never cut mid-swipe and leave an unbalanced touch.on.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

REC_PATH = Path("/opt/adbbot/rec_cache/rene_scroll.lua")

_HEADER: str | None = None
_FOOTER: str | None = None
_GROUPS: list[dict] | None = None   # each: {"start_ms": int, "end_ms": int, "lines": list[str]}

_RA_RE = re.compile(r"^RA\((\d+),\s*(touch\.\w+)")


def _load() -> None:
    """Parse the recording once per process. On any error, leave _GROUPS = [] (fallback)."""
    global _HEADER, _FOOTER, _GROUPS
    if _GROUPS is not None:
        return
    if not REC_PATH.exists():
        _GROUPS = []
        return
    try:
        lines = REC_PATH.read_text().splitlines(keepends=True)
    except OSError:
        _GROUPS = []
        return

    # Body = all consecutive RA() lines; header is everything before the first; footer everything
    # after the last. Non-RA interleaved lines (comments / blanks) go with the current group.
    first_ra = last_ra = None
    for i, L in enumerate(lines):
        if _RA_RE.match(L):
            if first_ra is None:
                first_ra = i
            last_ra = i
    if first_ra is None:
        _GROUPS = []
        return
    _HEADER = "".join(lines[:first_ra])
    _FOOTER = "".join(lines[last_ra + 1:])

    # Group by gesture: a touch.off (single-finger) or a touch.hand_end (multi-finger batch) is
    # the only safe place to cut. Everything between two safe cuts stays together.
    groups: list[dict] = []
    cur: dict | None = None
    for L in lines[first_ra:last_ra + 1]:
        m = _RA_RE.match(L)
        if not m:                                              # blank / comment inside the body
            if cur is not None:
                cur["lines"].append(L)
            continue
        ms, op = int(m.group(1)), m.group(2)
        if cur is None:
            cur = {"start_ms": ms, "end_ms": ms, "lines": []}
        cur["lines"].append(L)
        cur["end_ms"] = ms
        if op == "touch.off" or op == "touch.hand_end":        # safe to cut here
            groups.append(cur)
            cur = None
    if cur is not None:                                        # trailing partial group -- keep it
        groups.append(cur)
    _GROUPS = groups


def available() -> bool:
    _load()
    return bool(_GROUPS)


def recording_duration_ms() -> int:
    _load()
    if not _GROUPS:
        return 0
    return _GROUPS[-1]["end_ms"] - _GROUPS[0]["start_ms"]


def build_scroll_lua(seconds: float) -> str | None:
    """Return a Lua script that plays ~`seconds` of recorded scrolling from a random offset,
    or None if the recording isn't available. Timestamps in the slice are shifted so playback
    starts at ms=0; speed is kept at the recorded pace (no compression)."""
    _load()
    if not _GROUPS:
        return None

    budget_ms = max(1_000, int(seconds * 1000))
    n = len(_GROUPS)

    # Pick a random start group, then walk forward until the span reaches the budget OR we run
    # out of recording. Starting positions near the end would give a very short slice, so cap
    # the start so at least ~budget_ms is still ahead when possible.
    total_ms = _GROUPS[-1]["end_ms"] - _GROUPS[0]["start_ms"]
    if total_ms > budget_ms:
        latest_start_ms = _GROUPS[0]["start_ms"] + (total_ms - budget_ms)
        # Find the latest group index whose start_ms <= latest_start_ms
        latest_i = 0
        for i, g in enumerate(_GROUPS):
            if g["start_ms"] <= latest_start_ms:
                latest_i = i
            else:
                break
        start_i = random.randint(0, latest_i)
    else:
        start_i = 0

    picked: list[dict] = []
    for i in range(start_i, n):
        picked.append(_GROUPS[i])
        span = _GROUPS[i]["end_ms"] - picked[0]["start_ms"]
        if span >= budget_ms:
            break
    if not picked:
        return None

    base_ms = picked[0]["start_ms"]
    shifted: list[str] = []
    for g in picked:
        for L in g["lines"]:
            m = _RA_RE.match(L)
            if m:
                new_ms = int(m.group(1)) - base_ms
                shifted.append(f"RA({new_ms},{L[m.end(1) + 1:]}")
            else:
                shifted.append(L)

    return (_HEADER or "") + "".join(shifted) + (_FOOTER or "")
