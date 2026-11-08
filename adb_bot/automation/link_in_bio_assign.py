"""Give every iPhone-farm Instagram account its own oopsie.bio link (Airtable "Link in Bio").

Rene 14.10: the links of a model are entered once in "Link in Bio" (URL, Model, Status "Can Be Used");
every account of that model that exists on Instagram (Creation Status "Instagram Created" / "Verified",
a username) and has no "Link in Bio 2" yet gets the next free link of ITS model -- free = linked to no
account. The Account Übersicht then shows it as "Tracking Link (auto)". One link never goes to two accounts.

    .venv/bin/python -m adb_bot.automation.link_in_bio_assign            # show what would be linked
    .venv/bin/python -m adb_bot.automation.link_in_bio_assign --apply    # link it
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import requests

log = logging.getLogger("link_in_bio_assign")
BASE = "appwk9LrgAhJv8iEH"
T_ACC, T_LINKS = "tbly0y1I4hNDlvh1K", "tbl0PVH240iK7vms0"
CREATED = {"Instagram Created", "Verified"}


def _hdr() -> dict:
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}


def _all(table: str) -> list[dict]:
    out, off = [], None
    while True:
        params = {"pageSize": 100, **({"offset": off} if off else {})}
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}", headers=_hdr(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        off = j.get("offset")
        if not off:
            return out


def plan() -> list[tuple[str, str, str, str]]:
    """[(account id, username, link id, url)] -- what would be linked now."""
    links = _all(T_LINKS)
    free: dict[str, list[dict]] = {}
    for l in links:
        f = l["fields"]
        if f.get("Accounts (IG)") or f.get("Status") == "Can't Be Used" or not f.get("URL"):
            continue
        for m in f.get("Model") or []:
            free.setdefault(m, []).append(l)
    for m in free:
        # the base link ("…/redlea") last: copies first, the original stays free longest
        free[m].sort(key=lambda l: ("-copy" not in l["fields"]["URL"], l["fields"]["URL"]))
    todo = []
    for a in _all(T_ACC):
        f = a["fields"]
        if f.get("Link in Bio 2") or not f.get("Username") or f.get("Creation Status") not in CREATED:
            continue
        if str(f.get("Status") or "").lower() in ("banned", "completely banned"):
            continue
        model = (f.get("Model auswählen (Pflicht)") or [None])[0]
        if model and free.get(model):
            l = free[model].pop(0)
            todo.append((a["id"], f["Username"], l["id"], l["fields"]["URL"]))
    return todo


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    todo = plan()
    log.info("link_in_bio_assign: %d account(s) get a link", len(todo))
    for acc, user, link, url in todo:
        log.info("  @%s -> %s", user, url)
        if a.apply:
            r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{T_ACC}/{acc}", headers=_hdr(),
                               json={"fields": {"Link in Bio 2": [link]}}, timeout=30)
            r.raise_for_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
