"""Post spoofed Nikki reels on the iPhone accounts, XXTouch only -- in batches of 3 per account
(Rene 09.10): post, scroll 1 min, post, scroll 1 min, post, scroll 1 min = one batch; then that account rests
2-4 h before its next batch. 5 clips -> batch of 3 + batch of 2; 7 -> 3 + 3 + 1.

    .venv/bin/python3 -m adb_bot.automation.iphone_xx_post_batch 2026-10-09 A1,A2,A4,A6 B1,B2,B3,B4,B5
        [--reels N (default: every clip of the day)] [--batch 3] [--scroll 60] [--pause 2-4] [--window 9-23]

One thread per phone (all phones in parallel); on one phone the accounts take turns -- whichever account is
due next gets its batch. Each account gets its own spoof variant NN and starts on a different clip.
Clips: /opt/adbbot/manual_spoof/<date>/extra/Nikki/DE/<clip>/<clip>_xNN.mp4
State (what is posted + when each account may post again; re-runs resume from it):
/opt/adbbot/logs/xx_post_<date>.json. --window (Berlin) only decides when an account's FIRST batch may start;
later batches run whenever they are due, also after the window -- posting never stops until everything is out.
"""
from __future__ import annotations

import argparse
import fcntl
import datetime as dt
import json
import logging
import os
import random
import sys
import threading
import time

import requests
from pathlib import Path
from zoneinfo import ZoneInfo

from adb_bot.automation import iphone_diag
from adb_bot.automation import iphone_phone_lock as phone_lock
from adb_bot.automation.iphone_xx_flows import XXLicenceError, IG, XX, FlowError, post_reel, scroll_feed

log = logging.getLogger("xx_post")
PHONE_REF = {"A": "iphone1", "B": "iphone2", "C": "iphone3", "D": "iphone4", "E": "iphone5"}
AIRTABLE_BASE, T_ACC, T_CONT = "appwk9LrgAhJv8iEH", "tbly0y1I4hNDlvh1K", "tblyZXN7caKZZBaBj"
# 11.10 (Rene): before an account's very first post ever, scroll 5 min if it has never been
# warmed up at all, or 1 min if it already has a day of warmup behind it (today's new signups
# count as "already warmed up" the moment their first warmup ran) -- after that first post the
# existing per-post scroll below (post, scroll, post, scroll...) already does exactly 1 min.
LEAD_SCROLL_NEW, LEAD_SCROLL_WARMED = 300.0, 60.0


def _account_row_id(container: str) -> str | None:
    """The Airtable Accounts row currently linked to this container, or None."""
    import requests
    tok = os.environ["AIRTABLE_TOKEN"]
    hdr = {"Authorization": f"Bearer {tok}"}
    r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{T_CONT}",
                     headers=hdr, params={"filterByFormula": f"{{Container}}='{container}'"}, timeout=30)
    recs = r.json().get("records", [])
    if not recs:
        return None
    acc_ids = recs[0]["fields"].get("Accounts") or []
    return acc_ids[0] if acc_ids else None


def _has_warmup(container: str) -> bool:
    """True if the account currently in this container has a Warm-up Start date set."""
    import requests
    row = _account_row_id(container)
    if not row:
        return True                                           # unknown container: don't add an extra scroll
    tok = os.environ["AIRTABLE_TOKEN"]
    acc = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{T_ACC}/{row}",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=30).json()
    return bool(acc.get("fields", {}).get("Warm-up Start"))


def _mark_posted(container: str) -> None:
    """11.10 (Rene): log every real post in Airtable too, not only the local state file --
    'Last Posted' (date) and 'Posted Today' (checkbox), per account."""
    import requests
    row = _account_row_id(container)
    if not row:
        return
    tok = os.environ["AIRTABLE_TOKEN"]
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    body = {"fields": {"Last Posted": dt.datetime.now(BERLIN).isoformat(), "Posted Today": True}, "typecast": True}
    try:
        requests.patch(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{T_ACC}/{row}", headers=hdr, json=body, timeout=30)
    except Exception as exc:                                  # noqa: BLE001 -- never let a logging call stop a post
        log.warning("%s: could not update Airtable Last Posted (%s)", container, exc)
# 11.10: rebuilt from Airtable (Creation Status=Verified, Status not Human Verification/Review,
# D1 dropped -- its Airtable row and the real logged-in account on the container disagree, see
# Notes on that row). Every account "wir auf allen posten" today, 27 total. Numbers only need to
# be unique and <= EXTRA_VARIANTS in manual_spoof.py; they don't need to mean anything by phone.
VARIANT = {"A1": 1, "A2": 2, "A4": 3, "A6": 4, "A7": 5,
           "B1": 6, "B10": 7, "B11": 8, "B2": 9, "B3": 10, "B4": 11, "B5": 12, "B8": 13,
           "C10": 14, "C11": 15, "C12": 16, "C2": 17, "C3": 18, "C6": 19, "C7": 20, "C8": 21,
           "D13": 22, "D2": 23, "D4": 24, "D5": 25,
           "E1": 26, "E4": 27}
_lock = threading.Lock()
BERLIN = ZoneInfo("Europe/Berlin")
VARIANT_FILE = Path("/opt/adbbot/logs/xx_post_variants.json")
ACTIVE: set[str] = set()      # containers posting in this run (their fixed VARIANT numbers stay reserved)


def variant_for(model: str, container: str, limit: int) -> int:
    """13.10 (Rene: post on every Healthy account + the new Lea ones): containers outside the fixed VARIANT
    table get the next free spoof variant, remembered in VARIANT_FILE so an account keeps its number on
    every run (two accounts must never post the same file). Nikki numbers never collide with the table."""
    if model == "Nikki" and container in VARIANT:
        return VARIANT[container]
    with _lock:
        try:
            m = json.loads(VARIANT_FILE.read_text())
        except (OSError, ValueError):
            m = {}
        own = m.setdefault(model, {})
        if container not in own:
            # 13.10: all 40 Nikki numbers were "taken" by the fixed table incl. accounts that no longer post
            # (A7, B8, D2, D4 ...) -- D12 found none and phone D's whole thread died. Reserve only the numbers
            # of accounts actually posting today; a parked account's number is free again.
            taken = set(own.values()) | ({VARIANT[c] for c in ACTIVE if c in VARIANT} if model == "Nikki" else set())
            free = [v for v in range(1, limit + 1) if v not in taken]
            if not free:
                raise RuntimeError(f"no free {model} spoof variant left for {container} (limit {limit})")
            own[container] = free[0]
            VARIANT_FILE.write_text(json.dumps(m, indent=1))
        return own[container]


def _state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _mark(path: Path, container: str, clip: str):
    with _lock:
        st = _state(path)
        st.setdefault(container, []).append({"clip": clip, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        path.write_text(json.dumps(st, indent=1))


LICENCE_RETRY = 600          # seconds between licence re-checks while a phone's XXTouch says 403


def _tg(msg: str) -> None:
    try:
        from adb_bot.clients.telegram import TelegramNotifier
        TelegramNotifier().send(f"📱 <b>iPhone farm</b>\n{msg}", logger=log, category="phones")
    except Exception as exc:                              # noqa: BLE001
        log.warning("telegram failed: %s", exc)


def _licence_out(letter: str, containers: list, why: str) -> None:
    """22.10: XXTouch's Havoc licence on this phone answers 403. Nothing is skipped (Rene: we must post): the phone
    waits and retries every LICENCE_RETRY s; this just says once which phone it is and what usually fixes it."""
    msg = (f"iPhone {letter}: XXTouch says 'no licence' (403) -- {len(containers)} account(s) waiting "
           f"({', '.join(containers)}). Retrying every {LICENCE_RETRY // 60} min with XXTExplorer re-sync. If it stays: "
           f"Sileo -> Havoc -> login on this phone, or the licence check is stuck behind the proxy. ({why[:100]})")
    log.error(msg); _tg(msg)


def _licence_back(letter: str) -> None:
    msg = f"iPhone {letter}: XXTouch licence answers again -- posting continues."
    log.info(msg); _tg(msg)


def _licence_heal(letter: str) -> None:
    """Open XXTExplorer once + restart the XXTouch daemons (the heal that recovered A and C on 22.10) -- shared
    implementation in iphone_xxtouch.Touch._heal_licence, over SSH, no WDA needed."""
    try:
        from adb_bot.automation import iphone_xxtouch
        t = iphone_xxtouch.Touch(PHONE_REF[letter])
        t._lic_healed = False
        t._heal_licence()
    except Exception as exc:                              # noqa: BLE001
        log.warning("%s: licence heal failed (%s)", letter, exc)


def _skip(path: Path, container: str, why: str):
    """14.10: "skipped for today" lived only in memory -- after a restart E13 (phone-number check, then logged
    out) was tried again. Persist it; run_phone reads it back."""
    with _lock:
        st = _state(path)
        st.setdefault("_skipped", {})[container] = f"{time.strftime('%H:%M')} {why}"
        path.write_text(json.dumps(st, indent=1))


def _set_next(path: Path, container: str, when: float):
    with _lock:
        st = _state(path)
        st.setdefault("_next", {})[container] = round(when)
        path.write_text(json.dumps(st, indent=1))


def _next_due(st: dict, container: str, pause: tuple[float, float]) -> float:
    """When this account may start its next batch. Old state files have no "_next": then rest from its last post."""
    if container in st.get("_next", {}):
        return float(st["_next"][container])
    last = None
    for d in st.get(container, []):
        try:
            last = dt.datetime.strptime(d["at"][:16], "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            pass
    return last + random.uniform(*pause) if last else 0.0


def _window_open(window: tuple[int, int], t: float) -> bool:
    h = dt.datetime.fromtimestamp(t, BERLIN).hour
    return window[0] <= h < window[1]


def _window_next(window: tuple[int, int], t: float) -> float:
    """t itself if the window is open, else the next time it opens (Berlin)."""
    if _window_open(window, t):
        return t
    now = dt.datetime.fromtimestamp(t, BERLIN)
    start = now.replace(hour=window[0], minute=0, second=0, microsecond=0)
    if start <= now:
        start += dt.timedelta(days=1)
    return start.timestamp() + random.uniform(0, 900)


def run_phone(letter: str, containers: list[str], clips: list[Path], n: int, state: Path, batch: int,
              scroll: float, pause: tuple[float, float], window: tuple[int, int], model: str = "Nikki", limit: int = 40):
    # 14.10: with the Mac jump host refusing SSH for a while, XX() could not open its tunnel and the phone's whole
    # thread died at start (A, B, D, E did nothing all morning). Retry for ~30 min before giving the phone up.
    # 22.10 (Rene: "ueberspringen ist Quatsch, wir wollen posten"): a dead XXTouch licence never parks the phone.
    # Say it once (which phone, what to do), then keep healing + retrying every LICENCE_RETRY seconds until the
    # licence answers again (Havoc login done, proxy routed around, ...) -- the day's posts still go out.
    x, lic_said = None, False
    for attempt in range(30 * 6):
        try:
            x = XX(PHONE_REF[letter], log=lambda m: log.info("%s %s", letter, m))
            if lic_said:
                _licence_back(letter)
            break
        except XXLicenceError as exc:
            if not lic_said:
                _licence_out(letter, containers, str(exc)); lic_said = True
            _licence_heal(letter)
            time.sleep(LICENCE_RETRY)
        except Exception as exc:                          # noqa: BLE001
            log.warning("phone %s: XXTouch not reachable yet (%s) -- retry in 60 s", letter, exc)
            time.sleep(60)
    if x is None:
        log.error("phone %s: XXTouch unreachable/unlicensed all day -- phone given up", letter); return
    st = _state(state)
    queue, due, tries = {}, {}, {}
    lead_scroll_done = set()
    for c in containers:
        v = variant_for(model, c, limit)
        done = {d["clip"] for d in st.get(c, [])}
        files = [clips[(v - 1 + k) % len(clips)] for k in range(min(n, len(clips)))]
        queue[c] = [f for f in ((d / f"{d.name}_x{v:02d}.mp4") for d in files) if f.name not in done]
        if c in st.get("_skipped", {}):
            log.info("%s: skipped today (%s)", c, st["_skipped"][c]); queue[c] = []
        due[c] = _next_due(st, c, pause)
        if not st.get(c):                                 # an account's FIRST batch starts inside the window
            due[c] = _window_next(window, max(due[c], time.time()))
        log.info("%s: %d to post%s", c, len(queue[c]),
                 f", next batch {dt.datetime.fromtimestamp(due[c], BERLIN):%H:%M} Berlin" if due[c] > time.time() else "")
    batch_fd = None
    while True:
        open_ = [c for c in containers if queue[c]]
        if not open_:
            log.info("phone %s: everything posted", letter); return
        c = min(open_, key=lambda k: due[k])
        wait = due[c] - time.time()
        if wait > 0:
            log.info("phone %s: next batch %s at %s Berlin", letter, c, dt.datetime.fromtimestamp(due[c], BERLIN).strftime("%H:%M"))
            time.sleep(wait)
        try:
            # 10.10 (Rene): a signup that is due goes first -- wait here, before the Shortcut, until it is done
            waited = phone_lock.wait_free(letter, log=lambda m: log.info("phone %s: %s", letter, m))
            if waited > 1:
                log.info("phone %s: signup finished, posting goes on (waited %.0f min)", letter, waited / 60)
            # 13.10: Nikki and Lea post as two runs on phone E. The per-post lock below came AFTER the Shortcut,
            # so one run could switch the container while the other was posting -> a reel on the wrong account.
            # One batch (Shortcut + its posts) now owns the phone from start to end.
            batch_fd = open(f"/tmp/xx_post_batch_{letter}.lock", "w")
            fcntl.flock(batch_fd, fcntl.LOCK_EX)
            x.open_container(c)
            if c not in st and c not in lead_scroll_done:
                lead = LEAD_SCROLL_WARMED if _has_warmup(c) else LEAD_SCROLL_NEW
                log.info("%s: first post ever -- scrolling %.0f s first", c, lead)
                scroll_feed(x, lead)
                lead_scroll_done.add(c)
            this = queue[c][:batch]
            yielded = False
            for i, f in enumerate(this):
                # ...and between two posts: the current post (with its scroll) is always finished first
                held = None if phone_lock.signup_wanted(letter) else phone_lock.try_hold(letter)
                if held is None:
                    log.info("%s: a signup wants phone %s -- batch paused after %d/%d, continues right after it", c, letter, i, len(this))
                    yielded = True
                    break
                with held:
                    x.video_to_album(str(f))
                    log.info("%s: posting %s (%d/%d of this batch)", c, f.name, i + 1, len(this))
                    post_reel(x, str(f), log=lambda m: log.info("%s%s", c, m))
                    _mark(state, c, f.name)
                    _mark_posted(c)
                    queue[c].remove(f)
                    log.info("%s: scrolling %.0f s", c, scroll)
                    scroll_feed(x, random.uniform(scroll * 0.9, scroll * 1.15))
            fcntl.flock(batch_fd, fcntl.LOCK_UN); batch_fd.close(); batch_fd = None
            if yielded:
                due[c] = time.time()                      # the rest of this batch, as soon as the phone is free again
                continue
            due[c] = time.time() + random.uniform(*pause)
            _set_next(state, c, due[c])
            log.info("%s: batch done, %d left, next batch %s Berlin", c, len(queue[c]),
                     dt.datetime.fromtimestamp(due[c], BERLIN).strftime("%H:%M") if queue[c] else "-")
        except Exception as exc:                          # noqa: BLE001
            if batch_fd is not None:
                fcntl.flock(batch_fd, fcntl.LOCK_UN); batch_fd.close(); batch_fd = None
            # 11.10: was `except (FlowError, RuntimeError)` -- confirmed live the same run can throw
            # XXError (XXTouch tunnel/licence), requests.ConnectionError (SSH/tunnel reset) and a
            # plain NameError (a bug of mine). None of those are FlowError/RuntimeError, so each one
            # killed the whole phone thread outright -- every other account on that phone lost for
            # the rest of the day over one hiccup. Catching Exception broadly keeps the per-account
            # retry/skip logic below in charge instead.
            # 22.10 (Rene): every stop gets a screenshot first, so the real screen can be seen afterwards.
            shot = iphone_diag.keep_screen(x.ref, "stopped", f"{type(exc).__name__}: {exc}", container=c, xx=x,
                                           log_fn=lambda m: log.info("%s: %s", c, m))
            log.error("%s: STOPPED -- %s (screen: %s)", c, exc, shot or "none")
            if "requires a valid license" in str(exc) or isinstance(exc, XXLicenceError):
                # 22.10: licence gone mid-run -> do NOT burn the account's 3 tries: say it once, heal, wait, retry
                _licence_out(letter, [k for k in containers if queue.get(k)], str(exc))
                while True:
                    _licence_heal(letter)
                    time.sleep(LICENCE_RETRY)
                    try:
                        if x.t._post("/spawn", b"sys.msleep(1)\n").get("code") == 0:
                            _licence_back(letter); break
                    except Exception:                     # noqa: BLE001
                        pass
                due[c] = time.time() + 30                 # same account again, nothing was counted against it
                continue
            try:
                # 2026-10-19 Rene: "Confirm you're human" / "Enter your mobile number" screens are
                # Human Verification -- tag Status='Human Verification' in Airtable before skipping,
                # so morning triage sees it and the next account on this phone takes over.
                if x.front() == IG and x.find(r"confirm you.re human|Enter your mobile number|confirm this mobile number", 0):
                    log.error("%s: Human Verification on screen -- Status='Human Verification', skipped for today", c)
                    try:
                        row = _account_row_id(c)
                        if row:
                            tok = os.environ["AIRTABLE_TOKEN"]
                            hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
                            requests.patch(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{T_ACC}/{row}",
                                           headers=hdr, json={"fields": {"Status": "Human Verification"}, "typecast": True}, timeout=20)
                    except Exception as exc3:
                        log.warning("%s: could not set Airtable Status='Human Verification' (%s)", c, exc3)
                    _skip(state, c, "Human Verification")
                    queue[c] = []
                    continue
                if x.front() == IG and x.find(r"suspended|disabled your account|account has been|Try again later|We limit|restricted", 0):
                    # 13.10: was `return` (whole phone stopped) -- with every Healthy account in one run, one
                    # account in a human check would have cost all its neighbours their day. Skip just it.
                    log.error("%s: Instagram warning on screen -- account skipped for today", c)
                    _skip(state, c, "Instagram warning on screen")
                    queue[c] = []
                    continue
                # 2026-10-19 Rene: "Join Instagram" / "Get started" / "I already have a profile" means IG kicked
                # the account out -- same bucket as banned in effect, no sense retrying today. Tag "logged out"
                # in Airtable so the morning triage sees it and skip for today.
                if x.front() == IG and x.find(r"Join Instagram|Get started|I already have a profile", 0):
                    log.error("%s: Instagram logged this account out -- Status='logged out', skipped for today", c)
                    try:
                        row = _account_row_id(c)
                        if row:
                            tok = os.environ["AIRTABLE_TOKEN"]
                            hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
                            requests.patch(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{T_ACC}/{row}",
                                           headers=hdr, json={"fields": {"Status": "logged out"}, "typecast": True}, timeout=20)
                    except Exception as exc3:            # noqa: BLE001 -- tagging failure must not stop the thread
                        log.warning("%s: could not set Airtable Status='logged out' (%s)", c, exc3)
                    _skip(state, c, "logged out of Instagram")
                    queue[c] = []
                    continue
            except Exception as exc2:                     # noqa: BLE001 -- the ban-check itself must not be able to crash this thread
                log.warning("%s: ban-check failed too (%s) -- treating as a transient error", c, exc2)
            if "Done posting" in str(exc):                # may be live: never re-send blindly
                # 14.10: the skip only lived in memory -- after a restart B3 re-posted the very clip whose upload had
                # been "unknown" (nikki_4 x10, 03:33 then again 04:27). Record it in the state file as done, so no
                # restart can ever send it again; a person checks the profile instead.
                try:
                    _mark(state, c, f.name)
                except Exception:                         # noqa: BLE001
                    pass
                log.error("%s: upload state unknown -- account skipped, check its profile", c); queue[c] = []
            else:
                tries[c] = tries.get(c, 0) + 1
                if tries[c] > 3:
                    log.error("%s: 3 failed tries -- account skipped for today", c); queue[c] = []
                    _skip(state, c, "3 failed tries")
                else:
                    due[c] = time.time() + random.uniform(600, 1200)    # try this account again in 10-20 min
        try:
            x.pause(20, 40)
        except Exception:                                 # noqa: BLE001 -- even the idle pause must not be able to kill the thread
            time.sleep(random.uniform(20, 40))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("date")
    ap.add_argument("groups", nargs="+", help="comma lists per phone, e.g. A1,A2 B1,B2")
    ap.add_argument("--reels", type=int, default=0, help="reels per account today (default: every clip)")
    ap.add_argument("--batch", type=int, default=3)
    ap.add_argument("--scroll", type=float, default=60, help="seconds of feed scrolling after every post")
    ap.add_argument("--pause", default="2-4", help="hours an account rests between batches, e.g. 2-4")
    ap.add_argument("--window", default="9-23", help="hours (Berlin) in which the first batch may start")
    ap.add_argument("--model", default="Nikki", help="whose clips: extra/<model>/DE (13.10: Lea too)")
    ap.add_argument("--tag", default="", help="extra state/log suffix, for a second run next to a running one")
    a = ap.parse_args(argv)
    pause = tuple(float(v) * 3600 for v in a.pause.split("-"))
    window = tuple(int(v) for v in a.window.split("-"))
    logs = Path("/opt/adbbot/logs"); logs.mkdir(parents=True, exist_ok=True)
    suffix = ("" if a.model == "Nikki" else f"_{a.model}") + (f"_{a.tag}" if a.tag else "")   # own state per run
    ACTIVE.update(c for g in a.groups for c in g.split(","))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(logs / f"xx_post_{a.date}{suffix}.log")])
    clips = sorted(p for p in Path(f"/opt/adbbot/manual_spoof/{a.date}/extra/{a.model}/DE").iterdir() if p.is_dir())
    limit = min(len(list(c.glob("*_x*.mp4"))) for c in clips)        # variants made per clip (Nikki 40, Lea 13)
    state = logs / f"xx_post_{a.date}{suffix}.json"
    ths = [threading.Thread(target=run_phone, args=(g.split(",")[0][0], g.split(","), clips, a.reels or len(clips), state,
                                                    a.batch, a.scroll, pause, window, a.model, limit))
           for g in a.groups]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    log.info("state: %s", json.dumps(_state(state)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
