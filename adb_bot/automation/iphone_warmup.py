"""iPhone warm-up in four days, one script per day (Rene, 22.10).

Each day is its own function with its own repertoire -- day 1 only watches, day 4 behaves like a settled
user. Airtable (Accounts SOP) drives it: an account is "in warm-up" while `Warm-up Start` is set and
`Warm-up Day 4 done` is empty; the bot writes `Warm-up Day N done` when day N's last action finished,
sets `Status` to "Warmup day N" while running and to "Healthy" after day 4. Day N+1 may start once day N
is done AND it is the next Berlin calendar day (IPHONE_WARMUP_SAME_DAY=1 lifts the day rule, keeps a
IPHONE_WARMUP_GAP_HOURS gap, default 8 h). Window 09-23 Berlin, one account per phone per tick, the
phone lock keeps it out of posting/signup, a screenshot is kept wherever it stops.

    .venv/bin/python -m adb_bot.automation.iphone_warmup due               # who would run now, and why not
    .venv/bin/python -m adb_bot.automation.iphone_warmup days              # the four scripts, in words
    .venv/bin/python -m adb_bot.automation.iphone_warmup run <device> <container> --day 2 [--dry]
    .venv/bin/python -m adb_bot.automation.iphone_warmup tick [--max-phones 4]   # what the timer calls
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import random
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import requests

from adb_bot.automation import iphone_diag, iphone_farm_config
from adb_bot.automation import iphone_phone_lock as phone_lock
from adb_bot.automation.iphone_farm_wda import Phone, WDAError, resolve
from adb_bot.automation.iphone_ig import (FlowError, Human, _follow_one, _random_extra, dismiss_popups, go_tab,
                                          open_account)

log = logging.getLogger("iphone_warmup")
BERLIN = ZoneInfo("Europe/Berlin")
BASE, T_ACC, T_CONT = "appwk9LrgAhJv8iEH", "tbly0y1I4hNDlvh1K", "tblyZXN7caKZZBaBj"
F_START, F_STATUS, F_CREATION, F_NOTES, F_USER = "Warm-up Start", "Status", "Creation Status", "Notes", "Username"
F_DONE = {n: f"Warm-up Day {n} done" for n in (1, 2, 3, 4)}
BLOCKED_STATUS = {"Human Verification", "Completely Banned", "Banned", "Review", "logged out"}
OK_CREATION = {"Instagram Created", "Verified"}
WINDOW = (9, 23)
GAP_HOURS = float(os.environ.get("IPHONE_WARMUP_GAP_HOURS", "8"))
SAME_DAY = os.environ.get("IPHONE_WARMUP_SAME_DAY") == "1"
STATE_DIR = os.path.expanduser("~/.adb_bot")


# ---------------------------------------------------------------- the four day scripts

@dataclass
class Plan:
    day: int
    title: str
    minutes: tuple[float, float]
    follows: tuple[int, int]
    extras: dict = field(default_factory=dict)          # {extra name: weight}, see iphone_ig.EXTRA_WEIGHTS
    extra_rate: float = 0.0                              # chance per reel of one detour
    story_rounds: tuple[int, int] = (0, 0)               # story-tray visits at fixed points of the session
    explore_rounds: tuple[int, int] = (0, 0)
    words: str = ""


PLANS = {
    # Rene 22.10: always 6-10 min at random, never longer; follows day 1: 1-3, from day 2: 3-7. Always the Reels
    # tab. Profile/explore detours are allowed but risky ("dann spackt der wieder rum"), so they stay rare.
    1: Plan(1, "look around", (6, 10), (1, 3), {"like": 3, "linger": 4, "scroll-back": 2}, 0.10, (0, 0), (0, 0),
            "Reels only, 6-10 min, slow. A few likes, 1-3 follows. No stories, no explore, no comments."),
    2: Plan(2, "first interest", (6, 10), (3, 7), {"like": 4, "linger": 3, "scroll-back": 3, "story": 1}, 0.14, (0, 1), (0, 0),
            "Reels 6-10 min, 3-7 follows, more likes, maybe one story-tray visit."),
    3: Plan(3, "settling in", (6, 10), (3, 7), {"like": 4, "linger": 2, "scroll-back": 3, "story": 1, "profile-visit": 1}, 0.16, (0, 1), (0, 1),
            "Reels 6-10 min, 3-7 follows, maybe a story round or an explore detour, a rare creator profile visit."),
    4: Plan(4, "regular user", (6, 10), (3, 7), {"like": 4, "linger": 2, "scroll-back": 3, "story": 1, "profile-visit": 1, "comments": 1}, 0.18, (0, 1), (0, 1),
            "Reels 6-10 min, 3-7 follows, the whole (rare) repertoire. After this day the account is Healthy."),
}
MAX_CONSECUTIVE_ERRORS = 8


def _recover(h: Human, log_fn, stats: dict, exc: Exception, ref: str, container: str) -> None:
    """Something went wrong mid-session (popup, sheet, WDA hiccup): keep a screenshot the first two times,
    tell Rene once, go back to the Reels tab and carry on. Never abort for one bad step (Rene 22.10)."""
    stats["errors"].append(f"{type(exc).__name__}: {str(exc)[:80]}")
    n = len(stats["errors"])
    log_fn(f"step failed ({n}): {str(exc)[:120]} -- back to Reels")
    if n <= 2:
        shot = iphone_diag.keep_screen(ref, f"warmup-recover{n}", f"{type(exc).__name__}: {exc}", container=container,
                                       wda=h.p, xx=h.xx, log_fn=log_fn)
        if n == 1:
            iphone_diag.notify(f"warm-up {container}: hiccup, going back to Reels and continuing: {str(exc)[:160]}\n{shot or ''}")
    for attempt in range(3):
        try:
            if isinstance(exc, WDAError):
                h.p.sid = None
                time.sleep(5 + 5 * attempt)
            dismiss_popups(h)
            go_tab(h, "reels")
            return
        except Exception as exc2:                             # noqa: BLE001
            log_fn(f"back-to-Reels attempt {attempt + 1} failed: {str(exc2)[:80]}")
            time.sleep(3)


# Rene 22.10: "nicht immer gleich langsam -- auch mal zurueck, auch mal den Finger drauf halten, auch mal richtig
# schnell: 3 Reels in einer Sekunde und dann stopp." Per reel one of these rhythms is drawn (weights per day).
RHYTHM = {
    #        watch  skim  burst  hold  back
    1: dict(watch=62, skim=14, burst=8, hold=10, back=6),
    2: dict(watch=55, skim=16, burst=12, hold=10, back=7),
    3: dict(watch=52, skim=16, burst=14, hold=10, back=8),
    4: dict(watch=50, skim=16, burst=16, hold=10, back=8),
}


def _rhythm(h: Human, plan: Plan, stats: dict) -> None:
    """Leave the current reel the way a person would right now: watch it out, skim, burst through a few and
    stop, rest the finger on it, or go back one. Always ends with a new reel on screen and a pause."""
    w = RHYTHM[plan.day]
    mode = random.choices(list(w), weights=list(w.values()))[0]
    stats["rhythm"][mode] = stats["rhythm"].get(mode, 0) + 1
    if mode == "watch":                                       # the normal case: look, then one flick
        h.pause(3, 14) if random.random() < 0.85 else h.pause(15, 35)
        h.reel_next()
        stats["reels_watched"] += 1
    elif mode == "skim":                                      # a few reels at 1-3 s each, then one that gets watched
        for _ in range(random.randint(2, 4)):
            h.pause(0.9, 3.0)
            h.reel_next()
            stats["reels_watched"] += 1
        h.pause(4, 12)
    elif mode == "burst":                                     # 3-5 reels inside ~1-3 s, then stop on one
        n = random.randint(3, 5)
        h.pause(0.5, 2.0)
        h.reel_burst(n)
        stats["reels_watched"] += n
        h.pause(5, 16)
    elif mode == "hold":                                      # finger down on the reel (pauses it), lift, watch on
        h.pause(2, 6)
        h.reel_hold(random.uniform(1.2, 3.5))
        h.pause(2, 8)
        h.reel_next()
        stats["reels_watched"] += 1
    else:                                                     # back: the one before was better
        h.pause(2, 6)
        h.swipe(0.5 + random.uniform(-0.1, 0.1), 0.3, 0.5 + random.uniform(-0.1, 0.1), 0.8, random.uniform(0.15, 0.3))
        h.pause(3, 10)
        h.reel_next()
        stats["reels_watched"] += 1
    stats["swipes"] += 1


def _session(h: Human, plan: Plan, log_fn, stats: dict, ref: str = "?", container: str = "?") -> None:
    """The shared engine: reels loop with the day's follows spread out and the day's extras at its rate.
    Story/explore rounds happen at fixed fractions of the session so every day has its own rhythm. Every
    step is guarded: a failure means screenshot, back to Reels, go on."""
    minutes = random.uniform(*plan.minutes)
    follows = random.randint(*plan.follows)
    start, end = time.monotonic(), time.monotonic() + minutes * 60
    slots = sorted(random.uniform(0.1, 0.9) * minutes * 60 for _ in range(follows))
    story_at = sorted(random.uniform(0.2, 0.85) for _ in range(random.randint(*plan.story_rounds)))
    explore_at = sorted(random.uniform(0.3, 0.8) for _ in range(random.randint(*plan.explore_rounds)))
    log_fn(f"day {plan.day} '{plan.title}': {minutes:.1f} min, {follows} follows, {len(story_at)} story rounds, "
           f"{len(explore_at)} explore rounds")
    go_tab(h, "reels")
    n = streak = 0
    while time.monotonic() < end:
        n += 1
        frac = (time.monotonic() - start) / (minutes * 60)
        try:
            if n % 5 == 1:
                pop = dismiss_popups(h)
                if pop:
                    stats["popups"].append(pop)
                    log_fn(pop)
            if slots and time.monotonic() - start >= slots[0]:
                if _follow_one(h, stats, log_fn):
                    slots.pop(0)
            elif story_at and frac >= story_at[0]:
                story_at.pop(0)
                stats["extras"].append(_random_extra(h, "reels", log_fn, {"story": 1}) or "story-skipped")
            elif explore_at and frac >= explore_at[0]:
                explore_at.pop(0)
                stats["extras"].append(_random_extra(h, "reels", log_fn, {"explore": 1}) or "explore-skipped")
            elif plan.extras and n > 2 and random.random() < plan.extra_rate:
                name = _random_extra(h, "reels", log_fn, plan.extras)
                if name:
                    stats["extras"].append(name)
            _rhythm(h, plan, stats)
            streak = 0
        except Exception as exc:                              # noqa: BLE001
            streak += 1
            if streak >= MAX_CONSECUTIVE_ERRORS:
                raise FlowError(f"{streak} steps in a row failed, last: {exc}") from exc
            _recover(h, log_fn, stats, exc, ref, container)
    stats["follows_missed"] = len(slots)
    stats["minutes"] = round((time.monotonic() - start) / 60, 1)


def day1(h: Human, log_fn, stats: dict, **ctx) -> None:
    """Day 1 -- look around. Reels only, slow, a few likes, 1-3 follows."""
    _session(h, PLANS[1], log_fn, stats, **ctx)


def day2(h: Human, log_fn, stats: dict, **ctx) -> None:
    """Day 2 -- first interest. 3-7 follows, more likes, maybe one story round."""
    _session(h, PLANS[2], log_fn, stats, **ctx)


def day3(h: Human, log_fn, stats: dict, **ctx) -> None:
    """Day 3 -- settling in. 3-7 follows, rare story/explore/profile detours."""
    _session(h, PLANS[3], log_fn, stats, **ctx)


def day4(h: Human, log_fn, stats: dict, **ctx) -> None:
    """Day 4 -- regular user. 3-7 follows, the whole (rare) repertoire; afterwards the account is Healthy."""
    _session(h, PLANS[4], log_fn, stats, **ctx)


DAY_SCRIPTS = {1: day1, 2: day2, 3: day3, 4: day4}


# ---------------------------------------------------------------- Airtable

def _hdr() -> dict:
    tok = os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY") or ""
    if not tok:
        raise RuntimeError("AIRTABLE_TOKEN is not set (source /etc/adbbot/env)")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _list(table: str, formula: str) -> list[dict]:
    out, offset = [], None
    while True:
        params = {"filterByFormula": formula, "pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}", headers=_hdr(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out


def _get(table: str, rid: str) -> dict:
    r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}/{rid}", headers=_hdr(), timeout=30)
    r.raise_for_status()
    return r.json()


def _patch(rid: str, fields: dict) -> None:
    r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{T_ACC}/{rid}", headers=_hdr(),
                       json={"fields": fields, "typecast": True}, timeout=30)
    if r.status_code >= 300:
        log.warning("Airtable patch %s failed: %s %s", rid, r.status_code, r.text[:200])


def _note(rid: str, line: str) -> None:
    try:
        old = _get(T_ACC, rid)["fields"].get(F_NOTES) or ""
        stamp = dt.datetime.now(BERLIN).strftime("%d.%m %H:%M")
        _patch(rid, {F_NOTES: (old + "\n" if old else "") + f"[{stamp} warm-up] {line}"[:1800]})
    except Exception as exc:                                  # noqa: BLE001
        log.warning("note failed: %s", exc)


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BERLIN)
    except ValueError:
        return None


WARMUP_STATUS = re.compile(r"warm-?up\s*day\s*(\d)", re.I)      # "Warmup day 1" / "warmup day 3" (both spellings exist)
NOT_YET_CREATED = {"To Do", "Gmail Logged In", "Stuck / Needs Human"}


def next_day(f: dict, now: dt.datetime | None = None) -> tuple[int | None, str]:
    """(day to run now, reason) -- day None when nothing is due; reason says why.

    Who is in warm-up: Status says "Warmup day N" (Rene's hand-set statuses from the 3-day scheme count: days
    before N are taken as done by hand), or "setup"/"Instagram Created" with a Warm-up Start, or any `Warm-up
    Day N done` already written by this bot. "Healthy" with nothing written is an account that finished the
    OLD warm-up -- it is NOT re-warmed (22.10: 15 Healthy Nikki/Lea accounts would otherwise have started at
    day 1 again)."""
    now = now or dt.datetime.now(BERLIN)
    status = f.get(F_STATUS) or ""
    if status in BLOCKED_STATUS:
        return None, f"status {status!r}"
    if (f.get(F_CREATION) or "") in NOT_YET_CREATED:
        return None, f"creation status {f.get(F_CREATION)!r}"
    if not (f.get(F_USER) or "").strip():
        return None, "no username on the row"
    if not f.get(F_START):
        return None, "no Warm-up Start"
    written = [n for n in (1, 2, 3, 4) if f.get(F_DONE[n])]
    m = WARMUP_STATUS.search(status)
    if not written and not m and status not in ("setup", "Instagram Created"):
        return None, f"status {status!r} is not a warm-up status and no day written -> not in warm-up"
    hand_done = int(m.group(1)) - 1 if m else 0                # "warmup day 2" by hand = day 1 happened already
    for n in (1, 2, 3, 4):
        if f.get(F_DONE[n]) or n <= hand_done:
            continue
        if n == 1 or (n - 1 <= hand_done and not f.get(F_DONE[n - 1])):
            return n, (f"day {n} never ran" if n == 1 else f"status says day {n} (days before set by hand)")
        prev = _parse(f.get(F_DONE[n - 1]))
        if not prev:
            return None, f"day {n - 1} timestamp unreadable"
        gap_h = (now - prev).total_seconds() / 3600
        if gap_h < GAP_HOURS:
            return None, f"day {n - 1} finished {gap_h:.1f} h ago (< {GAP_HOURS:g} h)"
        if not SAME_DAY and prev.date() == now.date():
            return None, f"day {n - 1} finished today -> day {n} tomorrow"
        return n, f"day {n - 1} done {prev.strftime('%d.%m %H:%M')}"
    return None, "all four days done"


def candidates() -> list[dict]:
    """Every account in warm-up with its phone/container resolved; `day` is None with a `why` when not due."""
    rows = _list(T_ACC, f"AND({{{F_START}}} != '', {{{F_DONE[4]}}} = '', {{Container}} != '')")
    out = []
    for row in rows:
        f = row["fields"]
        day, why = next_day(f)
        cont_id = (f.get("Container") or [None])[0]
        try:
            cont = _get(T_CONT, cont_id)["fields"] if cont_id else {}
        except requests.RequestException:
            cont = {}
        device = (cont.get("Device") or [None])[0]
        out.append({"row": row["id"], "username": (f.get(F_USER) or "").lstrip("@"), "container": cont.get("Container"),
                    "device": device, "day": day, "why": why, "status": f.get(F_STATUS)})
    return out


# ---------------------------------------------------------------- one run

def _letter(device: str) -> str:
    try:
        _rec, phone = resolve(device)
        stream = str(phone.get("stream") or "")
        return iphone_diag._LETTER.get(stream, (phone.get("name") or "?")[-1:].upper())
    except Exception:                                         # noqa: BLE001
        return "?"


def run_one(device: str, container: str, day: int, row: str | None = None, expect: str | None = None,
            dry: bool = False, log_fn=None) -> dict:
    """Open the container, run day N's script, write Airtable. Raises nothing on a flow failure: returns
    {"ok": False, "error": ..., "screen": path} and leaves Airtable's day field untouched."""
    say = log_fn or (lambda m: log.info("%s/%s: %s", _letter(device), container, m))
    plan, script = PLANS[day], DAY_SCRIPTS[day]
    stats = {"day": day, "follows": [], "extras": [], "popups": [], "errors": [], "rhythm": {}, "swipes": 0, "reels_watched": 0}
    if dry:
        say(f"DRY: would run day {day} '{plan.title}' ({plan.words})")
        return {"ok": True, "dry": True, **stats}
    rec_id, _ = resolve(device)
    letter = _letter(device)
    # 22.10 Rene: order on a phone is signup > warm-up/setup > posting. Posting already yields at its next post
    # boundary to whoever holds the lock; a signup that announced itself (want) goes first, we wait for the next tick.
    if phone_lock.signup_wanted(letter):
        return {"ok": False, "error": f"phone {letter}: a signup is waiting for it -- warm-up next tick"}
    hold = phone_lock.hold(letter, timeout_s=600)
    if hold is None:
        return {"ok": False, "error": f"phone {letter} busy (posting/signup holds the lock)"}
    p: Phone | None = None
    try:
        iphone_farm_config._set_off(rec_id, True)             # watchdog: hands off this phone
        if row:
            fields = {F_STATUS: f"Warmup day {day}"}
            _patch(row, fields)
        p = open_account(device, container, expect, log=say)
        h = Human(p, say)
        script(h, say, stats, ref=device, container=container)
        say(f"day {day} done: {stats['reels_watched']} reels, {len(stats['follows'])} follows "
            f"({stats.get('follows_missed', 0)} missed), extras {stats['extras']}, popups {stats['popups']}, "
            f"{len(stats['errors'])} hiccups, rhythm {stats['rhythm']}")
        if row:
            fields = {F_DONE[day]: dt.datetime.now(BERLIN).isoformat()}
            if day == 4:
                fields[F_STATUS] = "Healthy"
            _patch(row, fields)
            _note(row, f"day {day} '{plan.title}' ok: {stats['reels_watched']} reels, {len(stats['follows'])} follows, "
                       f"{stats.get('minutes', '?')} min")
        return {"ok": True, **stats}
    except Exception as exc:                                  # noqa: BLE001
        shot = iphone_diag.keep_screen(device, f"warmup-day{day}", f"{type(exc).__name__}: {exc}", container=container,
                                       wda=p, log_fn=say)
        say(f"day {day} FAILED: {exc} (screen: {shot or 'none'})")
        if row:
            _note(row, f"day {day} failed: {str(exc)[:300]} | screen {shot or '-'}")
        iphone_diag.notify(f"warm-up day {day} on {letter}/{container} (@{expect or '?'}) stopped: {str(exc)[:200]}\n{shot or ''}")
        return {"ok": False, "error": str(exc), "screen": shot, **stats}
    finally:
        try:
            iphone_farm_config._set_off(rec_id, False)
        except Exception:                                     # noqa: BLE001
            pass
        hold.release()


# ---------------------------------------------------------------- the timer tick

def _in_window(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(BERLIN)
    return WINDOW[0] <= now.hour < WINDOW[1]


def tick(max_phones: int = 4) -> dict:
    """One account per phone, up to max_phones phones in parallel; everything else waits for the next tick."""
    if not _in_window():
        log.info("outside %02d-%02d Berlin, nothing to do", *WINDOW)
        return {"skipped": "window"}
    due = [c for c in candidates() if c["day"] and c["device"] and c["container"]]
    random.shuffle(due)
    by_phone: dict[str, dict] = {}
    for c in due:
        by_phone.setdefault(c["device"], c)                   # first due account per phone
    picked = list(by_phone.values())[:max_phones]
    if not picked:
        log.info("nothing due (%d accounts in warm-up)", len(due))
        return {"ran": []}
    results: dict[str, dict] = {}

    def work(c: dict) -> None:
        time.sleep(random.uniform(0, 90))                     # phones do not all start in the same second
        results[c["container"]] = run_one(c["device"], c["container"], c["day"], row=c["row"], expect=c["username"] or None)

    ths = [threading.Thread(target=work, args=(c,), name=c["container"]) for c in picked]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return {"ran": [{"container": k, "ok": v.get("ok"), "day": v.get("day"), "error": v.get("error")} for k, v in results.items()]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("days")
    sub.add_parser("due")
    s = sub.add_parser("run")
    s.add_argument("device"); s.add_argument("container")
    s.add_argument("--day", type=int, required=True, choices=(1, 2, 3, 4))
    s.add_argument("--row", help="Accounts SOP record id to write the result to (omit: no Airtable write)")
    s.add_argument("--expect", help="handle the container must hold")
    s.add_argument("--dry", action="store_true")
    s = sub.add_parser("tick")
    s.add_argument("--max-phones", type=int, default=4)
    a = ap.parse_args(argv)
    os.makedirs("/opt/adbbot/logs", exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("/opt/adbbot/logs/iphone_warmup.log")])
    if a.cmd == "days":
        for n, pl in PLANS.items():
            print(f"Day {n} -- {pl.title}: {pl.minutes[0]:g}-{pl.minutes[1]:g} min, {pl.follows[0]}-{pl.follows[1]} follows\n   {pl.words}")
        return 0
    if a.cmd == "due":
        for c in candidates():
            mark = f"DUE day {c['day']}" if c["day"] else "wait"
            print(f"{mark:10} {_letter(c['device'] or '')}/{c['container'] or '?':4} @{c['username'] or '?':22} "
                  f"status {c['status'] or '-':18} {c['why']}")
        return 0
    if a.cmd == "run":
        res = run_one(a.device, a.container, a.day, row=a.row, expect=a.expect, dry=a.dry)
        print(res)
        return 0 if res.get("ok") else 1
    res = tick(a.max_phones)
    log.info("tick: %s", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
