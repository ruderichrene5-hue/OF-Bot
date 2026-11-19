"""Standard Drive tree per model (16.11, Rene): one layout for every model so the portal, the reel
pipeline and the farm find the same folders. Contract and invoices live in the portal, not here.

    MODELS/<Name>/
      Northfeed – <Name>/            model: Leser (ONE shared folder; the welcome mail carries one link,
                                     the "Google Drive" onboarding video explains every sub-folder)
        Referenzbilder/<Kategorie>/  model: Bearbeiter  (images for the AI, filled by the portal upload)
        OF-Content/                  model: Bearbeiter
          Skripte/Skript N – …/Stufe K – …/{Bilder,Videos}   (made by scripts.ensure_folders)
          Bilder/  Videos/  Archiv/
        Social Media/Instagram/      hybrid model: the AI makes most of it, these are for the exceptions
          Rohmaterial/<YYYY-MM-DD>/  model: Bearbeiter  (a story or clip the AI can't do well; sorted into day folders)
          Fertig zum Posten/         model: Leser       (ready for her own account, if she runs one)
          Archiv/
        Custom-Anfragen/             model: Bearbeiter  (she uploads the customs fans ordered)
          Archiv/
        Stimme/                      model: Bearbeiter  (voice recording for her AI voice, video "Deine Stimme")
      KI-Content/                    team only, one per model
        Ready to Post/<YYYY-MM-DD>/  what we post (sorted into day folders)
        Archiv/
      (Intern/Vertrag, Intern/Abrechnung: backup copies written by onboarding/billing, the portal is the source)

Folder ids/links go to Airtable Models: "Drive folder" (the shared model folder), "Uploads folder" (Referenzbilder),
"Drive intern" (MODELS/<Name>, team only).
Sharing uses the Drive REST API with the OAuth token of the rclone remote; rclone itself cannot set
permissions. Migration of the pre-existing model folders is a separate, dry-run-first step
(plan_migration / migrate).
"""

from __future__ import annotations

import json
from pathlib import Path
import os
import re
import subprocess
import time

import requests

from . import portal

REMOTE = os.environ.get("MODELS_DRIVE_REMOTE", "veralta")
ROOT = os.environ.get("MODELS_DRIVE_ROOT", "MODELS")
# 16.11 Rene: folders inside "Northfeed – <Name>", parents first. "writer" = she may upload there (shared on that
# folder, inherited below); None = she only sees it through the reader share on the top folder.
MODEL_FOLDERS = {
    "Referenzbilder": "writer",
    "OF-Content": "writer", "OF-Content/Skripte": None, "OF-Content/Bilder": None, "OF-Content/Videos": None,
    "OF-Content/Archiv": None, "OF-Content/Profil": None,
    "Social Media": None, "Social Media/Instagram": None, "Social Media/Instagram/Rohmaterial": "writer",
    "Social Media/Instagram/Fertig zum Posten": None, "Social Media/Instagram/Archiv": None,
    "Custom-Anfragen": "writer", "Custom-Anfragen/Archiv": None,
    "Stimme": "writer",                          # 18.11 Rene: 30-60 min voice recording in one take for the ElevenLabs voice clone
}
REF_CATEGORIES = ["Gesicht vorne", "Gesicht links", "Gesicht rechts", "Körper vorne", "Körper seitlich"]   # = onboarding_portal.CATEGORIES keys (16.11: 5)
OF_PHOTOS = "OF-Content/Profil"                  # 17.11 Rene: profile picture + banner here; feed pictures go into OF-Content/Bilder
OF_SUB = ["Profilbild", "Banner"]
OF_COUNT = {"Profilbild": f"{OF_PHOTOS}/Profilbild", "Banner": f"{OF_PHOTOS}/Banner", "Feed": "OF-Content/Bilder"}   # = onboarding_portal.OF_CATEGORIES "folder"
POSTED = "Social Media/Instagram/Fertig zum Posten"
POST_TIMES = ["10:00", "12:30", "15:00", "17:30", "20:00"]   # 17.11 Rene: we always post 5 times a day between 10 and 20 h
POST_DAYS_AHEAD, POST_DAYS_KEEP = 5, 10                       # day folders 5 days ahead, trashed 10 days after the day
WEEKDAY = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
TEAM = "KI-Content"                              # team-only folder next to the shared one; the model never gets a share on it
INTERN = ["Ready to Post", "Archiv"]
TEAM_ONLY = {"Intern": ["Vertrag", "Abrechnung"]}   # 17.11 Rene: the whole tree exists the moment the model does
# 16.11 Rene: everything that arrives here goes into a folder named after the day -- she (or the cutter) just drops
# files in, sort_into_days() files them by upload date
DATED = ["{S}/Social Media/Instagram/Rohmaterial", "KI-Content/Ready to Post"]
F_ROOT, F_UPLOADS, F_INTERN = "Drive folder", "Uploads folder", "Drive intern"
SHARED = "Northfeed – {name}"
API = "https://www.googleapis.com/drive/v3"


def _rclone(*args: str, timeout: int = 180) -> str:
    r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    return r.stdout


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", " ", name).strip()


def _lsjson(path: str) -> list:
    out = _rclone("lsjson", "--dirs-only", f"{REMOTE}:{path}")
    return json.loads(out or "[]")


_root_id: str | None = None


def _root() -> str:
    """Drive id of the MODELS folder (looked up once per process)."""
    global _root_id
    if not _root_id:
        parent, _, leaf = ROOT.rpartition("/")
        for e in _lsjson(parent or ""):
            if e.get("Name") == leaf:
                _root_id = e["ID"]
                break
        if not _root_id:
            raise RuntimeError(f"root folder {ROOT} not found on {REMOTE}")
    return _root_id


def _req(method: str, url: str, **kw) -> requests.Response:
    """Drive API call with backoff on 403 rate limits / 429 / 5xx (Drive throttles bursts of list calls)."""
    global _tok
    for i in range(6):
        r = requests.request(method, url, headers={"Authorization": f"Bearer {_access_token()}"}, timeout=30, **kw)
        if r.status_code == 401 and i == 0:                  # 17.11: token expired under us -> fresh one, once
            _tok = ("", 0.0)
            continue
        if r.status_code in (403, 429, 500, 502, 503) and i < 5:
            if r.status_code == 403 and "rateLimit" not in r.text and "quota" not in r.text.lower() and "userRateLimit" not in r.text:
                r.raise_for_status()
            print("[drive] retry", i + 1, method, url.split("?")[0][-40:], r.status_code, r.text[:160].replace("\n", " "), flush=True)
            time.sleep(1.5 * (2 ** i))
            continue
        r.raise_for_status()
        return r
    return r


def _children(parent_id: str, name: str) -> list[dict]:
    q = f"'{parent_id}' in parents and name = '{name.replace(chr(39), chr(92) + chr(39))}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = _req("GET", f"{API}/files", params={"q": q, "fields": "files(id,name,createdTime)", "supportsAllDrives": "true",
                                            "includeItemsFromAllDrives": "true", "orderBy": "createdTime"})
    return r.json().get("files", [])


def ensure_folder_id(parent_id: str, name: str) -> str:
    """Drive id of the child folder <name> under <parent_id>; created via the Drive API when missing.
    Deterministic (no rclone path cache), and duplicates can only come from outside."""
    kids = _children(parent_id, name)
    if kids:
        return kids[0]["id"]
    r = _req("POST", f"{API}/files", params={"supportsAllDrives": "true", "fields": "id"},
             json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]})
    return r.json()["id"]


def ensure_folder(parent_path: str, name: str) -> str:
    """Path flavour kept for callers that think in paths (resolves the parent by walking from MODELS)."""
    pid = _root()
    rel = parent_path[len(ROOT):].strip("/") if parent_path.startswith(ROOT) else parent_path.strip("/")
    for part in [p for p in rel.split("/") if p]:
        pid = ensure_folder_id(pid, part)
    return ensure_folder_id(pid, name)


_folder_cache: dict[str, str] = {}


def ensure_folder_cached(parent_path: str, name: str) -> str:
    """ensure_folder with a per-process cache: reference uploads hit the same folder 35 times in a row."""
    key = f"{parent_path}/{name}"
    if key not in _folder_cache:
        _folder_cache[key] = ensure_folder(parent_path, name)
    return _folder_cache[key]


def upload_file(folder_id: str, src: Path, name: str, mime: str = "application/octet-stream") -> str:
    """One multipart upload straight into <folder_id>; returns the file id. 16.11 launch check: two rclone runs per
    reference image took ~24 s each, 35 images kept a model waiting a quarter of an hour."""
    meta = json.dumps({"name": name, "parents": [folder_id]})
    with open(src, "rb") as fh:
        files = {"metadata": ("metadata", meta, "application/json; charset=UTF-8"), "file": (name, fh, mime)}
        r = _req("POST", "https://www.googleapis.com/upload/drive/v3/files",
                 params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"}, files=files)
    return r.json()["id"]


def link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


_tok: tuple[str, float] = ("", 0.0)


def _access_token() -> str:
    """rclone keeps the OAuth token in its config and refreshes it on use; cached here for 10 minutes."""
    global _tok
    if _tok[0] and time.time() < _tok[1]:
        return _tok[0]
    _rclone("about", "--json", f"{REMOTE}:", timeout=60)                  # cheap call that forces a token refresh when expired
    dump = json.loads(_rclone("config", "dump"))
    tok = json.loads(dump[REMOTE]["token"])
    until = time.time() + 600
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$", str(tok.get("expiry") or ""))
    if m:                                    # 17.11 funnel test: the token lives 1 h and was cached past its expiry -> every Drive
        import datetime as _dt               # call of the day-folder / drive-check loops failed with 401 for up to 10 minutes
        exp = _dt.datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(2) == "Z" else m.group(2))).timestamp()
        until = min(until, exp - 15)
    _tok = (tok["access_token"], until)
    return _tok[0]


def share(folder_id: str, email: str, role: str = "reader") -> dict:
    """Grant <email> reader/writer on one folder, no notification mail (the welcome mail carries the links)."""
    email = email.strip().lower()
    if not email or role not in ("reader", "writer"):
        return {"ok": False, "error": "bad email/role"}
    h = {"Authorization": f"Bearer {_access_token()}"}
    cur = requests.get(f"{API}/files/{folder_id}/permissions", headers=h,
                       params={"fields": "permissions(id,emailAddress,role)", "supportsAllDrives": "true"}, timeout=30).json()
    for p in cur.get("permissions", []):
        if str(p.get("emailAddress", "")).lower() == email:
            if p.get("role") == role:
                return {"ok": True, "unchanged": True}
            r = requests.patch(f"{API}/files/{folder_id}/permissions/{p['id']}", headers=h, json={"role": role},
                               params={"supportsAllDrives": "true"}, timeout=30)
            return {"ok": r.ok, "updated": True, "status": r.status_code}
    r = requests.post(f"{API}/files/{folder_id}/permissions", headers=h,
                      params={"sendNotificationEmail": "false", "supportsAllDrives": "true"},
                      json={"type": "user", "role": role, "emailAddress": email}, timeout=30)
    return {"ok": r.ok, "created": True, "status": r.status_code, "error": None if r.ok else r.text[:200]}


def unshare(folder_id: str, email: str) -> dict:
    email = email.strip().lower()
    h = {"Authorization": f"Bearer {_access_token()}"}
    cur = requests.get(f"{API}/files/{folder_id}/permissions", headers=h,
                       params={"fields": "permissions(id,emailAddress)", "supportsAllDrives": "true"}, timeout=30).json()
    n = 0
    for p in cur.get("permissions", []):
        if str(p.get("emailAddress", "")).lower() == email:
            requests.delete(f"{API}/files/{folder_id}/permissions/{p['id']}", headers=h, params={"supportsAllDrives": "true"}, timeout=30)
            n += 1
    return {"ok": True, "removed": n}


def share_failed(name: str, email: str, rec: str, shares: dict) -> None:
    """16.11 Rene (A): Google shares a folder only with a Google account (Gmail). A failed share used to go unnoticed,
    the model then sees no folder. Now: one open team task + Telegram."""
    from . import cal_booking, manager
    title = f"Gmail von {name} nachfragen"
    new = True                                   # 17.11 funnel test: ensure_tree runs 2-3 times per model (signature, script folders,
    try:                                         # "Ordner anlegen"); Telegram only together with a new task, not on every rerun
        if any(str(r["fields"].get("Task", "")) == title and r["fields"].get("Status") != "Done" for r in portal._fetch(portal.T_TASKS)):
            new = False
        else:
            manager._create(portal.T_TASKS, {"Task": title, "Model": [rec], "Category": "Admin", "Status": "Open", "Priority": "High",
                                             "Assigned to": "Team", "Due type": "This week", "Visible to model": False,
                                             "Description": f"Google konnte den Drive-Ordner nicht für {email} freigeben, meist ist das keine Gmail-Adresse. "
                                                            "Gmail-Adresse beim Model erfragen, auf der Model-Seite eintragen, dann „Ordner anlegen“."})
    except Exception as e:  # noqa: BLE001
        print("[drive] share-failed task:", e)
    if not new:
        return
    err = "; ".join(f"{k}: {v.get('error') or v.get('status')}" for k, v in shares.items() if not v.get("ok"))[:300]
    cal_booking._tg(f"⚠️ Drive-Freigabe gescheitert · {name}\n{email} ist vermutlich keine Gmail.\n{err}\nhttps://portal.northfeed-management.com/manager#model/{rec}")


def revoke(email: str, fields: dict) -> dict:
    """16.11 Rene (J): Stage Beendet removes her access. Only the two folders that carry her permission
    (the shared root and Raw Material); nothing is created or deleted."""
    email = (email or "").strip().lower()
    out = {}
    if not email:
        return out
    for key in (F_ROOT, F_UPLOADS):
        m = re.search(r"/folders/([\w-]+)", str(fields.get(key) or ""))
        if m:
            try:
                out[key] = unshare(m.group(1), email)
            except Exception as e:  # noqa: BLE001
                out[key] = {"ok": False, "error": str(e)[:200]}
    return out


def ensure_tree(model_name: str, email: str = "", model_rec_id: str = "", write_airtable: bool = True) -> dict:
    """Create (idempotent) the whole tree, share the three model folders when an e-mail is known,
    write root + uploads links to Airtable. Returns ids and links."""
    name = _safe(model_name)
    if not name:
        raise ValueError("model name empty")
    root_id = ensure_folder_id(_root(), name)
    shared_id = ensure_folder_id(root_id, SHARED.format(name=name))
    ids = {"root": root_id, "shared": shared_id}
    for sub in MODEL_FOLDERS:
        parent, _, leaf = sub.rpartition("/")
        ids[sub] = ensure_folder_id(ids[parent] if parent else shared_id, leaf)
    for cat in REF_CATEGORIES:                               # one folder per reference category, she uploads in the Drive app
        ids[f"Referenzbilder/{cat}"] = ensure_folder_id(ids["Referenzbilder"], cat)
    for sub in OF_SUB:
        ids[f"{OF_PHOTOS}/{sub}"] = ensure_folder_id(ids[OF_PHOTOS], sub)
    team = ensure_folder_id(root_id, TEAM)
    ids[TEAM] = team
    for sub in INTERN:
        ids[f"{TEAM}/{sub}"] = ensure_folder_id(team, sub)
    for top, subs in TEAM_ONLY.items():
        ids[top] = ensure_folder_id(root_id, top)
        for sub in subs:
            ids[f"{top}/{sub}"] = ensure_folder_id(ids[top], sub)
    try:
        post_days(name)                                      # 17.11 Rene: posting days + times exist from the start too
    except Exception as e:  # noqa: BLE001
        print("[drive] post days", name, e)
    shares = {}
    if email:
        shares["shared"] = share(shared_id, email, "reader")             # one folder in "Shared with me"
        for sub, role in MODEL_FOLDERS.items():
            if role == "writer":
                shares[sub] = share(ids[sub], email, role)              # upload rights on top of the inherited reader
    if email and model_rec_id and any(not v.get("ok") for v in shares.values()):
        share_failed(name, email, model_rec_id, shares)
    if write_airtable and model_rec_id:
        fields = {F_ROOT: link(shared_id), F_UPLOADS: link(ids["Referenzbilder"]), F_INTERN: link(root_id)}
        r = requests.patch(f"https://api.airtable.com/v0/{portal.BASE}/{portal.T_MODELS}/{model_rec_id}",
                           headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"},
                           json={"fields": fields}, timeout=30)
        if r.status_code == 422:                                         # "Drive intern" field not created yet
            fields.pop(F_INTERN)
            requests.patch(f"https://api.airtable.com/v0/{portal.BASE}/{portal.T_MODELS}/{model_rec_id}",
                           headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"},
                           json={"fields": fields}, timeout=30)
    return {"ok": True, "name": name, "ids": ids, "links": {k: link(v) for k, v in ids.items()}, "shares": shares}


# ---------------------------------------------------------------- counting her uploads (16.11: everything goes up in Drive)

IMAGE_MIME = ("image/",)


def _shared_id(model_name: str) -> str | None:
    name = _safe(model_name)
    root = _find(_root(), name)
    return _find(root, SHARED.format(name=name)) if root else None


def folder_link(model_name: str, sub: str) -> str:
    """Link to one folder inside her shared tree ("" when it does not exist yet)."""
    sid = _shared_id(model_name)
    fid = _find(sid, sub) if sid else None
    return link(fid) if fid else ""


def _count_files(folder_id: str) -> int:
    n, token = 0, None
    while True:
        params = {"q": f"'{folder_id}' in parents and trashed = false and mimeType != 'application/vnd.google-apps.folder'",
                  "fields": "nextPageToken,files(mimeType)", "pageSize": 200, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if token:
            params["pageToken"] = token
        r = _req("GET", f"{API}/files", params=params).json()
        n += sum(1 for f in r.get("files", []) if str(f.get("mimeType", "")).startswith(IMAGE_MIME))
        token = r.get("nextPageToken")
        if not token:
            return n


def count_images(model_name: str) -> dict:
    """Images per reference category and per OnlyFans photo folder, keyed like onboarding_portal's categories."""
    sid = _shared_id(model_name)
    if not sid:
        return {}
    out = {}
    ref = _find(sid, "Referenzbilder")
    for cat in REF_CATEGORIES:
        fid = _find(ref, cat) if ref else None
        out[cat] = _count_files(fid) if fid else 0
    for key, path in OF_COUNT.items():
        fid = _find(sid, path)
        out[f"OF {key}"] = _count_files(fid) if fid else 0
    return out


def post_days(model_name: str) -> dict:
    """17.11 Rene: "Fertig zum Posten" always holds the next POST_DAYS_AHEAD days as "<YYYY-MM-DD> <Wochentag>", each with one
    folder per posting time; the cutter drops each finished reel into its time. Days older than POST_DAYS_KEEP go to the trash."""
    from zoneinfo import ZoneInfo
    import datetime as dt
    sid = _shared_id(model_name)
    pid = _find(sid, POSTED) if sid else None
    if not pid:
        return {"ok": False, "reason": "no folder"}
    today = dt.datetime.now(ZoneInfo("Europe/Berlin")).date()
    made = 0
    for i in range(POST_DAYS_AHEAD):
        d = today + dt.timedelta(days=i)
        name = f"{d.isoformat()} {WEEKDAY[d.weekday()]}"
        if not _children(pid, name):
            made += 1
        day = ensure_folder_id(pid, name)
        for t in POST_TIMES:
            ensure_folder_id(day, t)
    q = f"'{pid}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    trashed = 0
    for f in _req("GET", f"{API}/files", params={"q": q, "fields": "files(id,name)", "pageSize": 200, "supportsAllDrives": "true",
                                                 "includeItemsFromAllDrives": "true"}).json().get("files", []):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", f["name"])
        if m and dt.date.fromisoformat(m.group(1)) < today - dt.timedelta(days=POST_DAYS_KEEP):
            _req("PATCH", f"{API}/files/{f['id']}", params={"supportsAllDrives": "true", "fields": "id"}, json={"trashed": True})
            trashed += 1
    return {"ok": True, "new_days": made, "trashed": trashed}


# ---------------------------------------------------------------- day folders

def _find(parent_id: str, path: str) -> str | None:
    """Folder id under parent by path, lookup only (never creates -- the sorter must not grow trees)."""
    pid = parent_id
    for part in [p for p in path.split("/") if p]:
        kids = _children(pid, part)
        if not kids:
            return None
        pid = kids[0]["id"]
    return pid


def sort_into_days(model_name: str) -> int:
    """Move loose files in the DATED folders of one model into <YYYY-MM-DD> sub-folders (upload day, Berlin time).
    Returns how many files moved."""
    from zoneinfo import ZoneInfo
    import datetime as dt
    name = _safe(model_name)
    root = _find(_root(), name)
    if not root:
        return 0
    moved = 0
    for rel in DATED:
        fid = _find(root, rel.format(S=SHARED.format(name=name)))
        if not fid:
            continue
        q = f"'{fid}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
        files = _req("GET", f"{API}/files", params={"q": q, "fields": "files(id,createdTime)", "pageSize": 200,
                                                     "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}).json().get("files", [])
        days: dict[str, str] = {}
        for f in files:
            day = dt.datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
            if day not in days:
                days[day] = ensure_folder_id(fid, day)
            _req("PATCH", f"{API}/files/{f['id']}", params={"addParents": days[day], "removeParents": fid,
                                                            "supportsAllDrives": "true", "fields": "id"})
            moved += 1
    return moved


def sort_all() -> dict:
    """All Northfeed models with a Drive folder (old-agency models excluded)."""
    out = {}
    for r in portal.fetch_cached(portal.T_MODELS, 60):
        f = r["fields"]
        if not f.get(F_ROOT) or f.get("Alte Agentur") or not str(f.get("Name") or "").strip():
            continue
        try:
            n = sort_into_days(str(f["Name"]))
            if n:
                out[f["Name"]] = n
            _post_days_daily(str(f["Name"]))
        except Exception as e:  # noqa: BLE001
            out[f["Name"]] = f"error: {str(e)[:120]}"
    return out


_post_done: dict[str, str] = {}


def _post_days_daily(model_name: str) -> None:
    """post_days once per model and day (the sort loop runs every 15 minutes)."""
    import datetime as dt
    key, today = _safe(model_name), dt.date.today().isoformat()
    if _post_done.get(key) != today:
        if post_days(model_name).get("ok"):
            _post_done[key] = today


def sort_loop(every: int = 900) -> None:
    while True:
        try:
            res = sort_all()
            if res:
                print("[drive] day folders:", res)
        except Exception as e:  # noqa: BLE001
            print("[drive] day sort failed:", e)
        time.sleep(every)


# ---------------------------------------------------------------- migration of the pre-existing folders

OLD_MAP = [                                       # (regex on the old sub-path, new sub-path) — first match wins; {S} = shared folder
    (r"^Reels Instagram - [^/]+/Raw Material \(Model\)$", "{S}/Social Media/Instagram/Rohmaterial"),
    (r"^Reels Instagram - [^/]+/Ready to Post \(Cutter\) ?$", "KI-Content/Ready to Post"),
    (r"^Reels Instagram - [^/]+/Scheduled \([^)]*\)$", "{S}/Social Media/Instagram/Fertig zum Posten"),
    (r"^Reels Instagram - [^/]+/Spoofed Content$", "KI-Content/Archiv/Spoofed Content"),
    (r"^Reels Instagram - [^/]+/[Aa]rchive?$", "{S}/Social Media/Instagram/Archiv"),
    (r"^Skripte$", "{S}/OF-Content/Skripte"),
    (r"^Bilder/IG$", "{S}/Social Media/Instagram/Rohmaterial"),
    (r"^Bilder/OF$", "{S}/OF-Content/Bilder"),
    (r"^Custom (Content|Anfrage)$", "{S}/Custom-Anfragen"),
    (r"^OF Videos$", "{S}/OF-Content/Videos"),
    (r"^X$", "KI-Content/Archiv/X"),
]


def plan_migration(model_folder: str) -> list[dict]:
    """Dry run: which old sub-folders would move where (contents merged into the new folder)."""
    base = f"{ROOT}/{model_folder}"
    moves = []
    level1 = _lsjson(base)
    cands = [e["Name"] for e in level1]
    for e in level1:
        if e["Name"].startswith("Reels Instagram") or e["Name"] == "Bilder":
            cands += [f"{e['Name']}/{x['Name']}" for x in _lsjson(f"{base}/{e['Name']}")]
    for sub in cands:
        for rx, new in OLD_MAP:
            if re.match(rx, sub):
                n = int(subprocess.run(["rclone", "size", "--json", f"{REMOTE}:{base}/{sub}"], capture_output=True, text=True).stdout and
                        json.loads(subprocess.run(["rclone", "size", "--json", f"{REMOTE}:{base}/{sub}"], capture_output=True, text=True).stdout)["count"] or 0)
                moves.append({"from": sub, "to": new.replace("{S}", SHARED.format(name=_safe(model_folder))), "files": n})
                break
    return moves


def migrate(model_folder: str, apply: bool = False) -> dict:
    """Move contents of the old sub-folders into the standard tree (server-side moves, nothing deleted;
    the emptied old folders stay until a human removes them)."""
    moves = plan_migration(model_folder)
    if not apply:
        return {"dry_run": True, "moves": moves}
    ensure_tree(model_folder, write_airtable=False)
    done = []
    for m in moves:
        src = f"{REMOTE}:{ROOT}/{model_folder}/{m['from']}"
        dst = f"{REMOTE}:{ROOT}/{model_folder}/{m['to']}"
        _rclone("move", src, dst, "--drive-server-side-across-configs", "--create-empty-src-dirs", timeout=1800)
        done.append(m)
    return {"dry_run": False, "moves": done}
