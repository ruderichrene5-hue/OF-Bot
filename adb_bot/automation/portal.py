"""Northfeed model portal (16.10, Rene): one creator logs in and sees her day, her week, her reels and
her accounts -- in the website's look. Data: the IG Farm base through crm.data()/creator_view() (accounts,
followers, reels) plus the "Northfeed Portal" base (Models, Content Plan, Tasks).

    portal.build("Lea")  -> full HTML page for that model
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import requests

from . import crm

BASE = "appkNZyYhlmvqD84H"                      # Northfeed Portal
T_MODELS, T_PLAN, T_TASKS = "tblBJFZwfPONZrlD1", "tblhMYxVVowzGnScU", "tbljnBF6t5MtyeUoT"


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


def render(d: dict) -> str:
    tpl = Path(__file__).with_name("portal.html").read_text(encoding="utf-8")
    return tpl.replace("__DATA__", json.dumps(d, ensure_ascii=False).replace("</", "<\\/"))


def build(model: str, crm_dataset: dict | None = None) -> str:
    return render(portal_data(model, crm_dataset))


if __name__ == "__main__":
    import sys
    sys.stdout.write(build(sys.argv[1] if len(sys.argv) > 1 else "Lea"))
