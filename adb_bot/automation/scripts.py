"""OF scripts (16.11 Rene): PPV ladders, stage by stage more intimate. One Airtable row per script (table Skripte),
the stages live as JSON in "Stufen" so the manager edits a whole script in one go:

    [{"n": 1, "title": {"de": "Teaser", "en": "Teaser"}, "photos": "8", "videos": 0,
      "outfit": {"de": "...", "en": "..."}, "poses": {"de": "...", "en": "..."},
      "clips": [{"title": {"de": "...", "en": "..."}, "text": {"de": "...", "en": "..."}}], "price": null}, ...]

Models link scripts via Models."Skripte". The model sees every text in her language (Models.Language, DE/EN; a missing
translation falls back to the other language); the price is team-only. Drive folders per stage
(OF-Content/Skript N – Name/Stufe N – Titel/{Bilder,Videos}) are created by ensure_folders once the Northfeed
Drive is connected; the files are big, so the model uploads with the Drive app, never through the portal.
"""

from __future__ import annotations

import html
import json

from . import portal

T_SCRIPTS = "tblwFrlRODkJq0sVE"
F_LINK = "Skripte"

WORDS = {
    "de": {"root": "OF-Content", "script": "Skript", "stage": "Stufe", "photos": "Bilder", "videos": "Videos"},
    "en": {"root": "OF Content", "script": "Script", "stage": "Stage", "photos": "Photos", "videos": "Videos"},
}


def _lang(v) -> str:
    return "en" if str(v or "").strip().lower().startswith("en") else "de"


def _t(d, lang: str) -> str:
    """Pick the model's language, fall back to the other one."""
    if isinstance(d, str):
        return d
    d = d or {}
    return str(d.get(lang) or d.get("en" if lang == "de" else "de") or "")


def _stages(f: dict) -> list:
    try:
        v = json.loads(f.get("Stufen") or "[]")
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _place(f: dict) -> dict:
    """16.11 Rene: where the script is shot (bed, living room ...), one line per language."""
    try:
        v = json.loads(f.get("Ort") or "{}")
        return {"de": str(v.get("de") or ""), "en": str(v.get("en") or "")} if isinstance(v, dict) else {"de": str(v), "en": ""}
    except Exception:  # noqa: BLE001
        return {"de": str(f.get("Ort") or ""), "en": ""}


def all_scripts(cached: bool = False) -> list:
    rows = portal.fetch_cached(T_SCRIPTS, 30) if cached else portal._fetch(T_SCRIPTS)
    out = [{"id": r["id"], "name": str(r["fields"].get("Name") or ""), "no": r["fields"].get("Nummer") or 0,
            "active": bool(r["fields"].get("Aktiv")), "note": str(r["fields"].get("Notiz") or ""),
            "place": _place(r["fields"]), "stages": _stages(r["fields"])} for r in rows]
    return sorted(out, key=lambda s: (s["no"] or 999, s["name"]))


def _clean_stage(s: dict, i: int) -> dict:
    two = lambda v: {"de": str((v or {}).get("de") or "").strip(), "en": str((v or {}).get("en") or "").strip()}  # noqa: E731
    clips = [{"title": two(c.get("title")), "text": two(c.get("text")), "photos": str(c.get("photos") or "").strip()} for c in (s.get("clips") or [])
             if any(two(c.get("title")).values()) or any(two(c.get("text")).values())]
    price = s.get("price")
    try:
        price = float(str(price).replace(",", ".").replace("€", "").strip()) if str(price or "").strip() else None
    except ValueError:
        price = None
    return {"n": i + 1, "title": two(s.get("title")), "photos": str(s.get("photos") or "").strip(),
            "videos": len(clips) or int(s.get("videos") or 0), "outfit": two(s.get("outfit")), "poses": two(s.get("poses")),
            "clips": clips, "price": price}


# ---------------------------------------------------------------- manager API

def manager_api(action: str, body: dict, email: str) -> dict:
    from . import manager
    if action == "scripts.list":
        models = [{"id": r["id"], "name": str(r["fields"].get("Name") or ""), "scripts": r["fields"].get(F_LINK) or [],
                   "lang": _lang(r["fields"].get("Language"))}
                  for r in portal.fetch_cached(portal.T_MODELS, 30)
                  if r["fields"].get("Status") != "Ended" and not r["fields"].get("Alte Agentur")]
        return {"ok": True, "scripts": all_scripts(cached=True), "models": models}
    if action == "scripts.save":
        stages = [_clean_stage(s, i) for i, s in enumerate(body.get("stages") or [])]
        f = {"Name": str(body.get("name") or "").strip() or "Neues Skript", "Aktiv": bool(body.get("active", True)),
             "Stufen": json.dumps(stages, ensure_ascii=False), "Notiz": str(body.get("note") or ""),
             "Ort": json.dumps({k: str((body.get("place") or {}).get(k) or "").strip() for k in ("de", "en")}, ensure_ascii=False)}
        if body.get("no") not in (None, ""):
            f["Nummer"] = int(body["no"])
        if body.get("id"):
            manager._update(T_SCRIPTS, body["id"], f)
            sid = body["id"]
        else:
            f.setdefault("Nummer", max([s["no"] or 0 for s in all_scripts()] + [0]) + 1)
            sid = manager._create(T_SCRIPTS, f)["id"]
        portal.bust(T_SCRIPTS)
        return {"ok": True, "id": sid}
    if action == "scripts.assign":                           # body: {model, scripts: [ids]}
        manager._update(portal.T_MODELS, body.get("model", ""), {F_LINK: list(body.get("scripts") or [])})
        portal.bust(portal.T_MODELS)
        ensure_folders_later(body.get("model", ""))          # 16.11 Rene: stage folders exist as soon as a script is assigned
        return {"ok": True}
    raise ValueError("unknown action")


# ---------------------------------------------------------------- first tasks

STARTER = ((1, 7), (2, 7))                                     # (shoot no., days until due) - 16.11 Rene (final): two scripts within 7 days
BASE_SCRIPT = 1                                                 # 16.11 Rene: "jedes Skript kann nach dem Framework 1 sein" - both shoots follow script no. 1


def _round_folder(rec: str, sc: dict, rnd: int, lang: str) -> str:
    """Drive link of OF-Content/Skripte/<Skript rnd – name> (the second shoot gets its own tree so uploads never mix). '' when Drive is off."""
    if not drive_ready():
        return ""
    try:
        from . import drive_tree
        row = next((r["fields"] for r in portal.fetch_cached(portal.T_MODELS, 20) if r["id"] == rec), {})
        shared = drive_tree._shared_id(str(row.get("Name") or "").strip())
        if not shared:
            return ""
        root = drive_tree.ensure_folder_id(drive_tree.ensure_folder_id(shared, "OF-Content"), "Skripte")
        return drive_tree.link(drive_tree.ensure_folder_id(root, drive_tree._safe(f'{WORDS[lang]["script"]} {rnd} – {sc["name"]}')))
    except Exception as e:  # noqa: BLE001
        print("[scripts] round folder", rec, rnd, e)
        return ""


def starter_desc(rnd: int, n: int, lang: str) -> str:
    """16.11 Rene: there is only one framework, so the texts never name it; the second shoot only has to look like another day."""
    if rnd == BASE_SCRIPT:
        return (f"Alle {n} Stufen nach dem Framework drehen, so wie es unter „Skripte“ steht, und in die Stufen-Ordner im Drive hochladen. "
                f"Abhaken, wenn alles drin ist." if lang == "de" else
                f"Shoot all {n} stages of the framework as described under \"Scripts\" and upload them to the stage folders in Drive. "
                f"Tick it off when everything is in.")
    return (f"Noch einmal alle {n} Stufen nach dem Framework, am besten an einem anderen Tag als Skript 1. Drehst du am selben Tag, "
            f"wechsle vorher Outfit, Bettwäsche und alles, was man im Bild sieht. Hochladen in deinen Drive-Ordner „OF-Content › Skripte › Skript {rnd}“, "
            f"dort gibt es dieselben Stufen-Ordner. Abhaken, wenn alles drin ist." if lang == "de" else
            f"All {n} stages of the framework once more, ideally on a different day than script 1. If you shoot on the same day, "
            f"change your outfit, bedding and everything else in the picture first. Upload to your Drive folder \"OF Content › Skripte › Script {rnd}\", "
            f"it has the same stage folders. Tick it off when everything is in.")


def assign_starter(model_rec: str) -> list:
    """16.11 Rene: every new model gets two script shoots right away (Skript 1 and Skript 2, both after the Framework, both within
    7 days), as normal tasks next to Steckbrief / Bilder / Zugänge. Runs when the welcome videos are done.
    Idempotent: a task with the same title is left alone. Returns the created task titles."""
    import datetime as dt
    from . import manager, onboarding_portal as obp
    m = next((r for r in portal._fetch(portal.T_MODELS) if r["id"] == model_rec), None)
    if not m:
        return []
    lang = _lang(m["fields"].get("Language"))
    linked = list(m["fields"].get(F_LINK) or [])
    sc = next((s for s in all_scripts() if s["active"] and s["no"] == BASE_SCRIPT), None)
    if not sc:
        return []
    if sc["id"] not in linked:
        linked.append(sc["id"])
    have = {str(r["fields"].get("Task") or "").strip() for r in obp._linked(portal.T_TASKS, model_rec)}
    made, today, n = [], dt.date.today(), len(sc["stages"])
    for rnd, days in STARTER:
        title = (f"Skript {rnd} drehen und hochladen" if lang == "de" else f"Shoot and upload script {rnd}")
        if title in have:
            continue
        desc = starter_desc(rnd, n, lang)
        link = "/skripte" if rnd == BASE_SCRIPT else (_round_folder(model_rec, sc, rnd, lang) or "/skripte")
        obp._create(portal.T_TASKS, {"Task": title, "Model": [model_rec], "Category": "Content", "Status": "Open", "Priority": "High",
                                     "Assigned to": "Model", "Visible to model": True, "Due type": "By",
                                     "Due": (today + dt.timedelta(days=days)).isoformat(), "Description": desc, "Drive folder": link})
        made.append(title)
    if linked != list(m["fields"].get(F_LINK) or []):
        manager._update(portal.T_MODELS, model_rec, {F_LINK: linked})
        portal.bust(portal.T_MODELS)
    ensure_folders_later(model_rec)
    return made


# ---------------------------------------------------------------- model page

def model_scripts(rec: str) -> tuple[list, str]:
    m = next((r for r in portal.fetch_cached(portal.T_MODELS, 30) if r["id"] == rec), None)
    if not m:
        return [], "de"
    ids = m["fields"].get(F_LINK) or []
    lang = _lang(m["fields"].get("Language"))
    return [s for s in all_scripts(cached=True) if s["id"] in ids and s["active"]], lang


CAM = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h3l2-2h6l2 2h3v12H4z"/><circle cx="12" cy="13" r="3.5"/></svg>'
VID = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z"/></svg>'


def _split_dur(text: str) -> tuple[str, str]:
    """'… the shoulder. 30–43 s.' -> ('… the shoulder.', '30–43 s')"""
    import re
    m = re.search(r"\s*(\d+\s*[–-]\s*\d+\s*s|\d+\s*s)\.?\s*$", text or "")
    return ((text[:m.start()].rstrip() if m else text), (m.group(1) if m else ""))


MAP_DIR = __import__("pathlib").Path("/opt/northfeed-scripts")


def _map(rec: str) -> dict:
    try:
        return json.loads((MAP_DIR / f"{rec}.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def folder_link(rec: str, script_id: str, n: int, kind: str) -> str:
    """Drive link of OF-Content/<script>/<stage>/<Bilder|Videos> for this model (ids cached by ensure_folders)."""
    fid = _map(rec).get(f"{script_id}|{n}|{kind}")
    return f"https://drive.google.com/drive/folders/{fid}" if fid else ""


def drive_ready() -> bool:
    """16.11: folders only on the Northfeed Drive (info@) or, until that is connected, in the test root "Northfeed Test"
    (MODELS_DRIVE_ROOT); never inside the old agency's MODELS tree."""
    from . import drive_tree
    return drive_tree.REMOTE != "veralta" or drive_tree.ROOT != "MODELS"


_ensuring: set = set()


def ensure_folders(rec: str) -> dict:
    """Create OF-Content/Skripte/<Skript N – Name>/<Stufe K – Titel>/{Bilder,Videos} inside the model's shared folder for every
    assigned script, in her language, and cache the folder ids. Idempotent; runs in the background."""
    if not drive_ready() or rec in _ensuring:
        return {"ok": False, "reason": "drive not connected" if not drive_ready() else "running"}
    _ensuring.add(rec)
    try:
        from . import drive_tree
        scripts, lang = model_scripts(rec)
        if not scripts:                                          # 17.11 Rene: Skript 1 + 2 folders exist from the start, before the tasks
            scripts = [s for s in all_scripts(cached=True) if s["active"] and s["no"] == BASE_SCRIPT]
        if not scripts:
            return {"ok": True, "folders": 0}
        row = next((r["fields"] for r in portal.fetch_cached(portal.T_MODELS, 20) if r["id"] == rec), {})
        name = str(row.get("Name") or "").strip()
        em = str(row.get("Email") or "").strip().lower()
        shared = drive_tree.ensure_tree(name, email=em, model_rec_id=rec, write_airtable=True)["ids"]["shared"]
        w = WORDS[lang]
        of_root = drive_tree.ensure_folder_id(drive_tree.ensure_folder_id(shared, "OF-Content"), "Skripte")   # = drive_tree.MODEL_FOLDERS
        m = _map(rec)
        for sc in scripts:
            sf = drive_tree.ensure_folder_id(of_root, drive_tree._safe(f'{w["script"]} {sc["no"]} – {sc["name"]}'))
            if sc["no"] == BASE_SCRIPT:                              # 16.11: Skript 2 = second shoot of the same script, own stage tree
                for rnd, _ in STARTER:
                    if rnd != sc["no"]:
                        rf = drive_tree.ensure_folder_id(of_root, drive_tree._safe(f'{w["script"]} {rnd} – {sc["name"]}'))
                        for g in sc["stages"]:
                            st2 = drive_tree.ensure_folder_id(rf, drive_tree._safe(f'{w["stage"]} {g["n"]} – {_t(g["title"], lang)}'))
                            if g["photos"] and g["photos"] != "0" or any(c.get("photos") for c in g["clips"]):
                                drive_tree.ensure_folder_id(st2, w["photos"])
                            if g["videos"]:
                                drive_tree.ensure_folder_id(st2, w["videos"])
            for g in sc["stages"]:
                st = drive_tree.ensure_folder_id(sf, drive_tree._safe(f'{w["stage"]} {g["n"]} – {_t(g["title"], lang)}'))
                if g["photos"] and g["photos"] != "0" or any(c.get("photos") for c in g["clips"]):
                    m[f'{sc["id"]}|{g["n"]}|photos'] = drive_tree.ensure_folder_id(st, w["photos"])
                if g["videos"]:
                    m[f'{sc["id"]}|{g["n"]}|videos'] = drive_tree.ensure_folder_id(st, w["videos"])
        MAP_DIR.mkdir(parents=True, exist_ok=True)
        (MAP_DIR / f"{rec}.json").write_text(json.dumps(m))
        return {"ok": True, "folders": len(m)}
    except Exception as e:  # noqa: BLE001
        print("[scripts] folders failed", rec, e)
        return {"ok": False, "reason": str(e)[:200]}
    finally:
        _ensuring.discard(rec)


def ensure_folders_later(rec: str) -> None:
    import threading
    threading.Thread(target=ensure_folders, args=(rec,), daemon=True).start()


def page(rec: str, name: str) -> bytes:
    from .contract_data import _shell
    scripts, lang = model_scripts(rec)
    w = WORDS[lang]
    de = lang == "de"
    esc = html.escape
    if scripts and drive_ready() and not _map(rec):
        ensure_folders_later(rec)
    if not scripts:
        body = f'<div class="emptyc">{"Noch kein Skript für dich. Sobald wir eins zuweisen, steht es hier." if de else "No script yet. As soon as we assign one, it shows up here."}</div>'
    else:
        parts = []
        for s in scripts:
            total = len(s["stages"]) or 1
            st = []
            for g in s["stages"]:
                lv = (g["n"] - 1) / max(total - 1, 1)
                chips = "".join([
                    f'<span class="chip">{CAM}{esc(g["photos"])} {w["photos"]}</span>' if g["photos"] and g["photos"] != "0" else "",
                    f'<span class="chip">{VID}{g["videos"]} {w["videos"] if str(g["videos"]) != "1" else "Video"}</span>' if g["videos"] else ""])   # 17.11: "1 Video", not "1 Videos"
                blocks = "".join(f'<div class="blk"><div class="k">{k}</div><p>{esc(v)}</p></div>' for k, v in [
                    ("Outfit", _t(g["outfit"], lang)), ("Posen &amp; Bild" if de else "Poses &amp; look", _t(g["poses"], lang))] if v)
                clips = ""
                for i, c in enumerate(g["clips"]):
                    txt, dur = _split_dur(_t(c["text"], lang))
                    ph = str(c.get("photos") or "").strip()          # 16.11 Rene: 3-4 photos of exactly this moment, per video
                    phl = (f'<div class="cph">{CAM}+ {esc(ph)} {"Fotos dazu" if de else "photos"}</div>' if ph else "")
                    clips += (f'<div class="clip"><span class="cn">{i + 1}</span><div class="cb"><b>{esc(_t(c["title"], lang))}</b><p>{esc(txt)}</p>{phl}</div>'
                              f'{f"<span class=dur>{esc(dur)}</span>" if dur else ""}</div>')
                if clips:
                    clips = f'<div class="k" style="margin:4px 0 8px">{"Videos" if de else "Videos"}</div><div class="clips">{clips}</div>'
                ups = []
                for kind, icon, label in (("photos", CAM, "Bilder hochladen" if de else "Upload photos"), ("videos", VID, "Videos hochladen" if de else "Upload videos")):
                    if kind == "photos" and not (g["photos"] and g["photos"] != "0"):
                        continue
                    if kind == "videos" and not g["videos"]:
                        continue
                    link = folder_link(rec, s["id"], g["n"], kind)
                    ups.append(f'<a class="upb" href="{esc(link)}" target="_blank" rel="noopener">{icon}{label}</a>' if link
                               else f'<span class="upb off">{icon}{label}</span>')
                path = f'{w["root"]} › {w["script"]} {s["no"]} › {w["stage"]} {g["n"]}'
                hint = (f'Öffnet direkt den Ordner <b>{esc(path)}</b> in der Google-Drive-App, getrennt nach Bildern und Videos. Große Videos lädst du am besten im WLAN hoch.' if de
                        else f'Opens the folder <b>{esc(path)}</b> straight in the Google Drive app, photos and videos apart. Upload big videos on Wi-Fi.')
                if not any("href" in u for u in ups):
                    hint = ("Die Upload-Ordner werden gerade eingerichtet. Bis dahin: einfach schon drehen." if de
                            else "The upload folders are being set up. Until then: just start shooting.")
                st.append(f'<details class="stage" style="--lv:{lv:.2f}"><summary><span class="badge">{g["n"]}</span>'
                          f'<span class="tt"><span class="lbl">{w["stage"]} {g["n"]} / {total}</span><span class="name">{esc(_t(g["title"], lang))}</span>'
                          f'<span class="chips">{chips}</span></span><span class="chev">›</span></summary>'
                          f'<div class="body">{blocks}{clips}<div class="ups">{"".join(ups)}</div><p class="hint">{hint}</p></div></details>')
            scale = "".join(f'<i style="--lv:{(i) / max(total - 1, 1):.2f}"></i>' for i in range(total))
            place = _t(s.get("place"), lang)
            pin = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>'
            place_html = (f'<div class="place">{pin}<div><span class="lbl">{"Wo" if de else "Where"}</span>{esc(place)}</div></div>' if place else "")
            parts.append(f'<section class="script"><div class="shead"><div><div class="lbl">{w["script"]} {s["no"]}</div><h2>{esc(s["name"])}</h2></div>'
                         f'<div class="scale" title="{"von harmlos bis explizit" if de else "from soft to explicit"}">{scale}</div></div>{place_html}{"".join(st)}</section>')
        body = "".join(parts)
    first = esc((name or "").split(" ")[0])
    inner = (f'<a href="/" class="back">&larr; Portal</a>'
             f'<h1>{"Deine" if de else "Your"} <em>{"Skripte" if de else "scripts"}</em></h1>'
             f'<p class="lead">{"Hi " + first + ", jedes Skript geht Stufe für Stufe. Tippe eine Stufe an: Dort steht genau, was du drehst, und du lädst Bilder und Videos direkt in den richtigen Ordner." if de else "Hi " + first + ", every script goes stage by stage. Tap a stage to see exactly what to shoot, then upload photos and videos straight into the right folder."}</p>{body}')
    extra = ('<style>body{place-items:start center}.box{max-width:720px;margin:10px 0 40px}.back{color:#9aa3b2;text-decoration:none;font-size:13px}'
             '.lbl{display:block;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#9aa3b2;font-weight:600}'
             '.script{margin-top:26px}.shead{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;margin-bottom:14px}'
             '.shead h2{font-family:Fraunces,serif;font-weight:500;font-size:26px;margin:2px 0 0}'
             '.scale{display:flex;gap:4px;flex:none}.scale i{display:block;width:16px;height:6px;border-radius:3px;'
             'background:color-mix(in srgb,#ff5c9a calc(25% + var(--lv)*75%),#3a2232)}'
             '.stage{border:1px solid #2c3242;border-radius:18px;margin:0 0 12px;background:linear-gradient(180deg,#171b25,#13161e);overflow:hidden;transition:border-color .2s}'
             '.stage[open]{border-color:color-mix(in srgb,#ff5c9a calc(30% + var(--lv)*50%),#2c3242)}'
             '.stage summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:14px;padding:16px 18px}'
             '.stage summary::-webkit-details-marker{display:none}'
             '.badge{flex:none;width:42px;height:42px;border-radius:14px;display:grid;place-items:center;font-weight:700;font-size:17px;color:#fff;'
             'background:linear-gradient(135deg,color-mix(in srgb,#ff5c9a calc(20% + var(--lv)*80%),#2a2f3d),color-mix(in srgb,#c2416f calc(var(--lv)*100%),#232836))}'
             '.tt{flex:1;min-width:0}.name{display:block;font-weight:700;font-size:16.5px;margin:1px 0 6px}'
             '.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:#c9cfdb;'
             'background:#1f2430;border:1px solid #2c3242;border-radius:999px;padding:3px 10px}.chip svg{color:#ff9ec4}'
             '.chev{flex:none;color:#9aa3b2;font-size:22px;transition:transform .2s}.stage[open] .chev{transform:rotate(90deg)}'
             '.body{padding:4px 18px 18px;border-top:1px solid #232836}'
             '.blk{background:#1a1f2a;border-radius:14px;padding:12px 14px;margin:14px 0 10px}.blk p{margin:4px 0 0;color:#e6e8ee}'
             '.k{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#ff9ec4;font-weight:700}'
             '.clips{display:grid;gap:8px}.clip{display:flex;gap:12px;align-items:flex-start;background:#1a1f2a;border-radius:14px;padding:12px 14px}'
             '.cn{flex:none;width:24px;height:24px;border-radius:8px;display:grid;place-items:center;background:rgba(255,92,154,.16);color:#ff9ec4;font-size:12.5px;font-weight:700}'
             '.cph{display:inline-flex;align-items:center;gap:6px;margin-top:8px;font-size:13px;font-weight:600;color:#ffd3e4;background:rgba(255,92,154,.12);border:1px solid rgba(255,92,154,.35);border-radius:999px;padding:4px 10px}.cph svg{color:#ff9ec4}'
             '.cb{flex:1;min-width:0}.cb p{margin:3px 0 0;color:#c9cfdb;font-size:14px}.dur{flex:none;font-size:12px;color:#9aa3b2;background:#232836;border-radius:999px;padding:2px 8px;white-space:nowrap}'
             '.ups{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:16px}'
             '.upb{display:flex;align-items:center;justify-content:center;gap:8px;padding:13px 12px;border-radius:14px;font-weight:700;font-size:14px;color:#fff;text-decoration:none;'
             'background:linear-gradient(135deg,#ff5c9a,#c2416f)}.upb.off{background:#232836;color:#7d8596}'
             '.ups .upb:only-child{grid-column:1/-1}.hint{font-size:12.5px;color:#9aa3b2;margin:10px 2px 0}'
             '.place{display:flex;gap:12px;align-items:flex-start;background:rgba(255,92,154,.08);border:1px solid rgba(255,92,154,.3);border-radius:16px;padding:12px 14px;margin:0 0 14px;color:#e6e8ee}.place svg{flex:none;color:#ff9ec4;margin-top:2px}'
             '.emptyc{background:#161a22;border:1px dashed #343c4d;border-radius:16px;padding:22px;color:#9aa3b2;text-align:center}'
             '@media(max-width:520px){.box{padding:22px 16px}.shead{flex-direction:column;align-items:flex-start}.ups{grid-template-columns:1fr}}</style>')
    return _shell(inner).replace("Vertragsdaten</title>".encode(), f"{'Skripte' if de else 'Scripts'}</title>".encode(), 1) \
                        .replace(b"</head>", extra.encode() + b"</head>", 1)
