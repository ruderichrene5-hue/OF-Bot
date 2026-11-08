"""One-way sync: MLX fleet -> the standalone 'Reels farm' Airtable base.

This is deliberately separate from every other loop in this package. It never
reads back into anything the posting/warmup/verification code touches, and it
never writes to the local_tables JSON store -- it only reads from MLX and our
own local files, and writes to a *different*, purely informational Airtable
base that Rene asked for as an overview/tracking tool (2026-09-24). Nothing
here can affect posting, warmup, or verification if it breaks.

Matched on 'MLX Profile ID' (the MLX numeric id), a hidden field added to the
Accounts and Device tables for exactly this: without it, every run would
create a fresh duplicate row per profile instead of updating the existing one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests

BASE_ID = "appwk9LrgAhJv8iEH"
ACCOUNTS_TABLE = "tbly0y1I4hNDlvh1K"
DEVICE_TABLE = "tbl6OZrzFuns7rux6"
API_ROOT = "https://api.airtable.com/v0"

_MODEL_CHOICES = {"kathi": "Kathi", "lea": "Lea", "luisa": "Luisa",
                  "nikkien": "Nikki EN", "nikki": "Nikki DE"}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _mlx_items(token: str) -> list:
    items = []
    page = 1
    while True:
        resp = requests.get("https://api.multilogin.com/mobile_profiles/phone/list",
                            headers=_headers(token), params={"page": page, "page_size": 100},
                            timeout=30).json()
        batch = resp.get("data", {}).get("items", [])
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def _mlx_folders(token: str) -> dict:
    resp = requests.get("https://api.multilogin.com/workspace/folders", headers=_headers(token),
                        params={"folder_type": "mobile"}, timeout=30).json()
    return {f["folder_id"]: f["name"] for f in resp.get("data", {}).get("folders", [])}


def _model_for_folder(folder_name: str) -> str | None:
    key = "".join((folder_name or "").lower().split())
    return _MODEL_CHOICES.get(key)


def _mailboxes() -> dict:
    path = Path.home() / ".adb_bot" / "mailboxes.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _status_for(tags: list) -> str | None:
    if "Human Verification" in tags:
        return "Human Verification"
    if "Issue" in tags or "unable to verify" in tags:
        return "Flagged"
    if "Banned / Dead" in tags:
        return "Banned"
    if "Active / Posting" in tags:
        return "Healthy"
    if "Warmup" in tags:
        return "Warmup"
    return None


def _existing_by_mlx_id(airtable_token: str, table: str) -> dict:
    out = {}
    offset = None
    while True:
        params = {"fields[]": "MLX Profile ID"}
        if offset:
            params["offset"] = offset
        resp = requests.get(f"{API_ROOT}/{BASE_ID}/{table}", headers=_headers(airtable_token),
                            params=params, timeout=30).json()
        for rec in resp.get("records", []):
            mlx_id = rec.get("fields", {}).get("MLX Profile ID")
            if mlx_id:
                out[mlx_id] = rec["id"]
        offset = resp.get("offset")
        if not offset:
            break
    return out


def _batched(seq, n=10):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _upsert(airtable_token: str, table: str, existing: dict, rows: list, logger=None) -> dict:
    """rows: [(mlx_id, fields_dict), ...]. Splits into create/update by whether
    `existing` already has that MLX id, Airtable's own 10-records-per-call limit."""
    to_create = [{"fields": {**fields, "MLX Profile ID": mlx_id}}
                 for mlx_id, fields in rows if mlx_id not in existing]
    to_update = [{"id": existing[mlx_id], "fields": fields}
                for mlx_id, fields in rows if mlx_id in existing]
    created = updated = errors = 0
    for chunk in _batched(to_create):
        r = requests.post(f"{API_ROOT}/{BASE_ID}/{table}", headers=_headers(airtable_token),
                          json={"records": chunk}, timeout=30)
        if r.ok:
            created += len(chunk)
        else:
            errors += len(chunk)
            if logger:
                logger.warning("reelsfarm_sync: create failed in %s: %s", table, r.text[:200])
    for chunk in _batched(to_update):
        r = requests.patch(f"{API_ROOT}/{BASE_ID}/{table}", headers=_headers(airtable_token),
                           json={"records": chunk}, timeout=30)
        if r.ok:
            updated += len(chunk)
        else:
            errors += len(chunk)
            if logger:
                logger.warning("reelsfarm_sync: update failed in %s: %s", table, r.text[:200])
    return {"created": created, "updated": updated, "errors": errors}


def run(mlx_token: str, airtable_token: str, logger=None, dry_run: bool = True) -> dict:
    items = _mlx_items(mlx_token)
    folders = _mlx_folders(mlx_token)
    mailboxes = _mailboxes()
    mail_by_addr = {}
    for cred in mailboxes.values():
        addr = (cred.get("address") or "").lower()
        if addr:
            mail_by_addr[addr] = cred

    account_rows = []
    device_rows = []
    for item in items:
        mlx_id = str(item.get("id") or "")
        if not mlx_id:
            continue
        name = item.get("serial_name") or mlx_id
        tags = [(t["name"] if isinstance(t, dict) else t) for t in (item.get("tags") or [])]
        folder_name = folders.get(item.get("folder_id"), "")
        model = _model_for_folder(folder_name)
        remark = item.get("remark") or ""

        email = ""
        email_pw = ""
        if remark.startswith("MAIL:"):
            try:
                mail_part, pw_part = remark.split(" | PW:", 1)
                email = mail_part[len("MAIL:"):].strip()
                email_pw = pw_part.strip()
            except ValueError:
                pass

        fields: dict = {"Phone / Profile": name}
        if model:
            fields["Model"] = model
        status = _status_for(tags)
        if status:
            fields["Status"] = status
        if email:
            fields["Email"] = email
            fields["Account Created"] = True
        if email_pw:
            fields["Email Password"] = email_pw
        if tags:
            fields["Notes"] = "MLX tags: " + ", ".join(tags)
        account_rows.append((mlx_id, fields))

        device_rows.append((mlx_id, {"iPhone UID": name}))

    if dry_run:
        return {"would_sync_accounts": len(account_rows), "would_sync_devices": len(device_rows)}

    existing_accounts = _existing_by_mlx_id(airtable_token, ACCOUNTS_TABLE)
    existing_devices = _existing_by_mlx_id(airtable_token, DEVICE_TABLE)
    acc_report = _upsert(airtable_token, ACCOUNTS_TABLE, existing_accounts, account_rows, logger)
    dev_report = _upsert(airtable_token, DEVICE_TABLE, existing_devices, device_rows, logger)
    return {"accounts": acc_report, "devices": dev_report}


def main(argv=None) -> int:
    import argparse
    import logging

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually write to Airtable (default: dry-run count only).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger = logging.getLogger("reelsfarm_sync")

    mlx_token = os.environ.get("MULTILOGIN_TOKEN", "")
    airtable_token = os.environ.get("AIRTABLE_TOKEN", "")
    if not mlx_token or not airtable_token:
        logger.error("MULTILOGIN_TOKEN and/or AIRTABLE_TOKEN missing from the environment")
        return 1

    result = run(mlx_token, airtable_token, logger=logger, dry_run=not args.apply)
    logger.info("reelsfarm sync result: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
