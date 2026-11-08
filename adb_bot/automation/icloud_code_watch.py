"""Write every new code that arrives at a Hide My Email alias into Airtable, within seconds
(Rene 09.10: "nicht immer Claude fragen, der Code soll direkt in Airtable stehen").

Runs as a service (adbbot-icloud-codes). One IMAP connection per iCloud inbox from
/etc/adbbot/icloud_mail.json (same app-specific passwords as icloud_codes -- they never expire,
unlike the icloud.com browser sessions). Every POLL seconds it looks for mails with a UID it has
not seen yet in INBOX and Junk (iCloud files many forwards under Junk), takes the alias from To:
(forwards keep "Hide My Email <alias>"), finds the code and writes it on that alias's row in
Mail Aliases: "Letzter Code", "Code erhalten" (Thai time in the UI), "Code Absender".

    .venv/bin/python -m adb_bot.automation.icloud_code_watch          # service
    .venv/bin/python -m adb_bot.automation.icloud_code_watch --once   # one pass, for testing
"""

from __future__ import annotations

import argparse
import datetime as dt
import email
import imaplib
import logging
import os
import re
import sys
import time
from email.utils import getaddresses, parsedate_to_datetime

import requests

from adb_bot.automation.icloud_codes import CODE_RE, FOLDERS, HOST, _text, accounts

log = logging.getLogger("icloud_code_watch")

BASE, T_ALIAS, T_CONT = "appwk9LrgAhJv8iEH", "tblodjIl9a7B7LvHa", "tblyZXN7caKZZBaBj"
POLL = float(os.environ.get("ICLOUD_CODE_POLL", "6"))
LOOKBACK = 2 * 3600            # on (re)start, also pick up codes from the last two hours
SYNC_EVERY = float(os.environ.get("ICLOUD_STATUS_SYNC", "60"))   # Used/Belegt sync (Rene 10.10: 10 min was too slow)


def _hdr() -> dict:
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}


def _alias_row(address: str) -> str | None:
    r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_ALIAS}", headers=_hdr(), timeout=30,
                     params={"filterByFormula": f"LOWER({{Alias}})='{address.lower()}'", "maxRecords": 1})
    r.raise_for_status()
    recs = r.json().get("records", [])
    return recs[0]["id"] if recs else None


def _write(address: str, code: str, when: float, sender: str) -> bool:
    rid = _alias_row(address)
    if not rid:
        log.info("code %s for %s -- alias not in Mail Aliases, skipped", code, address)
        return False
    r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{T_ALIAS}/{rid}", headers=_hdr(), timeout=30,
                       json={"fields": {"Letzter Code": code,
                                        "Code erhalten": dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat(),
                                        "Code Absender": sender[:200]}})
    r.raise_for_status()
    log.info("code %s -> %s (%s)", code, address, sender[:60])
    return True


def sync_status() -> tuple[int, int]:
    """Keep "free" honest (Rene 09.10: used aliases/containers still showed up as free in Account Creation):
    Mail Aliases linked to an account -> Used; Containers with an account -> Belegt, without one -> Frei
    (Defekt untouched)."""
    def allrec(t):
        out, off = [], None
        while True:
            p = {"pageSize": 100, **({"offset": off} if off else {})}
            j = requests.get(f"https://api.airtable.com/v0/{BASE}/{t}", headers=_hdr(), params=p, timeout=30).json()
            out += j.get("records", []); off = j.get("offset")
            if not off:
                return out

    def patch(t, ups):
        for i in range(0, len(ups), 10):
            requests.patch(f"https://api.airtable.com/v0/{BASE}/{t}", headers=_hdr(), timeout=30,
                           json={"records": ups[i:i + 10], "typecast": True}).raise_for_status()

    a_ups = [{"id": a["id"], "fields": {"Status": "Used"}} for a in allrec(T_ALIAS)
             if a["fields"].get("Instagram account") and a["fields"].get("Status") not in ("Used", "Burned")]
    c_ups = []
    for c in allrec(T_CONT):
        st, has = c["fields"].get("Status"), bool(c["fields"].get("Accounts"))
        if has and st not in ("Belegt", "Defekt"):
            c_ups.append({"id": c["id"], "fields": {"Status": "Belegt"}})
        elif not has and st == "Belegt":
            c_ups.append({"id": c["id"], "fields": {"Status": "Frei"}})
    patch(T_ALIAS, a_ups); patch(T_CONT, c_ups)
    if a_ups or c_ups:
        log.info("status sync: %d aliases -> Used, %d containers corrected", len(a_ups), len(c_ups))
    return len(a_ups), len(c_ups)


class Inbox:
    def __init__(self, apple_id: str, cred: dict):
        self.apple_id, self.cred = apple_id, cred
        self.M: imaplib.IMAP4_SSL | None = None
        self.seen: dict[str, int] = {}          # folder -> highest UID handled

    def connect(self):
        self.M = imaplib.IMAP4_SSL(HOST, 993, timeout=60)
        self.M.login(self.cred["imap_user"], self.cred["app_password"])

    def poll(self) -> int:
        if self.M is None:
            self.connect()
        n = 0
        for folder in FOLDERS:
            if self.M.select(folder, readonly=True)[0] != "OK":
                continue
            if folder not in self.seen:                   # first pass: only the last two hours
                since = (dt.date.today() - dt.timedelta(days=1)).strftime("%d-%b-%Y")
                typ, ids = self.M.uid("search", None, f"SINCE {since}")
                uids = [int(u) for u in (ids[0] or b"").split()]
                self.seen[folder] = 0
                todo = uids
            else:
                typ, ids = self.M.uid("search", None, f"UID {self.seen[folder] + 1}:*")
                todo = [int(u) for u in (ids[0] or b"").split() if int(u) > self.seen[folder]]
            for uid in todo:
                self.seen[folder] = max(self.seen[folder], uid)
                data = self.M.uid("fetch", str(uid), "(BODY.PEEK[])")[1]
                if not data or not isinstance(data[0], tuple):
                    continue
                msg = email.message_from_bytes(data[0][1])
                try:
                    when = parsedate_to_datetime(msg["Date"]).timestamp()
                except Exception:                            # noqa: BLE001
                    when = time.time()
                if when < time.time() - LOOKBACK:
                    continue
                subject = str(msg["Subject"] or "")
                codes = [c.replace(" ", "") for c in CODE_RE.findall(subject + " " + _text(msg))]
                if not codes:
                    continue
                to = [a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Delivered-To", []))]
                alias = next((a for a in to if a.endswith("@icloud.com") and a != self.cred["imap_user"].lower()), None)
                if alias and _write(alias, codes[0], when, f"{subject} | {msg['From']}"):
                    n += 1
        return n

    def close(self):
        try:
            if self.M:
                self.M.logout()
        except Exception:                                    # noqa: BLE001
            pass
        self.M = None


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    boxes = [Inbox(k, v) for k, v in accounts().items()]
    log.info("watching %d iCloud inboxes every %.0f s", len(boxes), POLL)
    last_sync = 0.0
    while True:
        if time.time() - last_sync > SYNC_EVERY:
            last_sync = time.time()
            try:
                sync_status()
            except Exception as exc:                         # noqa: BLE001
                log.warning("status sync failed: %s", exc)
        for b in boxes:
            try:
                b.poll()
            except Exception as exc:                         # noqa: BLE001 -- reconnect next round
                log.warning("%s: %s -- reconnecting", b.cred.get("imap_user"), exc)
                b.close()
        if a.once:
            return 0
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
