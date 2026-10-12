"""Give every physical-farm Instagram account its own Drive folder, automatically.

Rene (2026-10-01): "jeder User bekommt automatisch einen eigenen Folder" --
matches the diagram he drew (Drive > IG Reels > per-device > per-username >
per-date). This covers the first two missing layers: as soon as an account
has a Username (set manually, or via the Account Creation -> Accounts
automation) and we know which physical iPhone it lives on (the `iPhone
(neu)` lookup, itself derived from the Container link -- see
iphone_farm_proxies.py's module docstring for that chain), this script
creates `IG Reels/<iPhone>/<username>/` if it doesn't exist yet and writes
the folder link back into Accounts.`GDrive Link`.

Idempotent and safe to run on a timer: a row that already has a GDrive Link
is left alone, and `get_or_create_folder` never makes a duplicate for the
same username.

The per-date layer (last node in the diagram) is deliberately not built here
-- that's the same daily-rolling create-ahead/archive-behind job as
drive_schedule_folders.py already does for the other models. Once there are
real posting schedules for these accounts, point that same recipe at each
username folder instead of writing a second copy of it.
"""

from __future__ import annotations

import os

import requests

REELS_BASE = "appwk9LrgAhJv8iEH"
ACCOUNTS_TABLE = "tbly0y1I4hNDlvh1K"

# by NAME, not field id: "Username" was recreated once and got a new id -- the old id silently matched
# nothing and no account got a folder for days (found 2026-10-10)
FIELD_USERNAME = "Username"
FIELD_GDRIVE_LINK = "GDrive Link"
FIELD_IPHONE_LOOKUP = "iPhone"
FIELD_CREATION = "Creation Status"

# IG Reels (root) -> per-device folder id. Device names must match exactly
# what the `iPhone (neu)` lookup returns (i.e. the Containers table's iPhone
# choices, which mirror the Device table's primary field).
DEVICE_FOLDERS = {
    # renamed 2026-10-07: phones are iPhone A, B, C, D… (old names kept so stale lookups still match)
    "iPhone A": "1TGJ6_pPPRWobC1j-CeftgXVzdB-0Busq",
    "iPhone B": "1mB7wgXM-5q2eP62a93nIoojLnaJOPtxL",
    "iPhone C": "1TUZiiLDnlG_e0L1Ps0LOiBZMHaCAf6_1",      # created 2026-10-10
    "iPhone D": "15x51-nZ75TeKaPbzPZnmJtZ13cKWFZZj",
    "iPhone E": "10rhmDVyKk0a-fs9XQJjVgT30vMOBWMZp",
    "iPhone 1 (weiß)": "1TGJ6_pPPRWobC1j-CeftgXVzdB-0Busq",
    "iPhone 2 (schwarz)": "1mB7wgXM-5q2eP62a93nIoojLnaJOPtxL",
}


def _airtable_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}"}


def accounts_needing_a_folder() -> list:
    """[{id, username, iphone}] for every Accounts row with a Username, a
    known iPhone, and no GDrive Link yet."""
    out, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"https://api.airtable.com/v0/{REELS_BASE}/{ACCOUNTS_TABLE}",
            headers=_airtable_headers(), params=params, timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            username = str(f.get(FIELD_USERNAME) or "").strip().lstrip("@")
            if not username or f.get(FIELD_GDRIVE_LINK):
                continue
            if (f.get(FIELD_CREATION) or "") == "To Do":
                continue  # planned, not created on Instagram yet
            iphone_vals = f.get(FIELD_IPHONE_LOOKUP) or []
            iphone = None
            if iphone_vals:
                first = iphone_vals[0]
                iphone = first.get("name") if isinstance(first, dict) else str(first)
            if iphone not in DEVICE_FOLDERS:
                continue  # no container assigned yet -- nothing to do until there is
            out.append({"id": rec["id"], "username": username, "iphone": iphone})
        offset = j.get("offset")
        if not offset:
            return out


def set_gdrive_link(record_id: str, link: str) -> None:
    r = requests.patch(
        f"https://api.airtable.com/v0/{REELS_BASE}/{ACCOUNTS_TABLE}/{record_id}",
        headers={**_airtable_headers(), "Content-Type": "application/json"},
        json={"fields": {FIELD_GDRIVE_LINK: link}}, timeout=30,
    )
    r.raise_for_status()


def run(apply: bool = True, out=print) -> int:
    from adb_bot.clients.gdrive import DriveClient

    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    client = DriveClient(service_account_json=service_account_json)

    due = accounts_needing_a_folder()
    out(f"iphone_farm_account_folders: {len(due)} account(s) need a Drive folder")
    for item in due:
        device_folder = DEVICE_FOLDERS[item["iphone"]]
        if apply:
            folder_id = client.get_or_create_folder(device_folder, item["username"])
            link = f"https://drive.google.com/drive/folders/{folder_id}"
            set_gdrive_link(item["id"], link)
            out(f"  {item['username']} ({item['iphone']}) -> {link}")
        else:
            out(f"  [dry-run] would create {item['iphone']}/{item['username']}")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="iphone_farm_account_folders")
    ap.add_argument("--apply", action="store_true", help="actually create folders / write links")
    a = ap.parse_args(argv)
    return run(apply=a.apply)


if __name__ == "__main__":
    raise SystemExit(main())
