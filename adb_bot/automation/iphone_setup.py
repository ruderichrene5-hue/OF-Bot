"""Account setup on the iPhone farm: profile picture, bio, story highlight with the OF link (Rene, 22.10).

Not bound to a warm-up day -- bound to the material. Every tick looks at each account: a picture in
`Profile Picture` and `Profile Picture done` unchecked -> upload it. A hand-written `Bio Text` and `Bio done`
unchecked -> type it (Rene 22.10: no pre-made pool bios, he writes them himself). `Story Highlight Picture` + `Funnel Link` and `Story Highlight Set`
unchecked -> story with link sticker, saved into a highlight. The bot ticks the checkbox when the step is
verified on the phone; on failure it keeps a screenshot, writes a Notes line and leaves the box empty, so
the next tick tries again. Two guards: warm-up day 1 must have run (or the account is Healthy), and one
profile edit per account per day (IPHONE_SETUP_MULTI=1 lifts that). Window 09-23 Berlin.

    .venv/bin/python -m adb_bot.automation.iphone_setup due
    .venv/bin/python -m adb_bot.automation.iphone_setup run <device> <container> --step picture|bio|highlight [--row rec..]
    .venv/bin/python -m adb_bot.automation.iphone_setup tick [--max-phones 4]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import sys
import tempfile
import threading
import time

import requests

from adb_bot.automation import iphone_diag, iphone_farm_config
from adb_bot.automation import iphone_phone_lock as phone_lock
from adb_bot.automation.iphone_farm_wda import Phone, resolve
from adb_bot.automation.iphone_ig import (create_story, media_to_photos, save_story_to_highlight, set_bio,
                                          set_profile_picture)
from adb_bot.automation.iphone_warmup import (BASE, BERLIN, BLOCKED_STATUS, F_DONE, F_NOTES, F_STATUS, F_USER, T_ACC,
                                              T_CONT, WARMUP_STATUS, WINDOW, _get, _hdr, _letter, _list, _note, _patch)

log = logging.getLogger("iphone_setup")
F_PIC, F_PIC_DONE, F_PIC_SET = "Profile Picture", "Profile Picture done", "Profile Picture Set"
F_BIO, F_BIO_DONE = "Bio Text", "Bio done"           # 22.10 Rene: hand-written text on the row, not the Bio POOL link
F_HL_PIC, F_HL_SET, F_LINK, F_LINK_TEXT = "Story Highlight Picture", "Story Highlight Set", "Funnel Link", "Story Link text"
F_LINK_DONE, F_FUNNEL_DONE = "Link gesetzt (Story/Highlight)", "Story funnel done"
F_PIC_USED, F_HL_USED = "Profile Picture used", "Story Highlight Picture used"      # 22.10 Rene: which picture went up
F_TRACK = "Tracking Link (auto)"                                                      # lookup from 'Link in Bio 2' -- the OF funnel link


def funnel_link(f: dict) -> str:
    """`Funnel Link` on the row, else the account's assigned tracking link (Link in Bio 2 -> URL)."""
    if (f.get(F_LINK) or "").strip():
        return f[F_LINK].strip()
    t = f.get(F_TRACK) or []
    return (t[0] if isinstance(t, list) and t else str(t or "")).strip()
T_GEN = "tblJSsv7TvCinqcTn"                                                           # Generate Pictures
GEN_KINDS = {"picture": ("Profilbild", "Profile Picture"), "highlight": ("Story",)}
HIGHLIGHT_NAME = os.environ.get("IPHONE_HIGHLIGHT_NAME", "Links")
MULTI = os.environ.get("IPHONE_SETUP_MULTI") == "1"
STATE_PATH = os.path.expanduser("~/.adb_bot/iphone_setup_state.json")
# 23.10 Rene (voice): don't wait for hand-written texts -- "schau mal da unten"-style bio, and the story link sticker
# always with the linked funnel link and a "schreib mir"-style text. Empty field -> one of these (picked per row,
# stable), written back into the field so the row shows what went on the phone. A filled field always wins.
BIO_DEFAULTS = ["schau mal da unten 👇", "mehr von mir gibt's da unten 👇", "alles weitere findest du unten ⬇️",
                "da unten gibt's mehr von mir 😏", "unten wartet noch mehr auf dich 👀"]
LINK_TEXT_DEFAULTS = ["schreib mir hier 💌", "hier schreibst du mir 💌", "schreib mir 💬", "hier geht's zu mir 👀",
                      "melde dich hier bei mir 💌"]


def _pick(pool: list[str], row_id: str) -> str:
    return pool[sum(row_id.encode()) % len(pool)]


def bio_text(f: dict, row_id: str = "") -> str:
    return (f.get(F_BIO) or "").strip() or _pick(BIO_DEFAULTS, row_id)


def link_text(f: dict, row_id: str = "") -> str:
    return (f.get(F_LINK_TEXT) or "").strip() or _pick(LINK_TEXT_DEFAULTS, row_id)


F_REPLACE = "Account ersetzen"                                                        # 23.10 Rene: "mach da einen neuen Account"
F_MODEL_LINK, F_CREATION, F_IGPW, F_EMAIL = "Model auswählen (Pflicht)", "Creation Status", "IG Password", "Email"
RESET_ON_REPLACE = {F_USER: "", F_IGPW: "", F_CREATION: "To Do", F_STATUS: "setup", F_REPLACE: False,
                    "Account created (tick when done)": False, F_PIC_DONE: False, F_PIC_SET: False, F_BIO_DONE: False,
                    F_HL_SET: False, F_LINK_DONE: False, F_FUNNEL_DONE: False, "Warm-up Start": None,
                    "Warm-up Day 1 done": None, "Warm-up Day 2 done": None, "Warm-up Day 3 done": None, "Warm-up Day 4 done": None,
                    F_PIC_USED: "", F_HL_USED: ""}
# 23.10 Rene: pictures are ordered by the bot itself -- profile picture = the model as she is, story/highlight =
# "Zunge raus oder so, ein bisschen mehr in die Richtung". Prompts and pose photos come from the model's own
# finished rows of the same Verwendung (what Rene already approved), picked at random.
GEN_ORDER = {"picture": "Profilbild", "highlight": "Story"}
MAX_ORDERS_PER_TICK = 6                                                               # ~18 pictures/h for the pipeline
GEN_FIELDS = {"model": "Model (required)", "account": "Für Account (optional)", "use": "Verwendung",
              "pose": "Pose Picture (required, mehrere = ein Job pro Bild)", "prompt": "Outfit & Scene (optional)",
              "pick": "Pick (1-4)", "go": "Generate (tick to start)", "status": "Status (auto)", "picked": "Picked Picture (auto)"}
STEPS = ("picture", "bio", "highlight")


# ---------------------------------------------------------------- who needs what

def _state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _mark_state(row: str) -> None:
    s = _state()
    s[row] = dt.datetime.now(BERLIN).strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)


def missing_for_highlight(f: dict, row_id: str = "") -> list[str]:
    """What the highlight step still lacks on the row (for `due` and the flow check)."""
    if f.get(F_HL_SET):
        return []
    pic = f.get(F_HL_PIC) or (row_id and generated_pictures(row_id).get("highlight"))
    return [n for n, v in (("Story picture (row or Generate Pictures)", pic), ("Funnel Link / Tracking Link", funnel_link(f))) if not v]


_GEN_INDEX: dict | None = None
_GEN_LOADED = 0.0


def _gen_index() -> dict:
    """{account row id: [Generate Pictures record, newest first]} -- one query per 10 min. (A FIND() on the linked
    field in a formula sees display names, not record ids, so the match is done here on the API's id lists.)"""
    global _GEN_INDEX, _GEN_LOADED
    if _GEN_INDEX is not None and time.time() - _GEN_LOADED < 600:
        return _GEN_INDEX
    idx: dict = {}
    try:
        params = {"filterByFormula": "{Für Account (optional)} != ''",
                  "sort[0][field]": "Bild (auto, leer lassen)", "sort[0][direction]": "desc", "pageSize": 100}
        offset = None
        while True:
            if offset:
                params["offset"] = offset
            r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_GEN}", headers=_hdr(), params=params, timeout=30)
            r.raise_for_status()
            j = r.json()
            for rec in j.get("records", []):
                for acc in rec["fields"].get("Für Account (optional)", []):
                    idx.setdefault(acc, []).append(rec)
            offset = j.get("offset")
            if not offset:
                break
        _GEN_INDEX, _GEN_LOADED = idx, time.time()
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Generate Pictures index failed: %s", exc)
        _GEN_INDEX = _GEN_INDEX or {}
    return _GEN_INDEX


def generated_pictures(row_id: str) -> dict:
    """{'picture': (attachment, label) | None, 'highlight': ...} from Generate Pictures rows linked to this account
    ('Für Account (optional)') with the matching 'Verwendung' and a picked picture. Newest row wins. 22.10 Rene:
    the bot picks the picture itself and writes down which one it used."""
    out = {"picture": None, "highlight": None}
    for rec in _gen_index().get(row_id, []):
        f = rec["fields"]
        use = f.get("Verwendung") or ""
        if not f.get("Picked Picture (auto)"):
            continue                                          # ordered, not finished yet
        for kind, names in GEN_KINDS.items():
            if use in names and out[kind] is None:
                att = f["Picked Picture (auto)"][0]
                out[kind] = (att, f"{att.get('filename')} from Generate Pictures {rec['id']} ({(f.get('Bild (auto, leer lassen)') or '')[:40]})")
    return out


_TEMPLATES: dict = {}


def _templates(model_id: str, use: str) -> list[dict]:
    """Finished Generate Pictures rows of this model and Verwendung -- the pool of approved prompts + pose photos."""
    key = (model_id, use)
    if key not in _TEMPLATES:
        try:
            r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_GEN}", headers=_hdr(), timeout=30,
                             params={"filterByFormula": f"AND({{{GEN_FIELDS['use']}}}='{use}', {{{GEN_FIELDS['picked']}}} != '', {{{GEN_FIELDS['pose']}}} != '')",
                                     "pageSize": 100})
            r.raise_for_status()
            _TEMPLATES[key] = [x["fields"] for x in r.json().get("records", []) if model_id in (x["fields"].get(GEN_FIELDS["model"]) or [])]
        except Exception as exc:                              # noqa: BLE001
            log.warning("templates %s/%s: %s", model_id, use, exc)
            _TEMPLATES[key] = []
    return _TEMPLATES[key]


_FREE: dict | None = None


def _free_rows(refresh: bool = False) -> dict:
    """{(model id, Verwendung): [finished Generate rows linked to NO account, newest first]} -- the stock.
    23.10 Rene: "wenn bestehende frei sind, ziehst du die einfach rüber" -- a free finished picture is claimed
    before anything new is ordered. Unlinked rows still generating count as pending stock."""
    global _FREE
    if _FREE is not None and not refresh:
        return _FREE
    free: dict = {"done": {}, "pending": {}}
    try:
        params = {"filterByFormula": f"AND({{{GEN_FIELDS['account']}}} = '', {{{GEN_FIELDS['use']}}} != '')",
                  "sort[0][field]": "Bild (auto, leer lassen)", "sort[0][direction]": "desc", "pageSize": 100}
        offset = None
        while True:
            if offset:
                params["offset"] = offset
            r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_GEN}", headers=_hdr(), params=params, timeout=30)
            r.raise_for_status()
            j = r.json()
            for rec in j.get("records", []):
                f = rec["fields"]
                for m in f.get(GEN_FIELDS["model"]) or []:
                    key = (m, f.get(GEN_FIELDS["use"]))
                    bucket = "done" if f.get(GEN_FIELDS["picked"]) else ("pending" if f.get(GEN_FIELDS["go"]) and (f.get(GEN_FIELDS["status"]) or "") not in ("Failed", "Error") else None)
                    if bucket:
                        free[bucket].setdefault(key, []).append(rec)
            offset = j.get("offset")
            if not offset:
                break
    except Exception as exc:                                  # noqa: BLE001
        log.warning("free pictures index failed: %s", exc)
    _FREE = free
    return free


def claim_picture(row_id: str, f: dict, kind: str) -> str | None:
    """Link the newest free finished picture of the right kind to this account. Returns the Generate row id."""
    model = (f.get(F_MODEL_LINK) or [None])[0]
    use = GEN_ORDER[kind]
    pool = _free_rows()["done"].get((model, use), [])
    if not model or not pool:
        return None
    rec = pool.pop(0)
    r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{T_GEN}/{rec['id']}", headers=_hdr(),
                       json={"fields": {GEN_FIELDS["account"]: [row_id]}, "typecast": True}, timeout=30)
    if r.status_code >= 300:
        log.warning("%s: claim of %s failed: %s", f.get(F_USER), rec["id"], r.text[:120])
        return None
    rec["fields"][GEN_FIELDS["account"]] = [row_id]
    _gen_index().setdefault(row_id, []).insert(0, rec)
    _note(row_id, f"took the free {use} picture {rec['id']} from Generate Pictures")
    return rec["id"]


STOCK_MIN = 4                                                                         # free finished+pending pictures per model and kind


def restock(models: set, budget: int) -> list[str]:
    """Keep STOCK_MIN free pictures per (model, kind) in Generate Pictures: order unlinked rows from the
    model's approved templates until free done + pending reaches the minimum. Returns what was ordered."""
    ordered = []
    free = _free_rows()
    for model in models:
        for kind, use in GEN_ORDER.items():
            have = len(free["done"].get((model, use), [])) + len(free["pending"].get((model, use), []))
            for _ in range(max(0, STOCK_MIN - have)):
                if budget <= 0:
                    return ordered
                pool = _templates(model, use)
                if not pool:
                    break
                t = random.choice(pool)
                fields = {GEN_FIELDS["model"]: [model], GEN_FIELDS["use"]: use,
                          GEN_FIELDS["pose"]: [{"url": a["url"], "filename": a.get("filename", "pose.jpg")} for a in t[GEN_FIELDS["pose"]][:1]],
                          GEN_FIELDS["prompt"]: t.get(GEN_FIELDS["prompt"]) or "", GEN_FIELDS["pick"]: "auto (best face match)",
                          GEN_FIELDS["go"]: True}
                r = requests.post(f"https://api.airtable.com/v0/{BASE}/{T_GEN}", headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=30)
                if r.status_code < 300:
                    free["pending"].setdefault((model, use), []).append(r.json())
                    ordered.append(f"stock {use} for {model} -> {r.json()['id']}")
                    budget -= 1
                else:
                    log.warning("restock %s/%s failed: %s", model, use, r.text[:120])
                    break
    return ordered


def pending_order(row_id: str, use: str) -> bool:
    return any((rec["fields"].get("Verwendung") or "") == use for rec in _gen_index().get(row_id, []))


def order_pictures(row_id: str, f: dict, dry: bool = False) -> list[str]:
    """Create Generate Pictures rows for what this account lacks (profile picture, story picture). One order per
    kind -- an existing row for the account and Verwendung (finished or not) means nothing is ordered."""
    ordered = []
    model = (f.get(F_MODEL_LINK) or [None])[0]
    if not model:
        return ordered
    for kind, use in GEN_ORDER.items():
        done = f.get(F_PIC_DONE) if kind == "picture" else f.get(F_HL_SET)
        if done or material(f, row_id, kind) or pending_order(row_id, use):
            continue
        if not dry:
            got = claim_picture(row_id, f, kind)              # a free finished picture first
            if got:
                ordered.append(f"{use} <- free {got}")
                continue
        elif _free_rows()["done"].get((model, use)):
            ordered.append(f"{use} (dry) <- free {_free_rows()['done'][(model, use)][0]['id']}")
            continue
        pool = _templates(model, use)
        if not pool:
            log.info("%s: no finished %s template for this model -- cannot order", f.get(F_USER), use)
            continue
        t = random.choice(pool)
        fields = {GEN_FIELDS["model"]: [model], GEN_FIELDS["account"]: [row_id], GEN_FIELDS["use"]: use,
                  GEN_FIELDS["pose"]: [{"url": a["url"], "filename": a.get("filename", "pose.jpg")} for a in t[GEN_FIELDS["pose"]][:1]],
                  GEN_FIELDS["prompt"]: t.get(GEN_FIELDS["prompt"]) or "", GEN_FIELDS["pick"]: "auto (best face match)",
                  GEN_FIELDS["go"]: True}
        if dry:
            ordered.append(f"{use} (dry) <- {(t.get(GEN_FIELDS['prompt']) or '')[:50]}")
            continue
        r = requests.post(f"https://api.airtable.com/v0/{BASE}/{T_GEN}", headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=30)
        if r.status_code < 300:
            rec = r.json()
            _gen_index().setdefault(row_id, []).insert(0, rec)
            ordered.append(f"{use} -> {rec['id']}")
            _note(row_id, f"ordered a {use} picture in Generate Pictures ({rec['id']}): {(t.get(GEN_FIELDS['prompt']) or '')[:80]}")
        else:
            log.warning("%s: order %s failed: %s %s", f.get(F_USER), use, r.status_code, r.text[:160])
    return ordered


def replace_account(device: str, container: str, row: str, f: dict, log_fn=None) -> dict:
    """Rene 23.10: wipe the Instagram container on the phone (cranectl --wipe = fresh app, nothing logged in),
    archive the old login into Notes, reset the row to a signup To Do on the same container and model."""
    say = log_fn or (lambda m: log.info("%s/%s: %s", _letter(device), container, m))
    from adb_bot.automation.iphone_ig import _phone_ssh
    letter = _letter(device)
    hold = phone_lock.hold(letter, timeout_s=600)
    if hold is None:
        return {"ok": False, "error": f"phone {letter} busy"}
    try:
        out = _phone_ssh(device, "/var/jb/var/mobile/bin/ig-close.sh >/dev/null 2>&1; "
                                 f"/var/jb/usr/local/bin/cranectl --wipe com.burbn.instagram 'name:{container} IG' 2>&1; "
                                 "/var/jb/usr/local/bin/cranectl --list com.burbn.instagram")
        if f"{container} IG" not in out:
            raise RuntimeError(f"container '{container} IG' not listed after the wipe: {out[:200]}")
        say(f"container {container} wiped")
        old = f"replaced {dt.datetime.now(BERLIN).strftime('%d.%m.%Y %H:%M')}: was @{f.get(F_USER, '')} / pw {f.get(F_IGPW, '')} / {f.get(F_EMAIL, '')} (status {f.get(F_STATUS, '-')})"
        _note(row, old)
        _patch(row, RESET_ON_REPLACE)
        say("row reset to 'To Do' -- plan the new signup in Tagesplan")
        iphone_diag.notify(f"{letter}/{container}: account @{f.get(F_USER, '?')} replaced -- container wiped, row reset to To Do. Plan the signup in Tagesplan.")
        return {"ok": True}
    except Exception as exc:                                  # noqa: BLE001
        say(f"replace FAILED: {exc}")
        _note(row, f"replace failed: {str(exc)[:300]}")
        iphone_diag.notify(f"{letter}/{container}: replace of @{f.get(F_USER, '?')} failed: {str(exc)[:200]}")
        return {"ok": False, "error": str(exc)}
    finally:
        hold.release()


_REUSE: dict = {}
DRIVE_PICTURES = {"Mandy": "veralta:MODELS/Mandy OF/Bilder/2026-10", "Kathi": "veralta:MODELS/Kathi/Bilder",
                  "Lea": "veralta:MODELS/Lea OF/Bilder"}
MODEL_NAMES = {"rec3wB3RZZTP5V0Zn": "Nikki", "recKizuQjcTQ6ePXM": "Kathi", "recikj1PZtZSNrpJv": "Lea",
               "recXuODWU9FETE6LD": "Luisa", "recXqY6RHUqQ0R8d1": "Mandy"}


def reuse_picture(f: dict, row_id: str, kind: str):
    """24.10 Rene: "Bilder können doppelt verwendet werden" -- with no own/free picture, take an already finished
    Generate Pictures image of the same model and kind (any account's), picked stably per row; a model with none
    at all (Mandy) takes a photo from her Drive picture folder. Never blocks on RunPod credit."""
    model = (f.get(F_MODEL_LINK) or [None])[0]
    if not model:
        return None
    use_names = GEN_KINDS[kind]
    key = (model, kind)
    if key not in _REUSE:
        pool = []
        try:
            offset = None
            while True:
                params = {"filterByFormula": f"{{{GEN_FIELDS['picked']}}} != ''", "pageSize": 100}
                if offset:
                    params["offset"] = offset
                r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_GEN}", headers=_hdr(), params=params, timeout=30)
                r.raise_for_status()
                j = r.json()
                for x in j.get("records", []):
                    xf = x["fields"]
                    if model in (xf.get(GEN_FIELDS["model"]) or []) and (xf.get("Verwendung") or "") in use_names:
                        att = xf[GEN_FIELDS["picked"]][0]
                        pool.append((att, f"{att.get('filename')} (reused from Generate Pictures {x['id']})"))
                offset = j.get("offset")
                if not offset:
                    break
        except Exception as exc:                              # noqa: BLE001
            log.warning("reuse pool %s/%s failed: %s", model, kind, exc)
        if not pool and MODEL_NAMES.get(model) in DRIVE_PICTURES:
            src = DRIVE_PICTURES[MODEL_NAMES[model]]
            import subprocess
            names = subprocess.run(["rclone", "lsf", "--files-only", src], capture_output=True, text=True, timeout=120).stdout.split()
            pool = [({"drive": f"{src}/{n}", "filename": n}, f"{n} (Drive {src.split(':', 1)[-1]})")
                    for n in names if n.lower().endswith((".jpg", ".jpeg", ".png", ".heic"))]
        _REUSE[key] = pool
    pool = _REUSE[key]
    if not pool:
        return None
    return pool[(sum(row_id.encode()) + (7 if kind == "highlight" else 0)) % len(pool)]


def material(f: dict, row_id: str, kind: str):
    """(attachment, label) for the picture step: the row's own attachment first, else a generated picture,
    else (24.10) a reused one."""
    field = F_PIC if kind == "picture" else F_HL_PIC
    if f.get(field):
        return f[field][0], f"{f[field][0].get('filename')} (attachment on the row)"
    return generated_pictures(row_id).get(kind) or reuse_picture(f, row_id, kind)


def needed(f: dict, row_id: str = "") -> list[str]:
    """The setup steps this row still needs, in order, judged by material present + checkbox empty."""
    out = []
    if not f.get(F_PIC_DONE) and material(f, row_id, "picture"):
        out.append("picture")
    if not f.get(F_BIO_DONE):                                  # 23.10: empty Bio Text -> default text (bio_text)
        out.append("bio")
    # 22.10 Rene: the link sticker ALWAYS carries a text; 23.10: empty `Story Link text` -> default text (link_text)
    if not f.get(F_HL_SET) and funnel_link(f) and material(f, row_id, "highlight"):
        out.append("highlight")
    return out


def next_step(f: dict, row: str) -> tuple[str | None, str]:
    """(step to run now, reason)."""
    status = f.get(F_STATUS) or ""
    steps = needed(f, row)
    if not steps:
        return None, "nothing to do"
    if status in BLOCKED_STATUS:
        return None, f"status {status!r}"
    if not (f.get(F_USER) or "").strip():
        return None, "no username on the row"
    m = WARMUP_STATUS.search(status)
    warmed = bool(f.get(F_DONE[1])) or (m and int(m.group(1)) >= 2) or status == "Healthy"
    if not warmed:
        # 22.10 Rene ("Ja"): the bio may go in before warm-up day 1 (Kathi accounts on G/H); picture and highlight
        # still wait for day 1
        # 23.10 Rene: profile picture + bio must be READY before the warm-up day; only the highlight waits
        early = [x for x in steps if x in ("picture", "bio")]
        if early:
            steps = early
        else:
            return None, f"warm-up day 1 not run yet (status {status!r}) -- needs {', '.join(steps)}"
    today = dt.datetime.now(BERLIN).strftime("%Y-%m-%d")
    if not MULTI and _state().get(row) == today:
        return None, f"already one profile edit today -- {', '.join(steps)} tomorrow"
    return steps[0], f"needs {', '.join(steps)}"


def candidates() -> list[dict]:
    rows = _list(T_ACC, f"AND({{Container}} != '', {{{F_USER}}} != '', OR({{{F_PIC_DONE}}} = 0, {{{F_BIO_DONE}}} = 0, {{{F_HL_SET}}} = 0))")
    out = []
    for row in rows:
        f = row["fields"]
        if (f.get(F_STATUS) or "") in BLOCKED_STATUS:
            continue
        lacking = missing_for_highlight(f, row["id"])
        if not needed(f, row["id"]):
            if lacking:                                       # nothing runnable: say what is missing (the tick orders pictures)
                cont_id = (f.get("Container") or [None])[0]
                cont = _get(T_CONT, cont_id)["fields"] if cont_id else {}
                out.append({"row": row["id"], "fields": f, "username": (f.get(F_USER) or "").lstrip("@"), "container": cont.get("Container"),
                            "device": (cont.get("Device") or [None])[0], "step": None, "status": f.get(F_STATUS),
                            "why": "highlight waits for: " + ", ".join(lacking)})
            elif not f.get(F_PIC_DONE):
                out.append({"row": row["id"], "fields": f, "username": (f.get(F_USER) or "").lstrip("@"), "container": None,
                            "device": None, "step": None, "status": f.get(F_STATUS), "why": "profile picture: no material yet"})
            continue
        step, why = next_step(f, row["id"])
        cont_id = (f.get("Container") or [None])[0]
        try:
            cont = _get(T_CONT, cont_id)["fields"] if cont_id else {}
        except requests.RequestException:
            cont = {}
        out.append({"row": row["id"], "fields": f, "username": (f.get(F_USER) or "").lstrip("@"), "container": cont.get("Container"),
                    "device": (cont.get("Device") or [None])[0], "step": step, "why": why, "status": f.get(F_STATUS)})
    return out


# ---------------------------------------------------------------- the three steps

def _download(att: dict, workdir: str) -> str:
    name = att.get("filename") or "media.jpg"
    path = os.path.join(workdir, name.replace("/", "_"))
    if att.get("drive"):                                      # 24.10: a photo from the model's Drive folder
        import subprocess
        subprocess.run(["rclone", "copyto", att["drive"], path], check=True, timeout=300)
        if path.lower().endswith(".heic"):
            subprocess.run(["convert", path, path[:-5] + ".jpg"], check=True, timeout=120)
            path = path[:-5] + ".jpg"
        return path
    r = requests.get(att["url"], timeout=120)
    r.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(r.content)
    return path


def step_picture(p: Phone, device: str, f: dict, say, row: str = "") -> dict:
    att, label = material(f, row, "picture")
    say(f"profile picture: {label}")
    with tempfile.TemporaryDirectory() as wd:
        path = _download(att, wd)
        media_to_photos(p, device, path, log=say)
    res = set_profile_picture(p, 0, grant_newest=True, log=say)
    if not res.get("ok"):
        raise RuntimeError(f"profile picture upload did not finish ({res})")
    return {F_PIC_DONE: True, F_PIC_SET: True, F_PIC_USED: label[:250]}


def step_bio(p: Phone, device: str, f: dict, say, row: str = "") -> dict:
    text = bio_text(f, row)
    default = not (f.get(F_BIO) or "").strip()
    if len(text) > 150:
        raise RuntimeError(f"Bio Text has {len(text)} chars, Instagram allows 150 -- shorten it in Airtable")
    res = set_bio(p, text, log=say, keep_existing=default)        # a default text never overwrites a bio already on the phone
    if not res.get("ok"):
        raise RuntimeError(f"bio read back differs: {res}")
    return {F_BIO_DONE: True, F_BIO: res["after"].strip() if res.get("kept") else text}


def step_highlight(p: Phone, device: str, f: dict, say, row: str = "") -> dict:
    att, label = material(f, row, "highlight")
    say(f"highlight picture: {label}")
    with tempfile.TemporaryDirectory() as wd:
        path = _download(att, wd)
        media_to_photos(p, device, path, log=say)
    link, text = funnel_link(f), link_text(f, row)
    if not link:
        raise RuntimeError("no Funnel Link and no Tracking Link on the row")
    story = create_story(p, grant_newest=1, log=say, link=link, link_text=text)
    if not story.get("shared"):
        raise RuntimeError(f"story not shared ({story})")
    hl = save_story_to_highlight(p, HIGHLIGHT_NAME, log=say)
    if not hl.get("ok"):
        raise RuntimeError(f"story shared with link, but highlight not confirmed ({hl}) -- check the profile")
    return {F_HL_SET: True, F_LINK_DONE: True, F_FUNNEL_DONE: True, F_HL_USED: label[:250], F_LINK_TEXT: text}


STEP_FN = {"picture": step_picture, "bio": step_bio, "highlight": step_highlight}


# ---------------------------------------------------------------- one run

def run_step(device: str, container: str, step: str, row: str | None = None, fields: dict | None = None,
             expect: str | None = None, log_fn=None) -> dict:
    say = log_fn or (lambda m: log.info("%s/%s: %s", _letter(device), container, m))
    if fields is None:
        if not row:
            raise ValueError("fields or row needed")
        fields = _get(T_ACC, row)["fields"]
    rec_id, _ = resolve(device)
    letter = _letter(device)
    if phone_lock.signup_wanted(letter):                        # signup first (Rene 22.10), see iphone_warmup.run_one
        return {"ok": False, "step": step, "error": f"phone {letter}: a signup is waiting for it -- next tick"}
    phone_lock.want(letter, "setup", kind="setup")            # posting pauses at its next post boundary for us
    hold = phone_lock.hold(letter, timeout_s=600)
    if hold is None:
        phone_lock.unwant(letter, kind="setup")
        return {"ok": False, "step": step, "error": f"phone {letter} busy"}
    p: Phone | None = None
    try:
        iphone_farm_config._set_off(rec_id, True)
        from adb_bot.automation.iphone_ig import open_account
        p = open_account(device, container, expect, log=say)
        say(f"setup step '{step}' starting")
        boxes = STEP_FN[step](p, device, fields, say, row or "")
        if row:
            _patch(row, boxes)
            _note(row, f"setup {step} ok")
            _mark_state(row)
        say(f"setup step '{step}' done -> {list(boxes)}")
        return {"ok": True, "step": step, "set": list(boxes)}
    except Exception as exc:                                  # noqa: BLE001
        shot = iphone_diag.keep_screen(device, f"setup-{step}", f"{type(exc).__name__}: {exc}", container=container, wda=p, log_fn=say)
        say(f"setup step '{step}' FAILED: {exc} (screen: {shot or 'none'})")
        if row:
            _note(row, f"setup {step} failed: {str(exc)[:300]} | screen {shot or '-'}")
            # a half-done edit still counts as today's edit -- but a step that never reached the editor did not edit
            # anything (24.10 B6: "bio editor did not open" parked two fresh accounts for the whole day)
            if not any(k in str(exc) for k in ("did not open", "not found", "busy", "not ready", "no Next")):
                _mark_state(row)
            if "logged out" in str(exc):
                _patch(row, {F_STATUS: "logged out"})
            elif "checkpoint" in str(exc):
                _patch(row, {F_STATUS: "Human Verification"})
        iphone_diag.notify(f"setup {step} on {letter}/{container} (@{expect or '?'}) failed: {str(exc)[:200]}\n{shot or ''}")
        return {"ok": False, "step": step, "error": str(exc), "screen": shot}
    finally:
        if p is not None:
            from adb_bot.automation.iphone_ig import finish_phone
            finish_phone(p, say)                              # Instagram closed, screen locked (Rene 22.10)
        try:
            iphone_farm_config._set_off(rec_id, False)
        except Exception:                                     # noqa: BLE001
            pass
        hold.release()
        phone_lock.unwant(letter, kind="setup")


def tick(max_phones: int = 4) -> dict:
    now = dt.datetime.now(BERLIN)
    if not WINDOW[0] <= now.hour < WINDOW[1]:
        log.info("outside %02d-%02d Berlin", *WINDOW)
        return {"skipped": "window"}
    # 23.10: "Account ersetzen" ticks go first, then picture orders for everyone who lacks them
    try:
        for row in _list(T_ACC, f"AND({{{F_REPLACE}}} = 1, {{Container}} != '')"):
            f = row["fields"]
            cont_id = (f.get("Container") or [None])[0]
            cont = _get(T_CONT, cont_id)["fields"] if cont_id else {}
            device, name = (cont.get("Device") or [None])[0], cont.get("Container")
            if device and name:
                log.info("replace: %s @%s", name, f.get(F_USER))
                replace_account(device, name, row["id"], f)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("replace scan failed: %s", exc)
    try:                                                      # 24.10 Rene: "mach alles selbst" -- new accounts start warm-up by themselves
        today = now.strftime("%Y-%m-%d")
        for row in _list(T_ACC, "AND({Creation Status} = 'Instagram Created', {Warm-up Start} = '', {Container} != '', {Username} != '')"):
            st = row["fields"].get(F_STATUS) or ""
            if st in BLOCKED_STATUS or st == "Healthy":
                continue
            fields = {"Warm-up Start": today}
            if not st:
                fields[F_STATUS] = "setup"
            _patch(row["id"], fields)
            log.info("@%s: warm-up start set (new account)", row["fields"].get(F_USER))
    except Exception as exc:                                  # noqa: BLE001
        log.warning("auto warm-up start failed: %s", exc)
    cands = candidates()
    orders = 0
    for c in cands:
        if orders >= MAX_ORDERS_PER_TICK:
            break                                             # the rest next tick -- do not flood the RunPod pipeline
        if c["status"] not in BLOCKED_STATUS and c["username"]:
            try:
                o = order_pictures(c["row"], c["fields"])
                if o:
                    orders += len(o)
                    log.info("%s @%s: ordered %s", c["container"], c["username"], o)
            except Exception as exc:                          # noqa: BLE001
                log.warning("%s: picture order failed: %s", c["container"], exc)
    try:                                                      # 23.10 Rene: "eine Regel, dass wir immer Bilder zur Verfügung haben"
        models = {(c["fields"].get(F_MODEL_LINK) or [None])[0] for c in cands if c["status"] not in BLOCKED_STATUS} - {None}
        o = restock(models, MAX_ORDERS_PER_TICK - orders)
        if o:
            log.info("restock: %s", o)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("restock failed: %s", exc)
    due = [c for c in cands if c["step"] and c["device"] and c["container"]]
    random.shuffle(due)
    by_phone: dict[str, dict] = {}
    for c in due:
        by_phone.setdefault(c["device"], c)
    picked = list(by_phone.values())[:max_phones]
    if not picked:
        log.info("nothing due")
        return {"ran": []}
    results: dict[str, dict] = {}

    def work(c: dict) -> None:
        time.sleep(random.uniform(0, 60))
        results[c["container"]] = run_step(c["device"], c["container"], c["step"], row=c["row"], fields=c["fields"],
                                           expect=c["username"] or None)

    ths = [threading.Thread(target=work, args=(c,), name=c["container"]) for c in picked]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return {"ran": [{"container": k, **{x: v.get(x) for x in ("ok", "step", "error")}} for k, v in results.items()]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("due")
    s = sub.add_parser("run")
    s.add_argument("device"); s.add_argument("container")
    s.add_argument("--step", required=True, choices=STEPS)
    s.add_argument("--row", help="Accounts SOP record id (fields + checkboxes come from / go to it)")
    s.add_argument("--expect")
    s = sub.add_parser("tick")
    s.add_argument("--max-phones", type=int, default=4)
    s = sub.add_parser("order", help="order missing profile/story pictures in Generate Pictures")
    s.add_argument("--username", help="only this account (default: everyone who lacks something)")
    s.add_argument("--dry", action="store_true")
    s = sub.add_parser("replace", help="wipe the container and reset the row to a signup To Do")
    s.add_argument("device"); s.add_argument("container"); s.add_argument("--row", required=True)
    a = ap.parse_args(argv)
    os.makedirs("/opt/adbbot/logs", exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("/opt/adbbot/logs/iphone_setup.log")])
    if a.cmd == "due":
        for c in candidates():
            mark = f"DUE {c['step']}" if c["step"] else "wait"
            print(f"{mark:14} {_letter(c['device'] or '')}/{c['container'] or '?':4} @{c['username'] or '?':22} "
                  f"status {c['status'] or '-':18} {c['why']}")
        return 0
    if a.cmd == "run":
        res = run_step(a.device, a.container, a.step, row=a.row, expect=a.expect)
        print(res)
        return 0 if res.get("ok") else 1
    if a.cmd == "order":
        for c in candidates():
            if a.username and c["username"].lower() != a.username.lower().lstrip("@"):
                continue
            if c["status"] in BLOCKED_STATUS or not c["username"]:
                continue
            o = order_pictures(c["row"], c["fields"], dry=a.dry)
            if o:
                print(f"{c['container'] or '?':4} @{c['username']:22} {o}")
        return 0
    if a.cmd == "replace":
        f = _get(T_ACC, a.row)["fields"]
        print(replace_account(a.device, a.container, a.row, f))
        return 0
    res = tick(a.max_phones)
    log.info("tick: %s", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
