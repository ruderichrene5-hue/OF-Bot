"""Upload folders for the model portal (17.10, Rene: every plan entry should link straight to the Drive
folder where the model uploads). Tree in the 100 GB account (rclone remote `veralta`, OAuth as the owner):

    Northfeed Portal/<Model>/                       -> Models."Drive folder"
    Northfeed Portal/<Model>/<YYYY-MM-DD>/          -> Content Plan."Drive folder" (one folder per shoot day, shared by
                                                       every entry of that day; Rene 17.10: one per entry is "too much")

Folders are created once, the link (https://drive.google.com/drive/folders/<id>) is written back to Airtable,
so the portal can show an "Upload" button and the team finds the files by date and title.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading

import requests

from . import portal

REMOTE = os.environ.get("PORTAL_DRIVE_REMOTE", "veralta:Northfeed Portal")
F_PLAN_DRIVE, F_MODEL_DRIVE = "Drive folder", "Drive folder"
_busy = threading.Lock()


def _rclone(*args: str) -> str:
    r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=120)
    return r.stdout


def _folder_id(parent: str, name: str) -> str | None:
    try:
        for d in json.loads(_rclone("lsjson", f"{REMOTE}/{parent}".rstrip("/"), "--dirs-only") or "[]"):
            if d.get("Name") == name:
                return d.get("ID")
    except Exception:  # noqa: BLE001
        return None
    return None


def ensure_folder(parent: str, name: str) -> str | None:
    """Create <parent>/<name> under the portal tree if missing; return the Drive link."""
    fid = _folder_id(parent, name)
    if not fid:
        _rclone("mkdir", f"{REMOTE}/{parent}/{name}".replace("//", "/"))
        fid = _folder_id(parent, name)
    return f"https://drive.google.com/drive/folders/{fid}" if fid else None


def _safe(s: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "-", s).strip()[:80]


def _patch(table: str, rec_id: str, fields: dict) -> None:
    requests.patch(f"https://api.airtable.com/v0/{portal.BASE}/{table}/{rec_id}", timeout=20,
                   headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"},
                   json={"fields": fields})


def sync(model: str, model_rec: dict, plan_rows: list) -> None:
    """Fill missing Drive links for one model: her root folder + one folder per plan row. Runs in a thread."""
    if not _busy.acquire(blocking=False):
        return
    try:
        if model_rec.get("id") and not model_rec.get("fields", {}).get(F_MODEL_DRIVE):
            link = ensure_folder("", model)
            if link:
                _patch(portal.T_MODELS, model_rec["id"], {F_MODEL_DRIVE: link})
        by_day: dict[str, str] = {}
        for p in plan_rows:
            if p.get("drive") or not p.get("date"):
                continue
            day = p["date"]
            if day not in by_day:
                by_day[day] = ensure_folder(model, day) or ""
            if by_day[day]:
                _patch(portal.T_PLAN, p["id"], {F_PLAN_DRIVE: by_day[day]})
                p["drive"] = by_day[day]
    except Exception as e:  # noqa: BLE001
        print("portal_drive:", e)
    finally:
        _busy.release()


def sync_async(model: str, model_rec: dict, plan_rows: list) -> None:
    threading.Thread(target=sync, args=(model, model_rec, list(plan_rows)), daemon=True).start()
