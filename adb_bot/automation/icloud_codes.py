"""Read signup codes that Instagram sends to iCloud+ "Hide My Email" aliases.

    .venv/bin/python -m adb_bot.automation.icloud_codes wait schlauch.depot_5b@icloud.com --timeout 300
    .venv/bin/python -m adb_bot.automation.icloud_codes recent schlauch.depot_5b@icloud.com

How it fits together (set up 2026-10-07):
- An alias is not a mailbox, it forwards to the Apple ID's iCloud inbox (Hide My Email ->
  "Forward to" must be the @icloud.com address, not the Apple ID's Gmail).
- The server reads that inbox over IMAP (imap.mail.me.com) with an app-specific password.
  Credentials live in /etc/adbbot/icloud_mail.json (root 600) -- never in Airtable, VAs read it:
      {"<apple id>": {"imap_user": "<x>@icloud.com", "app_password": "xxxx-xxxx-xxxx-xxxx"}}
- Forwarded mail keeps the alias in To ("Hide My Email <alias>"), so codes never get mixed up
  between accounts signing up at the same time.
- iCloud files these forwards under **Junk** quite often (both first test mails did), so every
  search covers INBOX and Junk.
- Mail sent from the same iCloud account to its own alias bounces (HM108 "local policy") --
  test with a mail from outside.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import re
import sys
import time
from email.utils import parsedate_to_datetime

CONFIG = "/etc/adbbot/icloud_mail.json"
HOST = "imap.mail.me.com"
FOLDERS = ("INBOX", "Junk")
# "123456 is your Instagram code", "Your code is 123 456", "Bestätigungscode: 123456"
CODE_RE = re.compile(r"(?<!\d)(\d{3}\s?\d{3})(?!\d)")


def accounts() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _text(msg) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    text = " ".join(parts)
    return re.sub(r"<[^>]+>", " ", text)


def find(alias: str, since: float = 0, sender: str | None = None) -> list[dict]:
    """Every mail to `alias` (newest first) in all configured iCloud inboxes, INBOX + Junk."""
    alias = alias.lower().strip()
    found = []
    for apple_id, cred in accounts().items():
        M = imaplib.IMAP4_SSL(HOST, 993)
        try:
            M.login(cred["imap_user"], cred["app_password"])
            for folder in FOLDERS:
                if M.select(folder, readonly=True)[0] != "OK":
                    continue
                typ, ids = M.search(None, f'(TO "{alias}")')
                for mid in reversed((ids[0] or b"").split()):
                    msg = email.message_from_bytes(M.fetch(mid, "(BODY.PEEK[])")[1][0][1])
                    try:
                        ts = parsedate_to_datetime(msg["Date"]).timestamp()
                    except Exception:
                        ts = 0
                    if ts and ts < since:
                        continue
                    frm = str(msg["From"] or "")
                    if sender and sender.lower() not in frm.lower() + " " + str(msg["Subject"] or "").lower():
                        continue
                    subject = str(msg["Subject"] or "")
                    codes = [c.replace(" ", "") for c in CODE_RE.findall(subject + " " + _text(msg))]
                    found.append({"apple_id": apple_id, "folder": folder, "time": ts, "from": frm,
                                  "subject": subject, "code": codes[0] if codes else None})
        finally:
            try:
                M.logout()
            except Exception:
                pass
    return sorted(found, key=lambda m: m["time"], reverse=True)


def wait_for_code(alias: str, since: float | None = None, timeout: float = 300, sender: str | None = "instagram",
                  poll: float = 8) -> str | None:
    """Poll until a mail to `alias` newer than `since` carries a code; return it (or None)."""
    since = since if since is not None else time.time() - 60
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for m in find(alias, since, sender):
            if m["code"]:
                return m["code"]
        time.sleep(poll)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("recent", help="list mails to an alias")
    s.add_argument("alias")
    s = sub.add_parser("wait", help="wait for a code to an alias")
    s.add_argument("alias")
    s.add_argument("--timeout", type=float, default=300)
    s.add_argument("--any-sender", action="store_true", help="not only Instagram (for tests)")
    a = ap.parse_args(argv)
    if a.cmd == "recent":
        for m in find(a.alias):
            print(time.strftime("%Y-%m-%d %H:%M", time.localtime(m["time"])), m["folder"], m["code"], "|", m["subject"][:60], "|", m["from"][:50])
        return 0
    code = wait_for_code(a.alias, timeout=a.timeout, sender=None if a.any_sender else "instagram")
    print(code or "no code")
    return 0 if code else 1


if __name__ == "__main__":
    sys.exit(main())
