"""Create iCloud+ "Hide My Email" aliases for Instagram signups and track them in Airtable
(09.10, Rene: "neue E-Mail-Aliase erstellen, 10 pro Apple-ID am Tag, alles in Airtable tracken").

Apple has no public API for Hide My Email; icloud.com's web app talks to
    https://p<NN>-maildomainws.icloud.com/v1/hme/generate   -> {"result": {"hme": "<alias>@icloud.com"}}
    https://p<NN>-maildomainws.icloud.com/v1/hme/reserve    (hme, label, note) -> activates it
with the cookies of a logged-in icloud.com session.

The session lives in a Multilogin BROWSER profile that Rene logged into by hand (one per Apple
ID, named "Apple Id: <mail>", tagged "logged in", folder APPLE_FOLDER). Claude never touches
the Apple password. This module starts the profile through the local MLX launcher
(launcher.mlx.yt:45001, agent unit adbbot-mlx-agent), attaches Selenium, runs the two fetches
from inside the icloud.com page (same origin, cookies included), stops the profile again and
writes the alias to the Mail Aliases table (Status Free, Apple ID link).

First alias made this way 09.10 05:12: ado-zeolite-53@icloud.com (lolsabine3). Apple throttles
alias creation; we stay at <= PER_DAY per Apple ID, one profile at a time.

Runs under the Selenium venv (the bot's .venv has no selenium):
    /root/.adb_bot/venv-browser/bin/python -m adb_bot.automation.icloud_aliases create <apple id> [--n 1]
    /root/.adb_bot/venv-browser/bin/python -m adb_bot.automation.icloud_aliases fill      # all IDs up to PER_DAY
    /root/.adb_bot/venv-browser/bin/python -m adb_bot.automation.icloud_aliases status
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time

import requests
import urllib3

log = logging.getLogger("icloud_aliases")

BASE = "appwk9LrgAhJv8iEH"
T_ALIAS, T_APPLE = "tblodjIl9a7B7LvHa", "tblX4tS6n1b4yy7nP"
A_ALIAS, A_APPLE, A_STATUS, A_NOTES = "Alias", "Apple ID", "Status", "Notes"
PER_DAY = int(os.environ.get("ICLOUD_ALIASES_PER_DAY", "15"))          # Rene 09.10: 15/day per Apple ID
MAX_TOTAL = int(os.environ.get("ICLOUD_ALIASES_MAX_TOTAL", "400"))
MIN_GAP = int(os.environ.get("ICLOUD_ALIASES_MIN_GAP", "3600"))  # Rene 09.10: at least 1 h between two aliases of one Apple ID     # stop an Apple ID near ~400 aliases
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Bangkok")                                          # the farm's day, not the server's (UTC)
except Exception:                                                          # noqa: BLE001
    TZ = dt.timezone(dt.timedelta(hours=7))
MIN_FREE = int(os.environ.get("ICLOUD_ALIASES_MIN_FREE", "0"))   # fill: stop early once this many Free exist (0 = always fill)

LAUNCHER = "https://launcher.mlx.yt:45001"
MLX_API = "https://api.multilogin.com"
APPLE_FOLDER = os.environ.get("ICLOUD_MLX_FOLDER", "f03f25bc-eb13-4ec1-9cd6-80f14b3f9255")
LOGGED_IN_TAG = "logged in"


def _hdr() -> dict:
    tok = os.environ.get("AIRTABLE_TOKEN") or ""
    if not tok:
        raise RuntimeError("AIRTABLE_TOKEN is not set (source /etc/adbbot/env)")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _mlx_hdr() -> dict:
    tok = (os.environ.get("MULTILOGIN_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError("MULTILOGIN_TOKEN is not set (source /etc/adbbot/env)")
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json", "Content-Type": "application/json"}


# ---------------------------------------------------------------- Airtable

def apple_ids() -> list[dict]:
    r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_APPLE}", headers=_hdr(), timeout=30)
    r.raise_for_status()
    return r.json().get("records", [])


def aliases_of(apple_mail: str) -> list[dict]:
    """ARRAYJOIN of a link field gives the linked primary field (the mail), not record ids.
    Paged: an Apple ID heads for ~400 aliases, a single request returns at most 100."""
    out, offset = [], None
    while True:
        params = {"filterByFormula": f"FIND('{apple_mail}', ARRAYJOIN({{Apple ID}}))", "pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_ALIAS}", headers=_hdr(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out


def _local_day(created: str) -> str:
    return dt.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(TZ).date().isoformat()


def aliases_today(apple_mail: str, als: list | None = None) -> int:
    today = dt.datetime.now(TZ).date().isoformat()
    return sum(1 for a in (als if als is not None else aliases_of(apple_mail)) if a.get("createdTime") and _local_day(a["createdTime"]) == today)


def slots(apple_mail: str, day: str | None = None) -> list[int]:
    """PER_DAY moments (seconds after local midnight) spread over the whole day, different for every
    Apple ID and every day (Rene 09.10: "unabhängig, immer zu verschiedenen Zeiten, auf 24 Stunden"):
    one per equal slice of the day at a random point inside it, so they never bunch up."""
    import random
    day = day or dt.datetime.now(TZ).date().isoformat()
    rnd = random.Random(f"{apple_mail}|{day}")
    width = 86400 / PER_DAY                       # 96 min for 15/day
    jitter = max(0.0, width - MIN_GAP)            # random start inside the slice, but neighbours stay >= MIN_GAP apart
    return [int(i * width + rnd.uniform(0, jitter)) for i in range(PER_DAY)]


def free_aliases() -> int:
    r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_ALIAS}", headers=_hdr(),
                     params={"filterByFormula": "{Status}='Free'"}, timeout=30)
    r.raise_for_status()
    return len(r.json().get("records", []))


def record_alias(address: str, apple_rec: str, note: str) -> str:
    r = requests.post(f"https://api.airtable.com/v0/{BASE}/{T_ALIAS}", headers=_hdr(),
                      json={"fields": {A_ALIAS: address, A_APPLE: [apple_rec], A_STATUS: "Free", A_NOTES: note}, "typecast": True}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


# ---------------------------------------------------------------- Multilogin browser profile

def find_profile(apple_id: str) -> str | None:
    """The browser profile Rene logged into for this Apple ID ("Apple Id: <mail>", tag "logged in")."""
    r = requests.post(f"{MLX_API}/profile/search", headers=_mlx_hdr(), timeout=30,
                      json={"is_removed": False, "limit": 100, "offset": 0, "search_text": apple_id,
                            "order_by": "created_at", "sort": "desc", "folder_id": APPLE_FOLDER})
    r.raise_for_status()
    for p in (r.json().get("data") or {}).get("profiles") or []:
        name = (p.get("name") or "").strip().lower()
        if name == f"apple id: {apple_id.lower()}" and LOGGED_IN_TAG in [t.lower() for t in (p.get("tags") or [])]:
            return p["id"]
    return None


def start_profile(profile_id: str, tries: int = 40) -> str:
    """Returns the WebDriver port. CORE_DOWNLOADING_STARTED is progress, not failure."""
    h = {"Authorization": _mlx_hdr()["Authorization"], "Accept": "application/json"}
    url = f"{LAUNCHER}/api/v2/profile/f/{APPLE_FOLDER}/p/{profile_id}/start?automation_type=selenium&headless_mode=false"
    for i in range(tries):
        try:
            r = requests.get(url, headers=h, timeout=300, verify=False)
        except requests.ConnectionError as e:
            raise RuntimeError(f"MLX launcher not reachable on :45001 -- is adbbot-mlx-agent up (launcher self-update trap)? {e}")
        body = r.json() if r.content else {}
        code = ((body.get("status") or {}).get("error_code") or "")
        if r.status_code == 200 and (body.get("data") or {}).get("port"):
            return str(body["data"]["port"])
        if code in ("CORE_DOWNLOADING_STARTED", "CORE_DOWNLOADING"):
            log.info("browser core downloading (%d/%d)", i + 1, tries)
            time.sleep(10)
            continue
        if code == "PROFILE_ALREADY_RUNNING" or "already" in str(body).lower():
            stop_profile(profile_id)
            time.sleep(3)
            continue
        raise RuntimeError(f"profile start failed {r.status_code}: {body}")
    raise RuntimeError("profile start: core still downloading after retries")


def stop_profile(profile_id: str) -> None:
    h = {"Authorization": _mlx_hdr()["Authorization"], "Accept": "application/json"}
    try:
        requests.get(f"{LAUNCHER}/api/v1/profile/stop/p/{profile_id}", headers=h, timeout=60, verify=False)
    except Exception as e:                                    # noqa: BLE001
        log.warning("stop profile %s: %s", profile_id, e)


_FETCH_JS = """
const [url, body, done] = [arguments[0], arguments[1], arguments[arguments.length-1]];
fetch(url, {method:'POST', credentials:'include', body: body,
  headers:{'Origin':'https://www.icloud.com','Content-Type':'text/plain'}})
 .then(r => r.text().then(t => done([r.status, t])))
 .catch(e => done(['ERR', String(e)]));
"""


class ICloudSession:
    """Selenium attached to the running MLX profile; all iCloud calls run inside the page."""

    def __init__(self, port: str):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        self.d = webdriver.Remote(command_executor=f"http://127.0.0.1:{port}", options=Options())
        self.d.set_page_load_timeout(90)
        self.d.set_script_timeout(60)
        self.host: str | None = None

    def open(self) -> None:
        if "icloud.com" not in (self.d.current_url or ""):
            self.d.get("https://www.icloud.com/icloudplus/")
            time.sleep(10)

    def post(self, url: str, body: str = "{}"):
        st, t = self.d.execute_async_script(_FETCH_JS, url, body)
        try:
            return st, json.loads(t)
        except Exception:                                     # noqa: BLE001
            return st, t[:300]

    def validate(self) -> dict:
        st, v = self.post("https://setup.icloud.com/setup/ws/1/validate")
        if st in (421, 450, 401) or not isinstance(v, dict) or "webservices" not in v:
            raise PermissionError(f"iCloud session not logged in ({st}) -- Rene signs into icloud.com in the Multilogin profile again")
        # karouiemilio (09.10): flag missing entirely -> generate answers a bare 403. No iCloud+ = no aliases.
        if not (v.get("dsInfo") or {}).get("isHideMyEmailSubscriptionActive"):
            raise RuntimeError("Hide My Email is not active on this Apple ID (no iCloud+ subscription)")
        self.host = v["webservices"]["premiummailsettings"]["url"].rstrip("/")
        return v

    def create_alias(self, label: str, note: str = "Instagram signup (farm)") -> str:
        if not self.host:
            self.validate()
        st, g = self.post(f"{self.host}/v1/hme/generate")
        hme = (g.get("result") or {}).get("hme") if isinstance(g, dict) else None
        if st != 200 or not hme:
            raise RuntimeError(f"generate failed {st}: {g}")
        st, r = self.post(f"{self.host}/v1/hme/reserve", json.dumps({"hme": hme, "label": label, "note": note}))
        if st != 200 or not (isinstance(r, dict) and r.get("success", True)):
            raise RuntimeError(f"reserve failed {st}: {r}")
        return hme

    def close(self) -> None:
        try:
            self.d.quit()
        except Exception:                                     # noqa: BLE001
            pass


# ---------------------------------------------------------------- work

def create_for(apple_id: str, rec: dict, n: int, dry_run: bool = False) -> list[str]:
    done_today = aliases_today(apple_id)
    room = max(0, PER_DAY - done_today)
    if room == 0:
        log.info("%s: already %d aliases today (cap %d)", apple_id, done_today, PER_DAY)
        return []
    n = min(n, room)
    pid = find_profile(apple_id)
    if not pid:
        log.warning("%s: no Multilogin profile 'Apple Id: %s' tagged '%s' in folder %s", apple_id, apple_id, LOGGED_IN_TAG, APPLE_FOLDER)
        return []
    if dry_run:
        print(f"would create {n} alias(es) for {apple_id} via profile {pid}")
        return []
    made: list[str] = []
    port = start_profile(pid)
    s = ICloudSession(port)
    try:
        s.open()
        s.validate()
        for i in range(n):
            label = f"farm {dt.datetime.now(TZ).date().isoformat()} #{done_today + i + 1}"
            hme = s.create_alias(label)
            rid = record_alias(hme, rec["id"], f"Erstellt {dt.datetime.now(TZ):%d.%m. %H:%M} (Thai-Zeit) automatisch über Multilogin-Browserprofil ({label})")
            log.info("%s: alias %s -> Airtable %s", apple_id, hme, rid)
            made.append(hme)
            if i + 1 < n:
                time.sleep(20 + 10 * i)                       # Apple throttles bursts
    finally:
        s.close()
        stop_profile(pid)
    return made


# ---------------------------------------------------------------- long-running service

def _notify(text: str) -> None:
    log.warning("ALERT %s", text)
    try:
        from adb_bot.clients.telegram import TelegramNotifier
        TelegramNotifier().send(f"✉️ <b>iCloud aliases</b>\n{text}", logger=log, category="phones")
    except Exception as exc:                                  # noqa: BLE001
        log.warning("telegram failed: %s", exc)


def serve(keepalive: float = 600, loop: float = 60) -> None:
    """Keep every Apple ID's browser profile OPEN on the server and its icloud.com session warm.
    Rene 09.10: logins kept dropping within an hour -- the old timer started the profile for one
    alias and closed it again (a session-only cookie dies with the browser). Here the profile stays
    up, `validate` every `keepalive` s keeps iCloud renewing the session, and an alias is made in the
    already-open browser when that Apple ID's next random slot of the day has passed."""
    sessions: dict[str, tuple[str, ICloudSession]] = {}      # apple id -> (profile id, session)
    last_ok: dict[str, float] = {}
    alerted: set[str] = set()
    pause: dict[str, float] = {}                              # apple id -> no new alias before this time
    while True:
        ids = {r["fields"].get("Apple ID", ""): r for r in apple_ids()}
        now = dt.datetime.now(TZ)
        sec = now.hour * 3600 + now.minute * 60 + now.second
        for mail, rec in ids.items():
            if not mail or str(rec["fields"].get("Status", "")).lower() in ("inactive", "dead", "banned", "gesperrt"):
                continue
            try:
                if mail not in sessions:
                    pid = find_profile(mail)
                    if not pid:
                        continue
                    s = ICloudSession(start_profile(pid))
                    s.open()
                    sessions[mail] = (pid, s)
                    last_ok[mail] = 0
                pid, s = sessions[mail]
                if time.time() - last_ok.get(mail, 0) >= keepalive:
                    s.validate()                                  # keeps the login alive
                    last_ok[mail] = time.time()
                    if mail in alerted:
                        alerted.discard(mail)
                        _notify(f"{mail}: icloud.com session is back.")
                als = aliases_of(mail)
                if (time.time() >= pause.get(mail, 0) and len(als) < MAX_TOTAL
                        and aliases_today(mail, als) < sum(1 for x in slots(mail) if x <= sec)):
                    label = f"farm {now.date().isoformat()} #{aliases_today(mail, als) + 1}"
                    hme = s.create_alias(label)
                    rid = record_alias(hme, rec["id"], f"Erstellt {dt.datetime.now(TZ):%d.%m. %H:%M} (Thai-Zeit) automatisch über Multilogin-Browserprofil ({label})")
                    log.info("%s: alias %s -> Airtable %s", mail, hme, rid)
                    # catching up missed slots one per minute ran into Apple's "-41015" limit (09.10):
                    # never two aliases of the same Apple ID less than MIN_GAP apart
                    pause[mail] = time.time() + MIN_GAP
            except PermissionError as exc:
                if mail not in alerted:
                    alerted.add(mail)
                    _notify(f"{mail}: not logged in on icloud.com any more -- open its Multilogin profile, sign in "
                            f"with 'Keep me signed in', close it again. ({exc})")
                _drop(sessions, mail)
            except Exception as exc:                              # noqa: BLE001 -- next Apple ID still runs
                if "-41015" in str(exc):                          # Apple: "limit of addresses you can create right now"
                    pause[mail] = time.time() + 2 * 3600          # keep the session, just wait (09.10: hit after a catch-up burst)
                    log.warning("%s: Apple alias limit reached -- pausing this Apple ID for 2 h", mail)
                    continue
                log.error("%s: %s -- reopening the profile next round", mail, exc)
                _drop(sessions, mail)
        time.sleep(loop)


def _drop(sessions: dict, mail: str) -> None:
    pid, s = sessions.pop(mail, (None, None))
    if s:
        try:
            s.d.quit()
        except Exception:                                     # noqa: BLE001
            pass
    if pid:
        stop_profile(pid)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    urllib3.disable_warnings()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("serve", help="service: keep the profiles open + sessions warm, make aliases at their slots")
    c = sub.add_parser("create")
    c.add_argument("apple_id")
    c.add_argument("--n", type=int, default=1)
    c.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("tick", help="timer: one alias per Apple ID whose next random slot of the day has passed")
    t.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("fill", help="every Apple ID with a logged-in profile up to the daily cap (timer)")
    f.add_argument("--per-run", type=int, default=int(os.environ.get("ICLOUD_ALIASES_PER_RUN", "2")))
    f.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    ids = {r["fields"].get("Apple ID", ""): r for r in apple_ids()}
    if a.cmd == "status":
        for mail, rec in ids.items():
            pid = find_profile(mail)
            als = aliases_of(mail)
            free = sum(1 for x in als if x["fields"].get("Status") == "Free")
            print(f"{mail:32} {rec['fields'].get('Status', '')!s:10} today {aliases_today(mail)}/{PER_DAY}  free {free}/{len(als)}  profile: {pid or '-'}")
        print(f"free aliases total: {free_aliases()}")
        return 0
    if a.cmd == "serve":
        serve()
        return 0
    if a.cmd == "tick":
        now = dt.datetime.now(TZ)
        sec = now.hour * 3600 + now.minute * 60 + now.second
        made = []
        for mail, rec in ids.items():
            if not mail or str(rec["fields"].get("Status", "")).lower() in ("inactive", "dead", "banned", "gesperrt"):
                continue
            als = aliases_of(mail)
            due = sum(1 for x in slots(mail) if x <= sec)
            done = aliases_today(mail, als)
            if len(als) >= MAX_TOTAL:
                log.info("%s: %d aliases -- reached the %d cap, no more", mail, len(als), MAX_TOTAL)
                continue
            if done >= due:
                continue
            log.info("%s: slot due (%d/%d today, %d due by %s)", mail, done, PER_DAY, due, now.strftime("%H:%M"))
            try:
                made += create_for(mail, rec, 1, dry_run=a.dry_run)
            except Exception as exc:                                   # noqa: BLE001 -- next Apple ID still runs
                log.error("%s: %s", mail, exc)
        print(f"tick: created {len(made)}: {made}")
        return 0
    if a.cmd == "fill":
        if MIN_FREE and free_aliases() >= MIN_FREE:
            print(f"fill: {free_aliases()} free aliases >= ICLOUD_ALIASES_MIN_FREE {MIN_FREE} -- nothing to do")
            return 0
        total = []
        for mail, rec in ids.items():
            if str(rec["fields"].get("Status", "")).lower() in ("inactive", "dead", "banned", "gesperrt"):
                continue
            try:
                total += create_for(mail, rec, a.per_run, a.dry_run)
            except Exception as e:                            # noqa: BLE001
                log.error("%s: %s", mail, e)
        print(f"fill: created {len(total)}: {total}")
        return 0
    rec = ids.get(a.apple_id)
    if not rec:
        print(f"{a.apple_id} is not in the Apple IDs table")
        return 1
    made = create_for(a.apple_id, rec, a.n, a.dry_run)
    print(f"created {len(made)}: {made}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
