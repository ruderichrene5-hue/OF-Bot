"""RNX Viewer "CRM" tab: the team's Instagram accounts at a glance -- followers, views, top accounts,
top reels, per model and per phone. Read-only view over Airtable (Rene 2026-10-08: "es geht nur um
die schönere Ansicht ... alles aus Airtable"; layout modelled on the DNZ CRM screenshots he sent).

Sources (all in the Reels base):
  Accounts SOP  -- handle, model, Status (stage), container -> phone, profile picture
  Performance   -- one row per account and day from reel_pipeline/performance_collector (RapidAPI)
  Reel Stats    -- one row per reel and day: views/likes/comments, "Aufrufe +/- (24h)", thumbnail
  Models, Device, Containers
Views over a period = sum of the per-reel daily deltas; follower growth = snapshot difference.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import requests

BASE = "appwk9LrgAhJv8iEH"
T_ACC, T_PERF, T_REELS = "tbly0y1I4hNDlvh1K", "tblySt695GOrzGqZk", "tblLkw1GLUZ88Ml5N"
T_MODELS, T_DEVICE, T_CONT = "tblMkfEq3cEnXuZs5", "tbl6OZrzFuns7rux6", "tblyZXN7caKZZBaBj"


def _fetch(table: str, fields: list | None = None, formula: str | None = None) -> list:
    out, offset = [], None
    while True:
        params: dict = {"pageSize": 100}
        if fields:
            params["fields[]"] = fields
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


def _name(v):
    if isinstance(v, list):
        return ", ".join(str(_name(x)) for x in v if x)
    return v.get("name") if isinstance(v, dict) else (v or "")


def _status(stage: str, scraped: bool, any_scrape: bool) -> str:
    s = (stage or "").lower()
    if "banned" in s:
        return "banned"
    if "review" in s or "verification" in s:
        return "check"
    if any_scrape and not scraped:
        return "inactive"                       # the scraper ran but Instagram returned nothing for the handle
    return "active"


def data() -> dict:
    since = (dt.date.today() - dt.timedelta(days=95)).isoformat()
    accs = _fetch(T_ACC, ["Username", "Model (auto)", "Status", "Container", "Profile Picture", "Warm-up Day", "Tracking Link (auto)"])
    perf = _fetch(T_PERF, ["Account", "Datum", "Follower", "Beiträge"], f"IS_AFTER({{Datum}}, '{since}')")
    reels = _fetch(T_REELS, ["Account", "Datum", "Reel Code", "Aufrufe", "Likes", "Kommentare", "Aufrufe +/- (24h)", "Thumbnail"],
                   f"IS_AFTER({{Datum}}, '{since}')")
    models = _fetch(T_MODELS, ["Model"])
    cont = {c["id"]: c["fields"] for c in _fetch(T_CONT, ["Container", "iPhone", "Proxy"])}
    devices = _fetch(T_DEVICE, ["iPhone UID", "Max Containers"])

    fol = defaultdict(dict)
    posts = {}
    for r in perf:
        f = r["fields"]
        for a in f.get("Account") or []:
            if f.get("Datum") and f.get("Follower") is not None:
                fol[a][f["Datum"]] = f["Follower"]
            if f.get("Datum") and f.get("Beiträge") is not None:
                posts[a] = max(posts.get(a, ("", 0)), (f["Datum"], f["Beiträge"]))
    rr = defaultdict(lambda: defaultdict(list))
    for r in reels:
        f = r["fields"]
        for a in f.get("Account") or []:
            if f.get("Datum"):
                rr[a][f["Datum"]].append(f)
    any_scrape = bool(fol)
    today = dt.date.today().isoformat()

    accounts = []
    for r in accs:
        f = r["fields"]
        handle = str(f.get("Username") or "").strip().lstrip("@")
        if not handle:
            continue
        cids = f.get("Container") or []
        c = cont.get(cids[0], {}) if cids else {}
        days_f, days_r = fol.get(r["id"], {}), rr.get(r["id"], {})
        views, new_reels, seen = defaultdict(int), defaultdict(int), set()
        for i, d in enumerate(sorted(days_r)):
            for x in days_r[d]:
                if x.get("Aufrufe +/- (24h)") is not None:
                    views[d] += x["Aufrufe +/- (24h)"]
                code = x.get("Reel Code")
                if code and code not in seen:
                    seen.add(code)
                    if i:
                        new_reels[d] += 1
        latest = max(days_r) if days_r else None
        cur = []
        for x in days_r.get(latest, []):
            th = (x.get("Thumbnail") or [{}])[0]
            cur.append({"code": x.get("Reel Code"), "views": x.get("Aufrufe") or 0, "likes": x.get("Likes") or 0,
                        "comments": x.get("Kommentare") or 0, "delta": x.get("Aufrufe +/- (24h)"),
                        "thumb": ((th.get("thumbnails") or {}).get("large") or {}).get("url") or th.get("url")})
        cur.sort(key=lambda x: -x["views"])
        pic = (f.get("Profile Picture") or [{}])[0]
        stage = _name(f.get("Status"))
        accounts.append({
            "id": r["id"], "handle": handle, "model": _name(f.get("Model (auto)")) or "No model", "stage": stage,
            "status": _status(stage, bool(days_f), any_scrape),
            "phone": _name(c.get("iPhone")), "container": c.get("Container", ""),
            "avatar": ((pic.get("thumbnails") or {}).get("small") or {}).get("url") or pic.get("url"),
            "followers": days_f[max(days_f)] if days_f else None, "followersDaily": dict(days_f),
            "viewsDaily": dict(views), "newReelsDaily": dict(new_reels), "reels": cur[:30],
            "totalViews": sum(x["views"] for x in cur), "posts": posts.get(r["id"], ("", None))[1],
            # oopsie.bio tracking link picked in "Link in Bio 2" (table Link in Bio, one link per account; 2026-10-11)
            "link": (f.get("Tracking Link (auto)") or [""])[0],
        })
    phones = []
    for d in devices:
        name = d["fields"].get("iPhone UID", "")
        mine = [a for a in accounts if a["phone"] == name]
        conts = [x for x in cont.values() if _name(x.get("iPhone")) == name]
        phones.append({"name": name, "containers": len(conts), "withProxy": sum(1 for x in conts if (x.get("Proxy") or "").strip()),
                       "accounts": [a["id"] for a in mine]})
    phones.sort(key=lambda p: p["name"])
    return {"accounts": accounts, "models": sorted({m["fields"].get("Model", "") for m in models if m["fields"].get("Model")}),
            "phones": phones, "today": today, "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}


def render(d: dict) -> str:
    tpl = Path(__file__).with_name("crm.html").read_text(encoding="utf-8")
    return tpl.replace("__DATA__", json.dumps(d, ensure_ascii=False).replace("</", "<\\/"))


def build() -> str:
    return render(data())


def creator_view(d: dict, model: str) -> dict:
    """14.10 (Rene): a model logs in and sees only her own accounts -- handle, health, followers,
    growth, reels, tracking link. No phones, no containers, no other models."""
    mine = [{k: v for k, v in a.items() if k not in ("phone", "container")} for a in d["accounts"]
            if (a.get("model") or "").strip().lower() == model.strip().lower()]
    return {"accounts": mine, "models": [model], "phones": [], "today": d["today"], "built": d["built"], "creator": model}
