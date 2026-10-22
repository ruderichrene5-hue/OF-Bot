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
                                          open_account, visible)

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
    1: Plan(1, "look around", (8, 10), (0, 1), {"like": 3, "linger": 4, "scroll-back": 2}, 0.10, (0, 0), (0, 0),
            "Reels only, 8-10 min, slow. 1-2 likes at most, at most one follow near the end. No stories, no explore, "
            "no comments -- a person who just installed the app."),
    2: Plan(2, "first interest", (10, 14), (2, 3), {"like": 4, "linger": 3, "scroll-back": 3, "story": 2}, 0.15, (1, 1), (0, 0),
            "Reels 10-14 min with 2-3 follows spread over the session, one visit to the story tray, a few more "
            "likes. Still no comments or profile visits."),
    3: Plan(3, "settling in", (14, 18), (4, 6), {"like": 4, "linger": 2, "scroll-back": 3, "story": 2, "explore": 2, "profile-visit": 2}, 0.18, (1, 2), (1, 1),
            "Reels 14-18 min, 4-6 follows, stories once or twice, one explore detour, first profile visits of "
            "creators. Comments are only read, never written."),
    4: Plan(4, "regular user", (18, 22), (5, 8), {"like": 4, "linger": 2, "scroll-back": 3, "story": 2, "explore": 2, "profile-visit": 2, "comments": 2}, 0.20, (2, 3), (1, 2),
            "Reels 18-22 min, 5-8 follows, the whole repertoire (stories, explore, comments opened, profile visits). "
            "After this day the account is Healthy and may post."),
}


def _session(h: Human, plan: Plan, log_fn, stats: dict) -> None:
    """The shared engine: reels loop with the day's follows spread out and the day's extras at its rate.
    Story/explore rounds happen at fixed fractions of the session so every day has its own rhythm."""
    minutes = random.uniform(*plan.minutes)
    follows = random.randint(*plan.follows)
    start, end = time.monotonic(), time.monotonic() + minutes * 60
    slots = sorted(random.uniform(0.15, 0.9) * minutes * 60 for _ in range(follows))
    # 22.10: the extras that happen "at a fixed point" of the day, not at random
    story_at = sorted(random.uniform(0.2, 0.85) for _ in range(random.randint(*plan.story_rounds)))
    explore_at = sorted(random.uniform(0.3, 0.8) for _ in range(random.randint(*plan.explore_rounds)))
    log_fn(f"day {plan.day} '{plan.title}': {minutes:.1f} min, {follows} follows, {len(story_at)} story rounds, "
           f"{len(explore_at)} explore rounds")
    go_tab(h, "reels")
    n = 0
    while time.monotonic() < end:
        n += 1
        frac = (time.monotonic() - start) / (minutes * 60)
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
            name = _random_extra(h, "reels", log_fn, {"story": 1})
            stats["extras"].append(name or "story-skipped")
        elif explore_at and frac >= explore_at[0]:
            explore_at.pop(0)
            name = _random_extra(h, "reels", log_fn, {"explore": 1})
            stats["extras"].append(name or "explore-skipped")
        elif plan.extras and n > 2 and random.random() < plan.extra_rate:
            name = _random_extra(h, "reels", log_fn, plan.extras)
            if name:
                stats["extras"].append(name)
        # one reel: swipe up, watch
        h.swipe(0.5 + random.uniform(-0.1, 0.1), 0.78, 0.5 + random.uniform(-0.1, 0.1), 0.25, random.uniform(0.18, 0.32))
        stats["swipes"] += 1
        stats["reels_watched"] += 1
        slow = plan.day == 1                                  # day 1 watches longer, swipes less
        h.pause(4, 18) if (random.random() < 0.85 or not slow) else h.pause(18, 40)
    stats["follows_missed"] = len(slots)
    stats["minutes"] = round((time.monotonic() - start) / 60, 1)


def day1(h: Human, log_fn, stats: dict) -> None:
    """Day 1 -- look around. Reels only, slow, a like or two, one follow at most and only near the end."""
    _session(h, PLANS[1], log_fn, stats)


def day2(h: Human, log_fn, stats: dict) -> None:
    """Day 2 -- first interest. A story round, 2-3 follows, more likes."""
    _session(h, PLANS[2], log_fn, stats)


def day3(h: Human, log_fn, stats: dict) -> None:
    """Day 3 -- settling in. Explore once, first profile visits, 4-6 follows."""
    _session(h, PLANS[3], log_fn, stats)
    # end of day 3: look at the own profile once, like a person checking what their page looks like
    try:
        go_tab(h, "profile")
        h.pause(3, 7)
        go_tab(h, "reels")
    except (FlowError, WDAError) as exc:
        log_fn(f"own-profile look skipped: {str(exc)[:80]}")


def day4(h: Human, log_fn, stats: dict) -> None:
    """Day 4 -- regular user. The whole repertoire incl. opening comments; afterwards the account is Healthy."""
    _session(h, PLANS[4], log_fn, stats)
    try:                                                      # a quick look at the inbox, then back
        go_tab(h, "direct-inbox")
        h.pause(3, 6)
        go_tab(h, "reels")
    except (FlowError, WDAError) as exc:
        log_fn(f"inbox look skipped: {str(exc)[:80]}")


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
    stats = {"day": day, "follows": [], "extras": [], "popups": [], "swipes": 0, "reels_watched": 0}
    if dry:
        say(f"DRY: would run day {day} '{plan.title}' ({plan.words})")
        return {"ok": True, "dry": True, **stats}
    rec_id, _ = resolve(device)
    letter = _letter(device)
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
        script(h, say, stats)
        say(f"day {day} done: {stats['reels_watched']} reels, {len(stats['follows'])} follows "
            f"({stats.get('follows_missed', 0)} missed), extras {stats['extras']}, popups {stats['popups']}")
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
