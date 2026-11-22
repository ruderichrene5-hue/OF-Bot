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
            transcribe_ref(rec_id)
    except Exception as e:  # noqa: BLE001
        print(f"portal ref {rec_id}: {e}")
    finally:
        _fetching.discard(rec_id)


def transcribe_ref(rec_id: str) -> None:
    """20.10 (Rene): the reference reel's spoken text, translated to German, into Content Plan "Transcript" / "Transcript DE".
    Runs reel_pipeline/portal_transcribe.py in its own venv (faster-whisper + argostranslate, CPU, offline)."""
    import subprocess
    f = REF_DIR / f"{rec_id}.mp4"
    if not f.exists():
        return
    try:
        out = subprocess.run(["/root/reel_pipeline/.venv/bin/python3", "/root/reel_pipeline/portal_transcribe.py", str(f)],
                             capture_output=True, text=True, timeout=600, cwd="/root/reel_pipeline").stdout.strip().splitlines()
        r = json.loads(out[-1]) if out else {}
        if r.get("text"):
            requests.patch(f"https://api.airtable.com/v0/{BASE}/{T_PLAN}/{rec_id}", timeout=20,
                           headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"},
                           json={"fields": {"Transcript": f"[{r.get('lang', '')}] {r['text']}", "Transcript DE": r.get("de", "")}})
    except Exception as e:  # noqa: BLE001
        print(f"portal transcribe {rec_id}: {e}")


def fetch_references(plan: list) -> None:
    """Background: download every reference video that is not cached yet (so it plays inside the portal)."""
    for p in plan:
        if p.get("reference") and not ref_file(f"{p['id']}.mp4") and p["id"] not in _fetching:
            _fetching.add(p["id"])
            threading.Thread(target=_download_ref, args=(p["id"], p["reference"]), daemon=True).start()


_CACHE: dict = {}


def fetch_cached(table: str, ttl: float = 30.0) -> list:
    """15.11 Rene "alles langsam": who-is-logged-in lookups ran one Airtable read (~0.5 s) per click.
    Short-lived copy for those lookups only; callers that compare passwords read fresh."""
    import time as _t
    hit = _CACHE.get(table)
    age = _t.time() - hit[0] if hit else None
    if hit and age < ttl:
        return hit[1]
    if hit and age < 600:                                    # 16.11 "warum lädt das": stale copy now, fresh one behind it
        _refresh_later(table)
        return hit[1]
    started = _t.time()
    rows = _fetch(table)
    _store(table, started, rows)
    return rows


_BUSTED: dict = {}


def _store(table: str, started: float, rows: list) -> None:
    """16.11 launch check: a read that began before the last write must not land in the cache. A background refresh
    started before her last video was saved came back after the bust and served "0 von 4 Videos" for 20 s, so
    "Weiter zu deinem Portal" sent her back to the videos."""
    import time as _t
    if _BUSTED.get(table, 0) >= started or _BUSTED.get(None, 0) >= started:
        return
    _CACHE[table] = (_t.time(), rows)


_REFRESHING: set = set()


def _refresh_later(table: str) -> None:
    import threading, time as _t
    if table in _REFRESHING:
        return
    _REFRESHING.add(table)
    def run():
        try:
            started = _t.time()
            _store(table, started, _fetch(table))
        except Exception as e:  # noqa: BLE001
            print("[portal] cache refresh failed", table, e)
        finally:
            _REFRESHING.discard(table)
    threading.Thread(target=run, daemon=True).start()


def warm(tables: list) -> None:
    """Fill the cache at start-up and keep it warm (every 60 s), so no click waits for Airtable."""
    import threading, time as _t
    def loop():
        while True:
            for t in tables:
                try:
                    started = _t.time()
                    _store(t, started, _fetch(t))
                except Exception as e:  # noqa: BLE001
                    print("[portal] warm failed", t, e)
            _t.sleep(60)
    threading.Thread(target=loop, daemon=True).start()


def bust(table: str | None = None) -> None:
    import time as _t
    _BUSTED[table] = _t.time()
    if table:
        _CACHE.pop(table, None)
    else:
        _CACHE.clear()


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
    return {"url": a.get("url"), "type": a.get("type", ""), "name": a.get("filename", ""), "thumb": ((a.get("thumbnails") or {}).get("large") or {}).get("url")} if a.get("url") else None


def portal_data(model: str, crm_dataset: dict | None = None) -> dict:
    base = crm.creator_view(crm_dataset or crm.data(), model)
    from concurrent.futures import ThreadPoolExecutor        # 15.11: three tables in parallel, page must feel instant
    with ThreadPoolExecutor(3) as ex:
        f_m, f_p, f_t = ex.submit(fetch_cached, T_MODELS, 20), ex.submit(fetch_cached, T_PLAN, 20), ex.submit(fetch_cached, T_TASKS, 20)   # 16.11 warm cache
        rows_m, rows_p, rows_t = f_m.result(), f_p.result(), f_t.result()
    models = {str(m["fields"].get("Name", "")).strip().lower(): m for m in rows_m}
    me = models.get(model.strip().lower(), {"id": None, "fields": {}})
    mid = me.get("id")
    plan = [{"id": r["id"], "title": f.get("Title", ""), "date": f.get("Date"), "platform": f.get("Platform", ""),
             "type": f.get("Type", ""), "status": f.get("Status", ""), "hook": f.get("Hook / idea", ""),
             "caption": f.get("Caption", ""), "outfit": f.get("Outfit / location", ""), "reference": f.get("Reference"),
             "published": f.get("Published link"), "file": _att(f.get("Files")), "notes": f.get("Notes", ""), "drive": f.get("Drive folder"),
             "transcript": f.get("Transcript", ""), "transcriptDe": f.get("Transcript DE", "")}
            for r in rows_p for f in [r["fields"]] if mid and mid in (f.get("Model") or [])]
    tasks = [{"id": r["id"], "title": f.get("Task", ""), "category": f.get("Category", ""), "due": f.get("Due"),
              "dueType": f.get("Due type", "Fixed day" if f.get("Due") else "This week"), "status": f.get("Status", "Open"),
              "priority": f.get("Priority", ""), "assigned": f.get("Assigned to", "Model"), "desc": f.get("Description", ""),
              "file": _att(f.get("Files")), "files": [x for x in (_att([a]) for a in f.get("Files") or []) if x],
              "drive": f.get("Drive folder"), "review": f.get("Task", "") in _review_titles()}
             for r in rows_t for f in [r["fields"]] if mid and mid in (f.get("Model") or []) and f.get("Visible to model")]
    for p in plan:
        p["refVideo"] = f"/portal/ref/{p['id']}.mp4" if ref_file(f"{p['id']}.mp4") else None
    fetch_references(plan)
    for p in plan:                                  # reference cached but not transcribed yet (e.g. downloaded before 20.10)
        if p["refVideo"] and p.get("reference") and not p.get("transcript") and p["id"] not in _fetching:
            _fetching.add(p["id"])
            threading.Thread(target=lambda rid=p["id"]: (transcribe_ref(rid), _fetching.discard(rid)), daemon=True).start()
    from . import portal_drive
    portal_drive.sync_async(f.get("Name", model) if (f := me.get("fields", {})) else model, me, plan)
    # 16.10 Rene: no fleet reels, no view counts, no account health in the portal -- followers and growth only
    base["accounts"] = [{k: v for k, v in a.items() if k in ("id", "handle", "avatar", "followers", "followersDaily", "posts", "link")}
                        for a in base["accounts"]]
    # 15.11 Rene: results, not machinery -- per-model switch "Accounts im Portal": Nein / Nur Summen (default) / Alle
    acc_mode = str(me.get("fields", {}).get("Accounts im Portal") or "Nein")
    if acc_mode == "Nein":
        base["accounts"] = []
    elif acc_mode != "Alle":
        base["accounts"] = [{**a, "handle": f"Account {i + 1}", "avatar": None, "link": None} for i, a in enumerate(base["accounts"])]
    plan.sort(key=lambda x: (x["date"] or "9999", x["title"]))
    tasks.sort(key=lambda x: (x["due"] or "9999", x["title"]))
    f = me.get("fields", {})
    return {**base, "model": {"name": f.get("Name", model), "language": f.get("Language", "EN"), "shootDays": f.get("Shoot days") or [],
                              "onlyfans": f.get("OnlyFans link"), "drive": f.get("Drive folder"), "accountsMode": acc_mode}, "plan": plan, "tasks": tasks,
            "today": dt.date.today().isoformat()}


def _review_titles() -> set:
    """16.11 Rene: Referenzbilder / OF-Bilder are ticked by the team after checking the images, never by her."""
    from .onboarding_portal import REVIEW_KEYS, _ob_title
    return set().union(*(_ob_title(k) for k in REVIEW_KEYS))


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
            if table == T_TASKS and r.json()["fields"].get("Task", "") in _review_titles():
                return False
        r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{table}/{rec_id}", headers={**hdr, "Content-Type": "application/json"},
                           json={"fields": {field: on if done else off}}, timeout=20)
        bust(table)
        return r.status_code == 200
    return False


LOGO = Path(__file__).with_name("portal_logo.png")


def render(d: dict) -> str:
    import base64
    tpl = Path(__file__).with_name("portal.html").read_text(encoding="utf-8")
    uri = lambda f: "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode() if f.exists() else ""
    return tpl.replace("__HEARTS__", Path(__file__).with_name("portal_hearts.js").read_text(encoding="utf-8")).replace("__LOGO__", uri(LOGO)).replace("__LOGO_N__", uri(LOGO.with_name("portal_logo_n.png"))).replace("__DATA__", json.dumps(d, ensure_ascii=False).replace("</", "<\\/"))


def build(model: str, crm_dataset: dict | None = None) -> str:
    return render(portal_data(model, crm_dataset))


if __name__ == "__main__":
    import sys
    sys.stdout.write(build(sys.argv[1] if len(sys.argv) > 1 else "Lea"))
