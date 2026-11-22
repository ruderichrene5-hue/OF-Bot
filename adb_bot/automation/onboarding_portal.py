"""Model-side onboarding (15.11, Rene): after the first login a creator finishes two steps before she sees
her content plan -- (1) the Steckbrief (persona / intake questionnaire, contract §4 Abs. 2) and (2) the
Referenzbilder, the structured talent-image set the AI trains on. "Upload your images once" replaces
"shoot once a week" as the promise, so this is the first thing she does and nobody at Northfeed touches it.

Data: Airtable base "Northfeed Portal" -- tables Steckbrief (one row per model) and Referenzbilder (one row
per uploaded file); files go to Drive MODELS/<Name>/Raw Material/Referenzbilder/<Kategorie>/ via rclone.
When both steps are complete the model's Stage moves Unterschrieben/Onboarding -> Material da and the
onboarding task is ticked.

    onboarding_portal.state(email)                                  -> dict (steps, counts, done)
    onboarding_portal.save_steckbrief(email, body)                  -> dict
    onboarding_portal.save_reference(email, category, fileobj, filename, size) -> dict
    onboarding_portal.build(email, lang)                            -> html
    onboarding_portal.manager_summary(model_rec_id)                 -> dict for the manager's model page
    onboarding_portal.handle_upload(handler, email)                 -> JSON response helper (stdlib server)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

from . import portal, drive_tree

T_STECKBRIEF = os.environ.get("PORTAL_STECKBRIEF_TABLE", "tblAz1EpHsOh7JDuR")
F_OF_NEEDED = "OF-Bilder nötig"                                   # Models checkbox, 16.11 Rene: OF photos task only when ticked
T_REFS = os.environ.get("PORTAL_REFS_TABLE", "tbl2YtqY89WG2KT5w")
TASK_TITLE = "Steckbrief ausfüllen und Referenzbilder hochladen"
STAGE_DONE = "Onboarding"        # 15.11 Rene: "Material da" column removed; completion only closes the task

# the standard set (memory: northfeed-positioning-reference-images)
CATEGORIES = [                     # 16.11 Rene (later): one good picture each, face front + both sides, body front + side; plain clothes
    {"key": "Gesicht vorne", "min": 1, "max": 3, "video": False,
     "de": ("Gesicht von vorne", "Gerade in die Kamera, Gesicht gut ausgeleuchtet. Schlichte, normale Kleidung, nichts Freizügiges und nicht extra eng. Natürlich wie du bist: wenig Make-up, kein Filter, gutes Licht."),
     "en": ("Face, front", "Straight into the camera, face well lit. Simple, normal clothes, nothing revealing and not extra tight. Natural, as you are: little make-up, no filter, good light.")},
    {"key": "Gesicht links", "min": 1, "max": 3, "video": False,
     "de": ("Gesicht von links", "Linke Seite im Profil. Schlichte, normale Kleidung, nichts Freizügiges und nicht extra eng. Natürlich wie du bist: wenig Make-up, kein Filter, gutes Licht."),
     "en": ("Face, left side", "Left side in profile. Simple, normal clothes, nothing revealing and not extra tight. Natural, as you are: little make-up, no filter, good light.")},
    {"key": "Gesicht rechts", "min": 1, "max": 3, "video": False,
     "de": ("Gesicht von rechts", "Rechte Seite im Profil. Schlichte, normale Kleidung, nichts Freizügiges und nicht extra eng. Natürlich wie du bist: wenig Make-up, kein Filter, gutes Licht."),
     "en": ("Face, right side", "Right side in profile. Simple, normal clothes, nothing revealing and not extra tight. Natural, as you are: little make-up, no filter, good light.")},
    {"key": "Körper vorne", "min": 1, "max": 3, "video": False,
     "de": ("Körper von vorne", "Ganzer Körper, stehend, von Kopf bis Fuß. Schlichte, normale Kleidung, nichts Freizügiges und nicht extra eng. Natürlich wie du bist: wenig Make-up, kein Filter, gutes Licht."),
     "en": ("Body, front", "Full body, standing, head to toe. Simple, normal clothes, nothing revealing and not extra tight. Natural, as you are: little make-up, no filter, good light.")},
    {"key": "Körper seitlich", "min": 1, "max": 3, "video": False,
     "de": ("Körper von der Seite", "Ganzer Körper von der Seite, damit man die Figur sieht. Schlichte, normale Kleidung, nichts Freizügiges und nicht extra eng. Natürlich wie du bist: wenig Make-up, kein Filter, gutes Licht."),
     "en": ("Body, side", "Full body from the side so your figure shows. Simple, normal clothes, nothing revealing and not extra tight. Natural, as you are: little make-up, no filter, good light.")},
]
# 16.11 Rene: real photos for her OnlyFans page (profile picture, banner, feed) are a standard task too; same upload,
# own section, files go to OF-Content/Profil & Feed/<Bereich>. A banner is landscape, so the size floor is lower there.
OF_CATEGORIES = [
    {"key": "OF Profilbild", "min": 1, "max": 3, "video": False, "min_px": 600, "folder": "Profilbild",
     "de": ("Profilbild", "Gesicht gut zu sehen, freundlich, gutes Licht. Wir wählen eins aus."),
     "en": ("Profile picture", "Face clearly visible, friendly, good light. We pick one.")},
    {"key": "OF Banner", "min": 1, "max": 2, "video": False, "min_px": 400, "folder": "Banner",
     "de": ("Banner", "Querformat, zum Beispiel ein schönes Ganzkörper- oder Lifestyle-Bild."),
     "en": ("Banner", "Landscape, for example a nice full-body or lifestyle shot.")},
    {"key": "OF Feed", "min": 20, "max": 40, "video": False, "min_px": 1080, "folder": "Feed",
     "de": ("Feed-Bilder", "Echte Fotos von dir für deine ersten Posts: verschiedene Outfits, Orte und Posen. Keine KI-Bilder."),
     "en": ("Feed pictures", "Real photos of you for your first posts: different outfits, places and poses. No AI images.")},
]
ALL_CATEGORIES = CATEGORIES + OF_CATEGORIES
OF_KEYS = {c["key"] for c in OF_CATEGORIES}
CAT_KEYS = {c["key"] for c in ALL_CATEGORIES}
MAX_IMAGE = 25 * 1024 * 1024
MAX_VIDEO = 200 * 1024 * 1024
MIN_SHORT_SIDE = 1080
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}

STECKBRIEF_FIELDS = {                      # json key -> Airtable field
    "kuenstlername": "Künstlername", "alter": "Alter", "groesse": "Größe cm", "herkunft": "Herkunft / Stadt",
    "sprachen": "Sprachen", "zeitzone": "Zeitzone", "persoenlichkeit": "Persönlichkeit in 3 Worten",
    "hobbys": "Hobbys / Interessen", "beruf": "Beruf-Story", "beziehung": "Beziehungsstatus-Story",
    "outfits": "Lieblingsoutfits", "tattoos": "Tattoos / Piercings", "content": "Content-Arten ok",
    "tabus": "Tabus / No-Gos", "preisAbo": "Preis Abo", "preisPpv": "Preis PPV", "preisCustoms": "Preis Customs",
    "tonfall": "Tonfall im Chat", "emoji": "Emoji-Stil", "niemals": "Sätze die sie nie sagen würde",
    "social": "Social Handles", "auszahlung": "Auszahlungsweg", "notizen": "Notizen",
}
REQUIRED = ("alter", "herkunft")   # Künstlername optional: she may not have one yet; 16.11 Rene: languages are set by the team in the manager
REVIEW_KEYS = ("bilder", "ofbilder")   # 16.11 Rene: image tasks are only Done once the team has checked the pictures   # 16.11 Rene: only what the chatters need (name, age, city, languages, tattoos, height)
TEAM = {"kuenstlername": "Künstlername", "alter": "Alter", "herkunft": "Stadt"}   # she may leave these to us -> Steckbrief."Team legt fest"
F_TEAM = "Team legt fest"
NUMERIC = {"alter", "groesse", "preisAbo", "preisPpv", "preisCustoms"}
_lock = threading.Lock()


# ---------------------------------------------------------------- airtable

def _hdr() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"}


def _url(table: str, rec: str = "") -> str:
    return f"https://api.airtable.com/v0/{portal.BASE}/{table}" + (f"/{rec}" if rec else "")


def _check(r: requests.Response) -> dict:
    if r.status_code >= 300:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except Exception:  # noqa: BLE001
            msg = r.text
        raise ValueError(str(msg)[:300])
    return r.json()


def _create(table: str, fields: dict) -> dict:
    r = _check(requests.post(_url(table), headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=40))
    portal.bust(table)                                       # after the write, so no reader re-caches the old rows
    return r


def _update(table: str, rec: str, fields: dict) -> dict:
    r = _check(requests.patch(_url(table, rec), headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=40))
    portal.bust(table)
    return r


def _s(v) -> str:
    return "" if v is None else str(v)


def _model(email: str) -> dict | None:
    email = email.strip().lower()
    for r in portal.fetch_cached(portal.T_MODELS, 20):
        if str(r["fields"].get("Email") or "").strip().lower() == email:
            return r
    return None


def _model_by_id(rec: str) -> dict | None:
    r = requests.get(_url(portal.T_MODELS, rec), headers=_hdr(), timeout=40)
    return r.json() if r.ok else None


def _linked(table: str, model_rec: str) -> list[dict]:
    """Rows of <table> linked to the model. ARRAYJOIN({Model}) yields display names, not ids, so a formula
    cannot filter by record id; the tables are small, so read and filter here."""
    return [r for r in portal.fetch_cached(table, 20) if model_rec in (r["fields"].get("Model") or [])]


def _steckbrief(model_rec: str) -> dict | None:
    rows = _linked(T_STECKBRIEF, model_rec)
    return rows[0] if rows else None


def _refs(model_rec: str) -> list[dict]:
    return _linked(T_REFS, model_rec)


# ---------------------------------------------------------------- state

def _counts(refs: list[dict]) -> dict[str, int]:
    out = {c["key"]: 0 for c in ALL_CATEGORIES}
    for r in refs:
        k = _s(r["fields"].get("Kategorie"))
        if k in out:
            out[k] += 1
    return out


def _refs_done(counts: dict[str, int]) -> bool:
    return all(counts.get(c["key"], 0) >= c["min"] for c in CATEGORIES)


def _of_done(counts: dict[str, int]) -> bool:
    return all(counts.get(c["key"], 0) >= c["min"] for c in OF_CATEGORIES)


def state(email: str) -> dict:
    """Everything the onboarding page needs: both steps, their completion, per-category counts, the model's name."""
    from concurrent.futures import ThreadPoolExecutor   # 15.11 Rene "langsam": three Airtable reads at once, not one after another
    with ThreadPoolExecutor(3) as ex:
        f_m, f_sb, f_rf = ex.submit(_model, email), ex.submit(portal.fetch_cached, T_STECKBRIEF, 20), ex.submit(portal.fetch_cached, T_REFS, 20)
        m = f_m.result()
        if not m:
            raise ValueError("unknown model")
        sb_rows = [r for r in f_sb.result() if m["id"] in (r["fields"].get("Model") or [])]
        refs = [r for r in f_rf.result() if m["id"] in (r["fields"].get("Model") or [])]
    f = m["fields"]
    sb = sb_rows[0] if sb_rows else None
    counts = {**_counts([]), **_drive_counts(m["id"])}      # 16.11: images live in Drive, counted by drive_check()
    sb_done = bool(sb and _s(sb["fields"].get("Status")) in ("Ausgefüllt", "Vom Team geprüft"))
    r_done = _refs_done(counts)
    values = {}
    if sb:
        for k, fld in STECKBRIEF_FIELDS.items():
            v = sb["fields"].get(fld)
            if v not in (None, "", []):
                values[k] = v
        for k, lbl in TEAM.items():
            if lbl in (sb["fields"].get(F_TEAM) or []):
                values[k] = "__team__"
    files = sorted(({"id": r["id"], "name": _s(r["fields"].get("Dateiname")), "cat": _s(r["fields"].get("Kategorie")),
                     "link": _s(r["fields"].get("Drive link")), "at": _s(r["fields"].get("Uploaded at"))} for r in refs),
                   key=lambda x: x["at"])
    return {"model": {"id": m["id"], "name": _s(f.get("Name")).strip(), "email": email.strip().lower(),
                      "language": _s(f.get("Language") or "DE"), "stage": _s(f.get("Stage"))},
            "steckbrief": {"done": sb_done, "status": _s(sb["fields"].get("Status")) if sb else "Offen", "values": values,
                           "required": list(REQUIRED), "fields": list(STECKBRIEF_FIELDS)},
            "references": {"done": r_done, "counts": counts, "files": files,
                           "categories": [{"key": c["key"], "min": c["min"], "max": c["max"], "video": c["video"],
                                           "de": c["de"], "en": c["en"]} for c in CATEGORIES]},
            "ofimages": {"done": _of_done(counts),
                         "categories": [{"key": c["key"], "min": c["min"], "max": c["max"], "video": False,
                                         "de": c["de"], "en": c["en"]} for c in OF_CATEGORIES]},
            "complete": sb_done and r_done,
            "limits": {"image_mb": MAX_IMAGE // 1024 // 1024, "video_mb": MAX_VIDEO // 1024 // 1024, "min_px": MIN_SHORT_SIDE}}


# ---------------------------------------------------------------- task + stage side effects

def ensure_task(model_rec: str) -> str | None:
    """The onboarding task exists once per model (created the first time she opens /onboarding).
    16.11: team-side only; the model sees the card „Deine nächsten Schritte“, a second identical task was noise."""
    for r in _linked(portal.T_TASKS, model_rec):
        if _s(r["fields"].get("Task")).strip() == TASK_TITLE:
            return r["id"]
    try:
        r = _create(portal.T_TASKS, {"Task": TASK_TITLE, "Model": [model_rec], "Category": "Content", "Status": "Open",
                                     "Priority": "High", "Assigned to": "Model", "Visible to model": False, "Due type": "This week",
                                     "Description": "Dein erster Schritt im Portal: Steckbrief ausfüllen und deine Referenzbilder hochladen. Danach planen wir deinen Content."})
        return r["id"]
    except ValueError as e:
        print("[onboarding] task create failed:", e)
        return None


# 16.11 Rene: after the welcome videos everything is open; what is still to do shows up as normal tasks in "Aufgaben"
OB_TASKS = [
    ("steckbrief", {"de": "Steckbrief ausfüllen", "en": "Fill in your profile"}, "/onboarding",
     {"de": "Ein paar Infos über dich, damit Team und Chatter dich richtig treffen.", "en": "A few facts about you so the team and chatters get you right."}),
    ("bilder", {"de": "Referenzbilder hochladen", "en": "Upload reference images"}, "drive:Referenzbilder",
     {"de": "In deinem Drive-Ordner „Referenzbilder“ in jeden der 5 Unterordner etwa 5 Bilder: Gesicht vorne, links, rechts, "
            "Körper vorne und seitlich. Lieber ein paar mehr, damit wir die besten auswählen können. Schlichte, normale Kleidung, nichts Freizügiges. Sobald alles drin ist, schauen wir es uns an und haken die Aufgabe für dich ab.",
      "en": "In your Drive folder \"Referenzbilder\" about 5 pictures into each of the 5 sub-folders: face front, left, right, "
            "body front and side. Rather a few more, so we can pick the best ones. Simple, normal clothes, nothing revealing. Once everything is in, we check it and tick the task off for you."}),
    ("ofbilder", {"de": "Bilder für OnlyFans hochladen", "en": "Upload photos for OnlyFans"}, "drive:OF-Content",
     {"de": "In deinem Drive-Ordner „OF-Content“: unter „Profil“ 1 Profilbild und 1 Banner (Querformat), unter „Bilder“ etwa 20 Bilder für deinen Feed. "
            "Echte Fotos von dir, keine KI. Sobald alles drin ist, schauen wir es uns an und haken die Aufgabe für dich ab.",
      "en": "In your Drive folder \"OF-Content\": under \"Profil\" 1 profile picture and 1 banner (landscape), under \"Bilder\" about 20 pictures for your feed. "
            "Real photos of you, no AI. Once everything is in, we check it and tick the task off for you."}),
    ("zugaenge", {"de": "Zugänge eintragen", "en": "Add your logins"}, "/zugaenge",
     {"de": "Login für OnlyFans und deine anderen Fanseiten, damit wir uns für dich einloggen können. Optional dein eigenes Instagram.",
      "en": "Login for OnlyFans and your other fan pages so we can log in for you. Optionally your own Instagram."}),
]


def _ob_title(key: str) -> set:
    return next(set(t.values()) for k, t, _, _ in OB_TASKS if k == key)


_ob_lock = threading.Lock()


def ensure_onboarding_tasks(model_rec: str) -> None:
    """Create the onboarding tasks once (in her language); already finished parts are created as Done.
    16.11 launch check: the player reports the last video twice within a second, so two callers raced and the model
    got every task twice; one at a time, each reading the tasks fresh.
    21.11 Rene: models from the old agency (Kati, Mandy) get no automatic tasks or starter scripts; the team sets them by hand."""
    if (_model_by_id(model_rec) or {}).get("fields", {}).get("Alte Agentur"):
        return
    with _ob_lock:
        portal.bust(portal.T_TASKS)                        # _linked reads a 20 s cache that would not show the first caller's rows
        _ensure_onboarding_tasks(model_rec)
        portal.bust(portal.T_TASKS)
        try:                                               # 16.11 Rene: Skript 1 + 2 come with the first tasks
            from . import scripts
            scripts.assign_starter(model_rec)
        except Exception as e:  # noqa: BLE001
            print("[onboarding] starter scripts:", e)
        portal.bust(portal.T_TASKS)


def _ensure_onboarding_tasks(model_rec: str) -> None:
    m = _model_by_id(model_rec) or {}
    f = m.get("fields", {})
    lang = "en" if _s(f.get("Language")).upper().startswith("EN") else "de"
    have = {_s(r["fields"].get("Task")).strip() for r in _linked(portal.T_TASKS, model_rec)}
    st = None
    for key, title, href, desc in OB_TASKS:
        if have & set(title.values()):
            continue
        if key == "ofbilder" and not f.get(F_OF_NEEDED):          # 16.11 Rene: optional, only for models without a ready OF profile
            continue
        if st is None:
            sb = _steckbrief(model_rec)
            dc = _drive_counts(model_rec)
            st = {"steckbrief": bool(sb and _s(sb["fields"].get("Status")) in ("Ausgefüllt", "Vom Team geprüft")),
                  "bilder": _refs_done(dc), "ofbilder": _of_done(dc)}
        if href and href.startswith("drive:"):                 # 16.11 Rene: uploads only in Drive, the task opens the folder
            try:
                link = drive_tree.folder_link(_s(f.get("Name")).strip(), href[6:]) or _s(f.get("Drive folder"))
            except Exception as e:  # noqa: BLE001
                print("[onboarding] folder link", key, e)
                link = _s(f.get("Drive folder"))
        else:
            link = href or _s(f.get("Drive folder"))
        try:
            _create(portal.T_TASKS, {"Task": title[lang], "Model": [model_rec], "Category": "Onboarding", "Status": "Done" if st.get(key) else "Open",
                                     "Priority": "High", "Assigned to": "Model", "Visible to model": True, "Due type": "By",
                                     "Due": (dt.date.today() + dt.timedelta(days=3)).isoformat(),   # 16.11 Rene: Steckbrief, Bilder, OF-Bilder, Zugänge within 3 days; open "bis" tasks sit under "Heute"
                                     "Description": desc[lang], **({"Drive folder": link} if link else {})})
        except ValueError as e:
            print("[onboarding] task", key, e)


def sync_of_task(model_rec: str) -> None:
    """Manager switch "OF-Bilder nötig": on -> the task exists and is visible (created with its 3-day deadline if missing),
    off -> an open task is hidden from the model (kept for the team, never deleted)."""
    with _ob_lock:
        m = _model_by_id(model_rec) or {}
        need = bool(m.get("fields", {}).get(F_OF_NEEDED))
        titles = _ob_title("ofbilder")
        rows = [r for r in _linked(portal.T_TASKS, model_rec) if _s(r["fields"].get("Task")).strip() in titles]
        for r in rows:
            if _s(r["fields"].get("Status")) != "Done" and bool(r["fields"].get("Visible to model")) != need:
                _update(portal.T_TASKS, r["id"], {"Visible to model": need})
        if need and not rows:
            _ensure_onboarding_tasks(model_rec)
        portal.bust(portal.T_TASKS)


def close_onboarding_task(model_rec: str, key: str) -> None:
    titles = _ob_title(key)
    for r in _linked(portal.T_TASKS, model_rec):
        if _s(r["fields"].get("Task")).strip() in titles and _s(r["fields"].get("Status")) != "Done":
            _update(portal.T_TASKS, r["id"], {"Status": "Done"})


DRIVE_COUNTS = Path("/opt/northfeed-drivecounts")          # last Drive count per model, written by drive_check()


def _drive_counts(model_rec: str) -> dict:
    try:
        return json.loads((DRIVE_COUNTS / f"{model_rec}.json").read_text()).get("counts", {})
    except Exception:  # noqa: BLE001
        return {}


_checked: dict = {}


def drive_check(model_rec: str, min_gap: float = 60) -> dict:
    """16.11 Rene: she uploads everything in Drive. Count her images there, remember the counts and close the image tasks
    (and the onboarding, with Rene's Telegram) once every folder has enough. Throttled per model."""
    import time as _time
    if _time.time() - _checked.get(model_rec, 0) < min_gap:
        return _drive_counts(model_rec)
    _checked[model_rec] = _time.time()
    m = _model_by_id(model_rec) or {}
    name = _s(m.get("fields", {}).get("Name")).strip()
    if not name:
        return {}
    counts = drive_tree.count_images(name)
    if not counts:
        return {}
    DRIVE_COUNTS.mkdir(parents=True, exist_ok=True)
    (DRIVE_COUNTS / f"{model_rec}.json").write_text(json.dumps({"counts": counts, "at": dt.datetime.now().isoformat(timespec="seconds")}))
    if _of_done(counts):
        await_review(model_rec, "ofbilder")
    if _refs_done(counts):
        await_review(model_rec, "bilder")
    return counts


def await_review(model_rec: str, key: str) -> None:
    """All images are in Drive: the task waits for the team (shown to her as "wird geprüft"); Rene gets one Telegram."""
    titles = _ob_title(key)
    for r in _linked(portal.T_TASKS, model_rec):
        if _s(r["fields"].get("Task")).strip() in titles and _s(r["fields"].get("Status")) == "Open":
            _update(portal.T_TASKS, r["id"], {"Status": "Waiting"})
            portal.bust(portal.T_TASKS)
            try:
                from . import cal_booking
                name = _s((_model_by_id(model_rec) or {}).get("fields", {}).get("Name"))
                what = "Referenzbilder" if key == "bilder" else "OF-Bilder"
                cal_booking._tg(f"🖼 {what} von {name} sind im Drive, bitte prüfen und bestätigen.\n"
                                f"https://portal.northfeed-management.com/manager#model/{model_rec}")
            except Exception:  # noqa: BLE001
                pass


def confirm_images(model_rec: str, key: str) -> dict:
    """Manager button: the team checked the images -> task Done (and the onboarding, if the Steckbrief is in too)."""
    if key not in REVIEW_KEYS:
        raise ValueError("unknown")
    close_onboarding_task(model_rec, key)
    portal.bust(portal.T_TASKS)
    return {"ok": True, **(_finish_if_complete(model_rec) if key == "bilder" else {})}


def set_languages(model_rec: str, text: str) -> dict:
    """Manager: the languages the chatters write in, stored on her Steckbrief (created if she has none yet)."""
    if not (_model_by_id(model_rec) or {}).get("id"):
        raise ValueError("unknown model")
    sb = _steckbrief(model_rec)
    val = text.strip()[:200] or None
    if sb:
        _update(T_STECKBRIEF, sb["id"], {"Sprachen": val})
    else:
        _create(T_STECKBRIEF, {"Model": [model_rec], "Status": "Offen", "Sprachen": val})
    portal.bust(T_STECKBRIEF)
    return {"ok": True, "languages": val or ""}


def _task_status(model_rec: str, key: str) -> str:
    titles = _ob_title(key)
    return next((_s(r["fields"].get("Status")) for r in _linked(portal.T_TASKS, model_rec) if _s(r["fields"].get("Task")).strip() in titles), "")


def drive_check_open(model_rec: str) -> None:
    """Background helper for page loads: only when one of the two image tasks is still open."""
    titles = _ob_title("bilder") | _ob_title("ofbilder")
    if any(_s(r["fields"].get("Task")).strip() in titles and _s(r["fields"].get("Status")) != "Done" for r in _linked(portal.T_TASKS, model_rec)):
        drive_check(model_rec)


def drive_check_all() -> dict:
    out = {}
    for r in portal.fetch_cached(portal.T_MODELS, 60):
        f = r["fields"]
        if f.get("Alte Agentur") or not f.get("Drive folder") or f.get("Status") == "Ended":
            continue
        try:
            drive_check_open(r["id"])
        except Exception as e:  # noqa: BLE001
            out[str(f.get("Name"))] = str(e)[:120]
    return out


def drive_check_loop(every: int = 300) -> None:
    """Every 5 minutes: models with an open image task get their Drive folders counted (tasks close themselves)."""
    import time as _time
    try:                                                   # warm Drive once at start-up, so the first click does not pay ~20 s
        drive_tree._root(); drive_tree._access_token()
    except Exception as e:  # noqa: BLE001
        print("[onboarding] drive warm-up:", e)
    while True:
        try:
            res = drive_check_all()
            if res:
                print("[onboarding] drive check:", res, flush=True)
        except Exception as e:  # noqa: BLE001
            print("[onboarding] drive check failed:", e, flush=True)
        _time.sleep(every)


def _finish_if_complete(model_rec: str) -> dict:
    """Both steps done -> task Done, Stage -> Material da (only from Unterschrieben/Onboarding)."""
    m = _model_by_id(model_rec) or {}
    f = m.get("fields", {})
    sb = _steckbrief(model_rec)
    sb_done = bool(sb and _s(sb["fields"].get("Status")) in ("Ausgefüllt", "Vom Team geprüft"))
    if not (sb_done and _task_status(model_rec, "bilder") == "Done"):   # 16.11 Rene: images count once the team confirmed them
        return {"complete": False}
    out = {"complete": True}
    tid = ensure_task(model_rec)
    if tid:
        was_done = any(r["id"] == tid and _s(r["fields"].get("Status")) == "Done" for r in _linked(portal.T_TASKS, model_rec))
        _update(portal.T_TASKS, tid, {"Status": "Done"})
        out["task"] = tid
        if not was_done:                                   # 16.11 Rene (G): tell him once when she is through
            try:
                from . import cal_booking
                cal_booking._tg(f"✅ Onboarding fertig · {_s(f.get('Name'))}\nSteckbrief und Referenzbilder sind komplett.\n"
                                f"https://portal.northfeed-management.com/manager#model/{model_rec}")
            except Exception:  # noqa: BLE001
                pass
    if _s(f.get("Stage")) == "Unterschrieben":
        _update(portal.T_MODELS, model_rec, {"Stage": STAGE_DONE})
        out["stage"] = STAGE_DONE
    return out


# ---------------------------------------------------------------- steckbrief

def save_steckbrief(email: str, body: dict) -> dict:
    """Upsert the model's Steckbrief row. `submit`=true marks it Ausgefüllt (requires the REQUIRED keys)."""
    m = _model(email)
    if not m:
        raise ValueError("unknown model")
    fields: dict = {}
    team = [k for k in TEAM if body.get(k) == "__team__"]
    for k in team:
        body[k] = ""
    if any(k in body for k in TEAM):
        fields[F_TEAM] = [TEAM[k] for k in team]
    for k, fld in STECKBRIEF_FIELDS.items():
        if k not in body:
            continue
        v = body[k]
        if k in NUMERIC:
            try:
                v = float(str(v).replace(",", ".")) if str(v).strip() != "" else None
            except ValueError:
                raise ValueError(f"{fld}: bitte eine Zahl")
            if v is not None and k == "alter" and not (18 <= v <= 99):
                raise ValueError("Alter: 18 oder älter")
        elif k == "content":
            v = [str(x) for x in (v if isinstance(v, list) else str(v).split(","))] if v else []
            v = [x.strip() for x in v if x.strip()]
        else:
            v = str(v).strip()[:4000]
        fields[fld] = v if v not in ("", None) else None
    submit = bool(body.get("submit"))
    if submit:
        missing = [k for k in REQUIRED if not body.get(k) and k not in team]
        if missing:
            raise ValueError("missing:" + ",".join(missing))
        fields["Status"] = "Ausgefüllt"
        fields["Submitted at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    sb = _steckbrief(m["id"])
    if sb:
        if not submit and _s(sb["fields"].get("Status")) == "Vom Team geprüft":
            fields.pop("Status", None)
        rec = _update(T_STECKBRIEF, sb["id"], fields)["id"]
    else:
        fields.setdefault("Status", "Offen")
        fields["Model"] = [m["id"]]
        fields.setdefault("Künstlername", _s(m["fields"].get("Name")).strip())
        rec = _create(T_STECKBRIEF, fields)["id"]
    out = {"ok": True, "id": rec, "submitted": submit}
    if submit:
        close_onboarding_task(m["id"], "steckbrief")
        out.update(_finish_if_complete(m["id"]))
    return out


# ---------------------------------------------------------------- references

def _probe(path: Path) -> dict:
    """width/height (+duration for video) via ffprobe; HEIC is decoded by ImageMagick if ffprobe cannot."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=60).stdout
        j = json.loads(out or "{}")
        st = (j.get("streams") or [{}])[0]
        return {"w": int(st.get("width") or 0), "h": int(st.get("height") or 0),
                "dur": float((j.get("format") or {}).get("duration") or 0)}
    except Exception:  # noqa: BLE001
        return {"w": 0, "h": 0, "dur": 0.0}


def _drive_put(model_name: str, category: str, src: Path, filename: str, parent: str = "Referenzbilder") -> str:
    """Copy into <ROOT>/<Name>/Northfeed – <Name>/Referenzbilder/<Kategorie>/ (16.11: the folder she sees in Drive) and return the link."""
    name = drive_tree._safe(model_name)
    shared = drive_tree.SHARED.format(name=name)
    fid = drive_tree.ensure_folder_cached(f"{drive_tree.ROOT}/{name}/{shared}/{parent}", category)   # creates the chain if missing
    import mimetypes
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    try:
        file_id = drive_tree.upload_file(fid, src, filename, mime)
    except Exception:  # noqa: BLE001                      # folder deleted or moved since it was cached: look it up again
        drive_tree._folder_cache.clear()
        fid = drive_tree.ensure_folder_cached(f"{drive_tree.ROOT}/{name}/{shared}/{parent}", category)
        file_id = drive_tree.upload_file(fid, src, filename, mime)
    return f"https://drive.google.com/file/d/{file_id}/view"


def save_reference(email: str, category: str, fileobj, filename: str, size: int) -> dict:
    """One file per call. Validates type/size/resolution, stores to Drive, writes the Referenzbilder row,
    returns the new counts and whether onboarding is now complete. Error messages are for the creator."""
    raise ValueError("Bitte lade deine Bilder direkt in deinen Google-Drive-Ordner hoch. Die Aufgabe auf der Startseite öffnet ihn.")
    if category not in CAT_KEYS:                             # 16.11 Rene: nothing is uploaded in the portal any more
        raise ValueError("Unbekannte Kategorie")
    import time as _time
    _t0 = _time.time(); _tl = []
    m = _model(email)
    _tl.append(("model", _time.time()))
    if not m:
        raise ValueError("unknown model")
    cat = next(c for c in ALL_CATEGORIES if c["key"] == category)
    min_px = cat.get("min_px", MIN_SHORT_SIDE)
    ext = Path(filename or "").suffix.lower()
    is_video = ext in VIDEO_EXT
    if cat["video"] and not is_video:
        raise ValueError("Für diese Kategorie bitte ein Video (MP4 oder MOV)")
    if not cat["video"] and ext not in IMAGE_EXT:
        raise ValueError("Bitte ein Foto (JPG, PNG oder HEIC)")
    if size > (MAX_VIDEO if is_video else MAX_IMAGE):
        raise ValueError(f"Datei zu groß (max. {MAX_VIDEO // 1024 // 1024 if is_video else MAX_IMAGE // 1024 // 1024} MB)")
    counts = _counts(_refs(m["id"]))
    _tl.append(("counts", _time.time()))
    if counts.get(category, 0) >= cat["max"]:
        raise ValueError(f"{category}: schon {cat['max']} Dateien da, das reicht")
    src = Path(getattr(fileobj, "name", "")) if hasattr(fileobj, "name") else None
    tmp = None
    if not src or not src.exists():
        import tempfile
        tmp = Path(tempfile.NamedTemporaryFile(prefix="nfref_", suffix=ext, delete=False).name)
        with open(tmp, "wb") as fh:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        src = tmp
    try:
        pr = _probe(src)
        short = min(pr["w"], pr["h"]) if pr["w"] and pr["h"] else 0
        if short and short < min_px:
            raise ValueError(f"Zu klein ({pr['w']}×{pr['h']}). Bitte das Original schicken, mindestens {min_px} px kurze Seite.")
        if is_video and pr["dur"] and pr["dur"] < 5:
            raise ValueError("Das Video ist kürzer als 5 Sekunden. Bitte etwa 10 Sekunden.")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)[:40] or "datei"
        final_name = f"{category}_{counts.get(category, 0) + 1:02d}_{stamp}_{safe}{ext}"
        if category in OF_KEYS:
            parent, leaf = drive_tree.OF_COUNT[cat["folder"]].rsplit("/", 1)
            link = _drive_put(_s(m["fields"].get("Name")).strip(), leaf, src, final_name, parent)
        else:
            link = _drive_put(_s(m["fields"].get("Name")).strip(), category, src, final_name)
        _tl.append(("drive", _time.time()))
        _create(T_REFS, {"Dateiname": final_name, "Model": [m["id"]], "Kategorie": category, "Drive link": link,
                         "Breite": pr["w"] or None, "Höhe": pr["h"] or None, "Bytes": size,
                         "Uploaded at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    finally:
        if tmp:
            try:
                tmp.unlink()
            except OSError:
                pass
    counts = _counts(_refs(m["id"]))
    _tl.append(("row", _time.time()))
    out = {"ok": True, "file": final_name, "link": link, "counts": counts, "references_done": _refs_done(counts),
           "of_done": _of_done(counts)}
    _parts, _prev = [], _t0                                # 16.11 launch check: uploads felt slow, one timing line per file
    for _k, _t in _tl:
        _parts.append(f"{_k}={_t - _prev:.1f}")
        _prev = _t
    print("[upload]", category, f"{_time.time() - _t0:.1f}s", " ".join(_parts), flush=True)
    if category in OF_KEYS:
        if out["of_done"]:
            await_review(m["id"], "ofbilder")
        return out
    if out["references_done"]:
        await_review(m["id"], "bilder")
        out.update(_finish_if_complete(m["id"]))
    return out


def delete_reference(email: str, ref_id: str) -> dict:
    """Remove one of her own uploads (row + Drive file)."""
    m = _model(email)
    if not m or not re.fullmatch(r"rec[A-Za-z0-9]{14}", ref_id):
        raise ValueError("bad request")
    r = requests.get(_url(T_REFS, ref_id), headers=_hdr(), timeout=40)
    if not r.ok or m["id"] not in (r.json()["fields"].get("Model") or []):
        raise ValueError("not yours")
    f = r.json()["fields"]
    fid = re.search(r"/file/d/([^/]+)", _s(f.get("Drive link")))
    try:                                                   # 16.11: by file id, the OnlyFans pictures live in another folder
        if fid:
            drive_tree._req("DELETE", f"{drive_tree.API}/files/{fid.group(1)}", params={"supportsAllDrives": "true"})
    except Exception as e:  # noqa: BLE001
        print("[onboarding] drive delete:", e)
    requests.delete(_url(T_REFS, ref_id), headers=_hdr(), timeout=40)
    portal.bust(T_REFS)
    return {"ok": True, "counts": _counts(_refs(m["id"]))}


# ---------------------------------------------------------------- stdlib server helpers

def handle_upload(handler, email: str) -> dict:
    """POST /onboarding/upload (multipart: category, file). Returns the JSON dict to send; raises ValueError for 400s."""
    from . import course
    mp = course.parse_multipart(handler, max_bytes=MAX_VIDEO + 1024 * 1024)
    f = mp["files"].get("file")
    if not f:
        raise ValueError("Keine Datei")
    try:
        with open(f["path"], "rb") as fh:
            return save_reference(email, mp["fields"].get("category", ""), fh, f["filename"], f["size"])
    finally:
        try:
            os.unlink(f["path"])
        except OSError:
            pass


# ---------------------------------------------------------------- manager

def manager_summary(model_rec_id: str) -> dict:
    """Steckbrief status + per-category counts + Drive folder for the manager's model page."""
    sb = _steckbrief(model_rec_id)
    refs = _refs(model_rec_id)
    counts = {**_counts([]), **_drive_counts(model_rec_id)}  # 16.11: counted in her Drive folders
    m = _model_by_id(model_rec_id) or {}
    name = drive_tree._safe(_s(m.get("fields", {}).get("Name")).strip())
    folder = ""
    try:
        fid = drive_tree.ensure_folder(f"{drive_tree.ROOT}/{name}/{drive_tree.SHARED.format(name=name)}", "Referenzbilder") if name else ""
        folder = drive_tree.link(fid) if fid else ""
    except Exception:  # noqa: BLE001
        folder = ""
    return {"steckbrief": {"status": _s(sb["fields"].get("Status")) if sb else "Offen", "id": sb["id"] if sb else None,
                           "submitted": _s(sb["fields"].get("Submitted at")) if sb else "",
                           "team": list(sb["fields"].get(F_TEAM) or []) if sb else [],   # 16.11: what she left to us
                           "languages": _s(sb["fields"].get("Sprachen")) if sb else "",
                           "review": {k: _task_status(model_rec_id, k) for k in REVIEW_KEYS},
                           "airtable": f"https://airtable.com/{portal.BASE}/{T_STECKBRIEF}/{sb['id']}" if sb else ""},
            "references": {"counts": counts, "done": _refs_done(counts), "total": sum(counts.get(c["key"], 0) for c in CATEGORIES), "folder": folder,
                           "targets": {c["key"]: [c["min"], c["max"]] for c in CATEGORIES}},
            "complete": bool(sb and _s(sb["fields"].get("Status")) in ("Ausgefüllt", "Vom Team geprüft")) and _refs_done(counts)}


# ---------------------------------------------------------------- page

def _tpl(name: str) -> str:
    return Path(__file__).with_name(name).read_text(encoding="utf-8")


def _logo_uri() -> str:
    import base64
    f = portal.LOGO
    return "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode() if f.exists() else ""


def build(email: str, lang: str | None = None) -> str:
    d = state(email)
    import threading                                       # the one-time task must not hold the page
    threading.Thread(target=ensure_task, args=(d["model"]["id"],), daemon=True).start()
    d["lang"] = (lang or d["model"]["language"] or "DE").upper()
    if d["lang"] not in ("DE", "EN"):
        d["lang"] = "EN"
    return (_tpl("onboarding.html").replace("__LOGO__", _logo_uri())
            .replace("__DATA__", json.dumps(d, ensure_ascii=False).replace("</", "<\\/")))


def banner_html(st: dict) -> str:
    """Small fixed card for the model's home page while Steckbrief or reference images are still missing (15.11:
    no hard gate; she can do the plan and come back any time)."""
    sb = st.get("steckbrief", {})
    r = st.get("references", {})
    c = r.get("counts", {})
    t = r.get("targets", {})
    lang = (st.get("model", {}).get("language") or "DE").upper()
    de = lang != "EN"
    parts = []
    if not sb.get("done"):
        parts.append("Steckbrief ausfüllen" if de else "Fill in your profile")
    missing = [f"{k} {c.get(k, 0)}/{v[0]}" for k, v in t.items() if k != "Details" and c.get(k, 0) < v[0]]
    if missing:
        parts.append(("Referenzbilder: " if de else "Reference images: ") + ", ".join(missing))
    elif not r.get("done"):                                   # 16.11: counts without targets said nothing about the images
        parts.append("Referenzbilder hochladen" if de else "Upload reference images")
    if not parts:
        return ""
    title = "Deine nächsten Schritte" if de else "Your next steps"
    cta = "Jetzt erledigen" if de else "Do it now"
    return (f'<div style="max-width:1240px;margin:0 auto;padding:24px clamp(16px,4vw,48px) 0;position:relative;z-index:2"><a href="/onboarding" style="display:flex;gap:14px;'
            f'align-items:center;background:linear-gradient(135deg,rgba(255,92,154,.16),rgba(255,92,154,.06));border:1px solid rgba(255,92,154,.55);border-radius:18px;padding:16px 18px;color:#e6e8ee;text-decoration:none;'
            f'font:15px/1.4 \'Bricolage Grotesque\',system-ui,sans-serif"><span style="flex:none;width:44px;height:44px;border-radius:14px;background:#ff5c9a;display:grid;place-items:center;font-size:20px">📸</span>'
            f'<span style="flex:1;min-width:0"><b style="font-size:16px">{title}</b><br><span style="color:#c9cfdb">{" · ".join(parts)}</span></span>'
            f'<span style="flex:none;background:#ff5c9a;color:#fff;border-radius:999px;padding:10px 16px;font-weight:700;white-space:nowrap">{cta}</span></a></div>')


def request_images(model_rec_id: str, note: str, by: str = "") -> dict:
    """Manager asks for new/more reference images: a visible task for the model plus a short mail (when SMTP is set)."""
    from . import onboarding as ob
    m = _model_by_id(model_rec_id)
    if not m:
        raise ValueError("unknown model")
    f = m["fields"]
    note = (note or "").strip()[:500]
    title = "Neue Referenzbilder hochladen" + (f": {note}" if note else "")
    _create(portal.T_TASKS, {"Task": title, "Model": [model_rec_id], "Category": "Content", "Status": "Open", "Priority": "High",
                             "Assigned to": "Model", "Visible to model": True, "Due type": "This week",
                             "Description": (note + "\n\n" if note else "") + "In deinem Drive-Ordner „Referenzbilder“ in den passenden Unterordner hochladen. Scharf, aktuelle Haarfarbe, keine starken Filter.",
                             **({"Drive folder": _s(f.get("Uploads folder"))} if f.get("Uploads folder") else {})})
    email = _s(f.get("Email")).strip().lower()
    mailed = False
    if email:
        first = _s(f.get("Name")).split(" ")[0] or "du"
        mailed = ob.send_mail(email, f"Northfeed: neue Referenzbilder, {first}",
                              f"Hi {first},\n\nwir brauchen noch ein paar neue Referenzbilder von dir{(': ' + note) if note else ''}.\n\n"
                              f"Einfach in der Google-Drive-App in deinen Ordner „Referenzbilder“ hochladen. Die Aufgabe im Portal öffnet ihn dir: {ob.PORTAL_URL}\n\nDanke dir!\nDein Northfeed-Team\n")
    return {"ok": True, "mailed": mailed}
