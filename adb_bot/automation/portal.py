"""Northfeed model portal (16.10, Rene): one creator logs in and sees her day, her week, her reels and
her accounts -- in the website's look. Data: the IG Farm base through crm.data()/creator_view() (accounts,
followers, reels) plus the "Northfeed Portal" base (Models, Content Plan, Tasks).

    portal.build("Lea")  -> full HTML page for that model
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from pathlib import Path

import requests

from . import crm

BASE = "appkNZyYhlmvqD84H"                      # Northfeed Portal
T_MODELS, T_PLAN, T_TASKS = "tblBJFZwfPONZrlD1", "tblhMYxVVowzGnScU", "tbljnBF6t5MtyeUoT"
REF_DIR = Path(os.environ.get("PORTAL_REF_DIR", "/opt/northfeed-portal/refs"))   # reference videos, downloaded once
COOKIES = Path("/root/reel_pipeline/cookies.txt")
_fetching: set = set()


def ref_file(name: str) -> Path | None:
    """'recXXXX.mp4' -> cached file or None (no path tricks)."""
    if not re.fullmatch(r"rec[A-Za-z0-9]{14}\.mp4", name):
        return None
    f = REF_DIR / name
    return f if f.exists() and f.stat().st_size > 0 else None


def _download_ref(rec_id: str, url: str) -> None:
    try:
        import yt_dlp
        REF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REF_DIR / f"{rec_id}.part.%(ext)s"
        opts = {"outtmpl": str(tmp), "format": "mp4/bestvideo[ext=mp4]+bestaudio/best", "merge_output_format": "mp4",
                "quiet": True, "no_warnings": True, "max_filesize": 150 * 1024 * 1024}
        if COOKIES.exists():
            opts["cookiefile"] = str(COOKIES)
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
            got = Path(y.prepare_filename(info))
        if got.exists():
            got.rename(REF_DIR / f"{rec_id}.mp4")
    except Exception as e:  # noqa: BLE001
        print(f"portal ref {rec_id}: {e}")
    finally:
        _fetching.discard(rec_id)


def fetch_references(plan: list) -> None:
    """Background: download every reference video that is not cached yet (so it plays inside the portal)."""
    for p in plan:
        if p.get("reference") and not ref_file(f"{p['id']}.mp4") and p["id"] not in _fetching:
            _fetching.add(p["id"])
            threading.Thread(target=_download_ref, args=(p["id"], p["reference"]), daemon=True).start()


def _fetch(table: str, formula: str | None = None) -> list:
    out, offset = [], None
    while True:
        params: dict = {"pageSize": 100}
        if formula:
            params["filterByFormula"] = formula
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}", params=params, timeout=40,
                         headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}"})
        r.raise_for_status()
        j = r.json()
        out.extend(j.get("records", []))
        offset = j.get("offset")
        if not offset:
            return out


def _att(v):
    a = (v or [{}])[0]
    return {"url": a.get("url"), "type": a.get("type", ""), "thumb": ((a.get("thumbnails") or {}).get("large") or {}).get("url")} if a.get("url") else None


def portal_data(model: str, crm_dataset: dict | None = None) -> dict:
    base = crm.creator_view(crm_dataset or crm.data(), model)
    models = {str(m["fields"].get("Name", "")).strip().lower(): m for m in _fetch(T_MODELS)}
    me = models.get(model.strip().lower(), {"id": None, "fields": {}})
    mid = me.get("id")
    plan = [{"id": r["id"], "title": f.get("Title", ""), "date": f.get("Date"), "platform": f.get("Platform", ""),
             "type": f.get("Type", ""), "status": f.get("Status", ""), "hook": f.get("Hook / idea", ""),
             "caption": f.get("Caption", ""), "outfit": f.get("Outfit / location", ""), "reference": f.get("Reference"),
             "published": f.get("Published link"), "file": _att(f.get("Files")), "notes": f.get("Notes", "")}
            for r in _fetch(T_PLAN) for f in [r["fields"]] if mid and mid in (f.get("Model") or [])]
    tasks = [{"id": r["id"], "title": f.get("Task", ""), "category": f.get("Category", ""), "due": f.get("Due"),
              "dueType": f.get("Due type", "Fixed day" if f.get("Due") else "This week"), "status": f.get("Status", "Open"),
              "priority": f.get("Priority", ""), "assigned": f.get("Assigned to", "Model"), "desc": f.get("Description", ""),
              "file": _att(f.get("Files"))}
             for r in _fetch(T_TASKS) for f in [r["fields"]] if mid and mid in (f.get("Model") or []) and f.get("Visible to model")]
    for p in plan:
        p["refVideo"] = f"/portal/ref/{p['id']}.mp4" if ref_file(f"{p['id']}.mp4") else None
    fetch_references(plan)
    # 16.10 Rene: no fleet reels, no view counts, no account health in the portal -- followers and growth only
    base["accounts"] = [{k: v for k, v in a.items() if k in ("id", "handle", "avatar", "followers", "followersDaily", "posts", "link")}
                        for a in base["accounts"]]
    plan.sort(key=lambda x: (x["date"] or "9999", x["title"]))
    tasks.sort(key=lambda x: (x["due"] or "9999", x["title"]))
    f = me.get("fields", {})
    return {**base, "model": {"name": f.get("Name", model), "language": f.get("Language", "EN"), "shootDays": f.get("Shoot days") or [],
                              "onlyfans": f.get("OnlyFans link")}, "plan": plan, "tasks": tasks,
            "today": dt.date.today().isoformat()}


def mark_done(rec_id: str, done: bool, creator: str | None = None) -> bool:
    """Tick from the portal. Tasks -> Done/Open; plan rows -> Shot/Planned. A creator may only touch her own rows."""
    if not rec_id.startswith("rec"):
        return False
    hdr = {"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}"}
    for table, field, on, off in ((T_TASKS, "Status", "Done", "Open"), (T_PLAN, "Status", "Shot", "Planned")):
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}/{rec_id}", headers=hdr, timeout=20)
        if r.status_code != 200:
            continue
        if creator:
            names = {m["id"]: m["fields"].get("Name", "") for m in _fetch(T_MODELS)}
            if creator.strip().lower() not in {names.get(x, "").strip().lower() for x in r.json()["fields"].get("Model") or []}:
                return False
        r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{table}/{rec_id}", headers={**hdr, "Content-Type": "application/json"},
                           json={"fields": {field: on if done else off}}, timeout=20)
        return r.status_code == 200
    return False


LOGO = Path(__file__).with_name("portal_logo.png")


def render(d: dict) -> str:
    import base64
    tpl = Path(__file__).with_name("portal.html").read_text(encoding="utf-8")
    logo = "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode() if LOGO.exists() else ""
    return tpl.replace("__LOGO__", logo).replace("__DATA__", json.dumps(d, ensure_ascii=False).replace("</", "<\\/"))


def build(model: str, crm_dataset: dict | None = None) -> str:
    return render(portal_data(model, crm_dataset))


if __name__ == "__main__":
    import sys
    sys.stdout.write(build(sys.argv[1] if len(sys.argv) > 1 else "Lea"))
