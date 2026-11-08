"""Keep the per-model "Scheduled (Account Manager)" Drive folders rolling.

Each model's `MODELS/<...>/Reels Instagram - <Model>/Scheduled (Account Manager)`
holds one `YYYY-MM-DD` folder per posting day, filled by the account managers.
This pass (added 2026-09-29, operator request):

  * creates the date folders for today .. today+AHEAD_DAYS that are missing,
    and fills each of them with the model's time slots (TIME_TEMPLATES) --
    only missing subfolders are added, nothing existing is touched,
  * moves date folders older than the day before yesterday into the model's archive folder
    (an existing `OLD` / `archiv` subfolder, else a new `OLD`).

It also keeps the bot's own raw-video tree rolling: every model folder under
DRIVE_RAW_FOLDER_ID (`01_Raw_Videos/<Model>/`) that already uses date folders
gets today .. today+AHEAD_DAYS created (flat, no archive -- the pipeline reads
today's folder and, except for Nikki, falls back to yesterday's). A missing
"today" folder means the pipeline finds no content for that model all day.

Dry run by default; `--apply` writes. Run daily from a systemd timer.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SCHEDULED_NAME = "Scheduled (Account Manager)"
ARCHIVE_NAMES = ("old", "archiv", "archive")
DEFAULT_ARCHIVE = "OLD"
AHEAD_DAYS = 7
KEEP_DAYS_BACK = 2   # today, yesterday and the day before stay out of the archive
SLOTS = ["15 UHR", "17 UHR", "19 UHR", "21 UHR", "23 UHR"]
# Per model: a flat list of slot folders, or {language: [slots]} one level down.
TIME_TEMPLATES = {
    "nikki": {"DE": SLOTS, "EN": ["15 UHR", "17 UHR", "19 UHR"]},
}
DEFAULT_TEMPLATE = SLOTS
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def scheduled_folders(client) -> list:
    """[(model label, folder id)] for every Scheduled (Account Manager) folder."""
    q = (f"name = '{SCHEDULED_NAME}' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    out = []
    for f in client.service.files().list(
            q=q, fields="files(id,name,parents)", pageSize=100,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", []):
        parent = (f.get("parents") or [None])[0]
        label = f["id"]
        if parent:
            label = client.service.files().get(fileId=parent, fields="name",
                                               supportsAllDrives=True).execute()["name"]
        out.append((label, f["id"]))
    return sorted(out)


def plan(subfolders: list, today: date, ahead: int = AHEAD_DAYS, keep_from: date | None = None):
    """Pure: (dates to create, [(folder, ...)] to archive, archive folder or None)."""
    keep_from = keep_from or (today - timedelta(days=KEEP_DAYS_BACK))
    by_name = {s["name"].strip(): s for s in subfolders}
    archive = next((s for s in subfolders if s["name"].strip().lower() in ARCHIVE_NAMES), None)
    create = [(today + timedelta(days=i)).isoformat() for i in range(ahead + 1)
              if (today + timedelta(days=i)).isoformat() not in by_name]
    to_archive = [s for s in subfolders
                  if DATE_RE.match(s["name"].strip())
                  and date.fromisoformat(s["name"].strip()) < keep_from]
    return create, sorted(to_archive, key=lambda s: s["name"]), archive


def template_for(label: str):
    low = label.lower()
    for key, tpl in TIME_TEMPLATES.items():
        if key in low:
            return tpl
    return DEFAULT_TEMPLATE


def fill_date_folder(client, date_folder_id: str, template, apply: bool) -> list:
    """Add the template's missing subfolders; returns what was (or would be) created."""
    made = []
    existing = {s["name"].strip(): s["id"] for s in client.list_subfolders(date_folder_id)}
    if isinstance(template, dict):
        for lang, slots in template.items():
            lang_id = existing.get(lang)
            if lang_id is None:
                made.append(lang)
                lang_id = client.create_folder(date_folder_id, lang) if apply else None
            have = {s["name"].strip() for s in client.list_subfolders(lang_id)} if lang_id else set()
            for slot in slots:
                if slot not in have:
                    made.append(f"{lang}/{slot}")
                    if apply:
                        client.create_folder(lang_id, slot)
    else:
        if any(k in existing for k in ("DE", "EN")):
            return made   # someone split this day by language by hand -- leave it
        for slot in template:
            if slot not in existing:
                made.append(slot)
                if apply:
                    client.create_folder(date_folder_id, slot)
    return made


def run(apply: bool, keep_from: date | None = None, out=print) -> int:
    from adb_bot.clients.gdrive import DriveClient
    client = DriveClient(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    mode = "APPLY" if apply else "DRY-RUN"
    for label, fid in scheduled_folders(client):
        subs = client.list_subfolders(fid)
        create, to_archive, archive = plan(subs, today, keep_from=keep_from)
        out(f"[{mode}] {label}: create {create or '-'}; archive {[s['name'] for s in to_archive] or '-'}"
            f" -> {archive['name'].strip() if archive else DEFAULT_ARCHIVE + ' (new)'}")
        if not apply:
            subs = {s["name"].strip(): s["id"] for s in subs}
            for i in range(AHEAD_DAYS + 1):
                day = (today + timedelta(days=i)).isoformat()
                if day in subs:
                    made = fill_date_folder(client, subs[day], template_for(label), False)
                    if made:
                        out(f"    {day}: + {made}")
            continue
        for name in create:
            client.create_folder(fid, name)
        subs = {s["name"].strip(): s["id"] for s in client.list_subfolders(fid)}
        for i in range(AHEAD_DAYS + 1):
            day = (today + timedelta(days=i)).isoformat()
            if day in subs:
                made = fill_date_folder(client, subs[day], template_for(label), apply)
                if made:
                    out(f"    {day}: + {made}")
        if to_archive:
            archive_id = archive["id"] if archive else client.create_folder(fid, DEFAULT_ARCHIVE)
            for s in to_archive:
                client.move_file(s["id"], archive_id, fid)
    raw_root = os.environ.get("DRIVE_RAW_FOLDER_ID")
    if raw_root:
        for model in sorted(client.list_subfolders(raw_root), key=lambda m: m["name"]):
            names = {s["name"].strip() for s in client.list_subfolders(model["id"])}
            if not any(DATE_RE.match(n) for n in names):
                continue   # not on the dated layout -- leave it alone
            missing = [(today + timedelta(days=i)).isoformat() for i in range(AHEAD_DAYS + 1)
                       if (today + timedelta(days=i)).isoformat() not in names]
            out(f"[{mode}] raw {model['name']}: create {missing or '-'}")
            if apply:
                for name in missing:
                    client.create_folder(model["id"], name)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="drive_schedule_folders")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--keep-from", help="YYYY-MM-DD; archive date folders before this (default: 2 days back)")
    a = ap.parse_args(argv)
    return run(a.apply, date.fromisoformat(a.keep_from) if a.keep_from else None)


if __name__ == "__main__":
    sys.exit(main())
