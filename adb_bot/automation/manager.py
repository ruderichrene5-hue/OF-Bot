"""Northfeed manager side of the portal (14.11, Rene): the agency lead logs in on the same host,
sees every model at once (plan, tasks, what was ticked off), creates models, plan entries and tasks
directly, and turns funnel applications into models. Data: the "Northfeed Portal" base (same
tables as portal.py) plus a Managers table, and the funnel applications on disk.

    manager.build(email) -> full HTML page
    manager.api(action, body, email) -> dict (JSON for the page)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
from pathlib import Path

import requests

from . import portal

T_MANAGERS = os.environ.get("PORTAL_MANAGERS_TABLE", "tblufIWqAmfRfTx2k")
T_TOOLS = "tblSXOhsJFiOEVr9m"                      # 15.11 Rene: every tool one click away
TOOL_FIELDS = {"name": "Name", "url": "URL", "group": "Group", "desc": "Description", "order": "Order", "hint": "Login hint"}
APPS_DIR = Path("/opt/northfeed-applications")
LOGO = portal.LOGO
APP_STATUS = ("new", "contacted", "accepted", "declined")


# ---------------------------------------------------------------- airtable helpers

def _hdr() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"}


def _url(table: str, rec: str = "") -> str:
    return f"https://api.airtable.com/v0/{portal.BASE}/{table}" + (f"/{rec}" if rec else "")


def _create(table: str, fields: dict) -> dict:
    r = requests.post(_url(table), headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=40)
    portal.bust(table)                                       # 16.11: after the write, so no reader re-caches the old rows
    if r.status_code >= 300:
        raise ValueError(r.json().get("error", {}).get("message", r.text)[:300])
    return r.json()


def _update(table: str, rec: str, fields: dict) -> dict:
    if not re.fullmatch(r"rec[A-Za-z0-9]{14}", rec):
        raise ValueError("bad record id")
    r = requests.patch(_url(table, rec), headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=40)
    portal.bust(table)
    if r.status_code >= 300:
        raise ValueError(r.json().get("error", {}).get("message", r.text)[:300])
    return r.json()


def managers_by_email(cached: bool = False) -> dict[str, dict]:
    out = {}
    try:
        for m in (portal.fetch_cached(T_MANAGERS, 120) if cached else portal._fetch(T_MANAGERS)):
            f = m["fields"]
            mail = str(f.get("Email") or "").strip().lower()
            if mail and f.get("Active", True):
                out[mail] = {"id": m["id"], "name": f.get("Name", ""), "password": str(f.get("Password") or "")}
    except Exception as e:  # noqa: BLE001 -- table missing / token problem: nobody is a manager
        print("manager.managers_by_email:", e)
    return out


def is_manager(email: str | None) -> bool:
    if not email:
        return False
    em = email.strip().lower()
    return em in managers_by_email(cached=True)          # 2-min copy; the manager list changes almost never


# ---------------------------------------------------------------- data

def _s(v) -> str:
    return "" if v is None else str(v)


def _att(v):
    return [{"url": a.get("url"), "name": a.get("filename"), "type": a.get("type", "")} for a in (v or [])] if isinstance(v, list) else []


def applications() -> list[dict]:
    """Funnel applications on disk (paid-ads-funnel = creator funnel), newest first, with status file."""
    out = []
    if not APPS_DIR.exists():
        return out
    for p in sorted(APPS_DIR.glob("*.json"), reverse=True):
        if p.name.endswith(".status.json"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if d.get("source") == "chatter-funnel":
            continue
        aid = p.stem
        st = {"status": "new", "note": ""}
        sp = APPS_DIR / f"{aid}.status.json"
        if sp.exists():
            try:
                st.update(json.loads(sp.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
        answers = d.get("answers")
        if isinstance(answers, str):
            try:
                answers = json.loads(answers.replace("'", '"'))
            except Exception:  # noqa: BLE001
                answers = {"raw": answers}
        photos = d.get("_photos") or []
        if isinstance(photos, str):
            photos = re.findall(r"p[a-z0-9]+\.(?:jpg|png|webp|pdf)", photos)
        out.append({"id": aid, "received": _s(d.get("_received")), "lang": _s(d.get("lang")), "score": _s(d.get("score")),
                    "firstName": _s(d.get("first_name")), "lastName": _s(d.get("last_name")), "email": _s(d.get("email")),
                    "phone": _s(d.get("phone")), "instagram": _s(d.get("instagram")), "answers": answers if isinstance(answers, dict) else {},
                    "photos": [f"/manager/app/{aid}/{x}" for x in photos if (APPS_DIR / aid / x).exists()],
                    "utm": _s((d.get("attribution") or {}).get("utm_campaign") if isinstance(d.get("attribution"), dict) else ""),
                    "status": st.get("status", "new") if st.get("status") in APP_STATUS else "new", "note": _s(st.get("note")),
                    "model": _s(st.get("model"))})
    return out


def app_file(aid: str, name: str) -> Path | None:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9-]+", aid) or not re.fullmatch(r"p[a-z0-9]{1,40}\.(jpg|png|webp|pdf)", name):   # 17.11: the funnel names photos p<base36>, e.g. pmwkbrcult53.jpg
        return None
    f = APPS_DIR / aid / name
    return f if f.exists() else None


def overview(email: str) -> dict:
    from concurrent.futures import ThreadPoolExecutor        # 15.11: four Airtable tables in parallel instead of one after another
    with ThreadPoolExecutor(5) as ex:
        fut = {t: ex.submit(portal.fetch_cached, t, 20) for t in (portal.T_MODELS, portal.T_PLAN, portal.T_TASKS, T_TOOLS)}   # 16.11: warm cache, writes bust it
        fut_apps = ex.submit(applications)
        fut_me = ex.submit(managers_by_email, True)
        rows = {t: f.result() for t, f in fut.items()}
        apps = fut_apps.result()
    models = [{"id": r["id"], "name": _s(f.get("Name")).strip(), "email": _s(f.get("Email")), "status": _s(f.get("Status") or "Onboarding"),
               "language": _s(f.get("Language") or "EN"), "shootDays": f.get("Shoot days") or [], "start": _s(f.get("Start date")),
               "handles": _s(f.get("Instagram handles")), "onlyfans": _s(f.get("OnlyFans link")), "notes": _s(f.get("Notes")),
               "hasPassword": bool(f.get("Portal password")), "drive": _s(f.get("Drive folder")),
               "stage": LEGACY_STAGE.get(_s(f.get("Stage")), _s(f.get("Stage"))), "package": _s(f.get("Package")), "split": f.get("Management split %"),
               "whatsapp": _s(f.get("WhatsApp")), "callDate": _s(f.get("Call date")), "contractSent": _s(f.get("Contract sent")),
               "contractSigned": _s(f.get("Contract signed")), "contractPdf": _s(f.get("Contract PDF")), "uploadsFolder": _s(f.get("Uploads folder")),
               "welcomeSent": _s(f.get("Welcome sent")), "legalName": _s(f.get("Legal name")), "birthdate": _s(f.get("Birthdate")),
               "address": _s(f.get("Address")), "proofChannel": _s(f.get("Proof channel")), "accountsMode": _s(f.get("Accounts im Portal") or "Nein"), "ofPhotos": bool(f.get("OF-Bilder nötig")),
               "dataSubmitted": _s(f.get("Contract data submitted")), "street": _s(f.get("Street")), "zip": _s(f.get("ZIP")), "city": _s(f.get("City")), "country": _s(f.get("Country")),
               "oldAgency": bool(f.get("Alte Agentur"))}
              for r in rows[portal.T_MODELS] for f in [r["fields"]]]
    by_id = {m["id"]: m for m in models}
    plan = [{"id": r["id"], "model": (f.get("Model") or [""])[0], "title": _s(f.get("Title")), "date": _s(f.get("Date")),
             "platform": _s(f.get("Platform")), "type": _s(f.get("Type")), "status": _s(f.get("Status") or "Planned"),
             "hook": _s(f.get("Hook / idea")), "caption": _s(f.get("Caption")), "outfit": _s(f.get("Outfit / location")),
             "reference": _s(f.get("Reference")), "published": _s(f.get("Published link")), "notes": _s(f.get("Notes")),
             "files": _att(f.get("Files")), "drive": _s(f.get("Drive folder"))}
            for r in rows[portal.T_PLAN] for f in [r["fields"]]]
    tasks = [{"id": r["id"], "model": (f.get("Model") or [""])[0], "title": _s(f.get("Task")), "category": _s(f.get("Category")),
              "due": _s(f.get("Due")), "dueType": _s(f.get("Due type") or ("Fixed day" if f.get("Due") else "This week")),
              "status": _s(f.get("Status") or "Open"), "priority": _s(f.get("Priority")), "assigned": _s(f.get("Assigned to") or "Model"),
              "desc": _s(f.get("Description")), "visible": bool(f.get("Visible to model")), "files": _att(f.get("Files"))}
             for r in rows[portal.T_TASKS] for f in [r["fields"]]]
    for p in plan:
        p["modelName"] = by_id.get(p["model"], {}).get("name", "")
    for t in tasks:
        t["modelName"] = by_id.get(t["model"], {}).get("name", "")
    plan.sort(key=lambda x: (x["date"] or "9999", x["title"]))
    tasks.sort(key=lambda x: (x["status"] == "Done", x["due"] or "9999", x["title"]))
    me = fut_me.result().get(email.lower(), {})
    return {"manager": {"email": email, "name": me.get("name") or email.split("@")[0]}, "models": models, "plan": plan, "tasks": tasks,
            "applications": apps, "today": dt.date.today().isoformat(), "links": {"cal": "https://cal.com/northfeed/call"},
            "mailReady": bool(os.environ.get("PORTAL_SMTP_HOST") and os.environ.get("PORTAL_SMTP_PASSWORD")),
            "deeplReady": bool(os.environ.get("DEEPL_API_KEY")),
            "massSheet": f"https://docs.google.com/spreadsheets/d/{os.environ['MASS_SHEET_ID']}/edit" if os.environ.get("MASS_SHEET_ID") else "",
            "discordReady": sum(bool(os.environ.get(k)) for k in ("CHATTER_WH_ONLINE", "CHATTER_WH_LOG", "CHATTER_WH_ALERT")),
            "tools": sorted([{"id": r["id"], "name": _s(f.get("Name")), "url": _s(f.get("URL")), "group": _s(f.get("Group") or "Sonstiges"),
                              "desc": _s(f.get("Description")), "order": f.get("Order") or 999, "hint": _s(f.get("Login hint"))}
                             for r in rows[T_TOOLS] for f in [r["fields"]]], key=lambda t: (t["order"], t["name"])),
            "toolGroups": ["Northfeed", "Tools", "Content Creation", "Ads", "Models", "Sonstiges"],   # 15.11 Rene; "Archiv" is hidden
            "options": {"status": ["Onboarding", "Active", "Paused", "Ended"], "language": ["DE", "EN"],
                        "stage": STAGES, "package": list(PACKAGES), "proofChannel": ["WhatsApp", "Telegram", "E-Mail"], "accountsMode": ["Nein", "Nur Summen", "Alle"],
                        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                        "platform": ["Instagram", "TikTok", "OnlyFans", "X", "Other"],
                        "type": ["Reel", "Story", "Post", "Carousel", "Set", "Custom", "PPV", "Live"],
                        "planStatus": ["Idea", "Planned", "To shoot", "Shot", "Edited", "Scheduled", "Published", "Dropped"],
                        "category": ["Content", "Shooting", "Account", "Chat", "Admin", "Other"],
                        "taskStatus": ["Open", "In progress", "Waiting", "Done"], "priority": ["High", "Medium", "Low"],
                        "dueType": ["Fixed day", "By", "This week"]}}


# ---------------------------------------------------------------- writes

def _clean(body: dict, allowed: dict) -> dict:
    """Pick allowed keys -> Airtable field names; drop empties; keep lists."""
    out = {}
    for key, field in allowed.items():
        if key in body:
            v = body[key]
            if isinstance(v, str):
                v = v.strip()
            if v in ("", None, []):
                continue
            out[field] = v
    return out


MODEL_FIELDS = {"name": "Name", "email": "Email", "status": "Status", "language": "Language", "shootDays": "Shoot days", "start": "Start date",
                "handles": "Instagram handles", "onlyfans": "OnlyFans link", "notes": "Notes", "password": "Portal password",
                # 15.11 pipeline + contract
                "stage": "Stage", "package": "Package", "split": "Management split %", "whatsapp": "WhatsApp", "callDate": "Call date",
                "contractSent": "Contract sent", "contractSigned": "Contract signed", "contractPdf": "Contract PDF",
                "uploadsFolder": "Uploads folder", "welcomeSent": "Welcome sent", "legalName": "Legal name", "birthdate": "Birthdate",
                "address": "Address", "proofChannel": "Proof channel", "accountsMode": "Accounts im Portal",
                "street": "Street", "zip": "ZIP", "city": "City", "country": "Country",
                "ofPhotos": "OF-Bilder nötig"}
STAGES = ["Bewerbung", "Kontaktiert", "Call gebucht", "Vertrag offen", "Unterschrieben", "Onboarding", "Aktiv", "Pausiert", "Beendet"]
POST_SIGN = ["Unterschrieben", "Onboarding", "Material da", "Aktiv", "Pausiert"]
LEGACY_STAGE = {"Neu": "Bewerbung", "Material da": "Onboarding"}        # 15.11 Rene: fewer columns; old values read as the new ones
PACKAGES = {"Full Management": 60, "Marketing": 40, "Chatting": 40}
PLAN_FIELDS = {"title": "Title", "date": "Date", "platform": "Platform", "type": "Type", "status": "Status", "hook": "Hook / idea",
               "caption": "Caption", "outfit": "Outfit / location", "reference": "Reference", "published": "Published link", "notes": "Notes"}
TASK_FIELDS = {"title": "Task", "category": "Category", "due": "Due", "dueType": "Due type", "status": "Status", "priority": "Priority",
               "assigned": "Assigned to", "desc": "Description"}


def _ensure_drive_quiet(rec: str) -> None:
    try:
        _ensure_drive(rec)
    except Exception as e:  # noqa: BLE001
        print("[manager] drive tree failed for", rec, e)


def _ensure_drive(rec: str) -> dict:
    """Standard tree for one model (drive_tree.ensure_tree), shared with her e-mail, links written back."""
    from . import drive_tree
    rows = {r["id"]: r["fields"] for r in portal._fetch(portal.T_MODELS)}
    f = rows.get(rec)
    if not f:
        raise ValueError("unknown model")
    r = drive_tree.ensure_tree(_s(f.get("Name")).strip(), email=_s(f.get("Email")).strip().lower(), model_rec_id=rec)
    from . import scripts
    scripts.ensure_folders(rec)                              # 17.11 Rene: complete tree incl. Skript 1 + 2 right away
    return {"root": r["links"]["root"], "uploads": r["links"]["Referenzbilder"], "shared": bool(r["shares"])}


def api(action: str, body: dict, email: str) -> dict:
    if action.startswith(("course.", "lesson.")):            # 15.11 Memberspot-style courses
        from . import course
        return course.course_api(action, body)
    if action == "model.create":
        f = _clean(body, MODEL_FIELDS)
        if not f.get("Name"):
            raise ValueError("Name fehlt")
        if "Status" not in f:
            f["Status"] = "Onboarding"
        # 15.11 Rene: a model we create by hand is in onboarding; a dragged application carries its target stage
        f.setdefault("Stage", "Onboarding")
        f.setdefault("Package", "Full Management")
        f.setdefault("Proof channel", "WhatsApp")
        f.setdefault("Management split %", PACKAGES.get(f.get("Package"), 60))
        if body.get("generatePassword") and "Portal password" not in f:
            f["Portal password"] = secrets.token_urlsafe(8)
        r = _create(portal.T_MODELS, f)
        drive = None
        if f["Stage"] not in ("Bewerbung", "Kontaktiert", "Call gebucht", "Vertrag offen"):   # 15.11 Rene: folder exists the moment the model does
            import threading
            threading.Thread(target=_ensure_drive_quiet, args=(r["id"],), daemon=True).start()
            drive = {"queued": True}
        return {"ok": True, "id": r["id"], "password": f.get("Portal password", ""), "drive": drive}
    if action == "model.update":
        f = _clean(body, MODEL_FIELDS)
        if body.get("clearPassword"):
            f["Portal password"] = ""
        if f.get("Package") and "Management split %" not in f:
            f["Management split %"] = PACKAGES.get(f["Package"], 60)
        _update(portal.T_MODELS, body.get("id", ""), f)
        if "ofPhotos" in body:                                # 16.11 Rene: OF photos task follows the switch
            import threading
            from . import onboarding_portal
            portal.bust(portal.T_MODELS)
            threading.Thread(target=onboarding_portal.sync_of_task, args=(body.get("id", ""),), daemon=True).start()
        return {"ok": True}
    if action == "model.stage":                              # 15.11: pipeline step with its side effects
        rec, stage = body.get("id", ""), str(body.get("stage", ""))
        if stage not in STAGES:
            raise ValueError("unknown stage")
        cur = next((r["fields"] for r in portal._fetch(portal.T_MODELS) if r["id"] == rec), {})
        signed = bool(cur.get("Contract signed") or cur.get("Alte Agentur")) or LEGACY_STAGE.get(_s(cur.get("Stage")), _s(cur.get("Stage"))) in POST_SIGN
        if signed and stage not in POST_SIGN and stage != "Beendet":     # 22.11 Rene: a signed model never goes back into the funnel
            raise ValueError("Model hat schon unterschrieben, zurück in die Bewerbung geht nicht")
        f = {"Stage": stage}
        today = dt.date.today().isoformat()
        if stage == "Vertrag offen":
            f["Contract sent"] = today
        if stage == "Unterschrieben":
            f.setdefault("Contract signed", today)
        if stage == "Aktiv":
            f["Status"] = "Active"
        if stage == "Pausiert":
            f["Status"] = "Paused"
        if stage == "Beendet":
            f["Status"] = "Ended"
        _update(portal.T_MODELS, rec, f)
        out = {"ok": True, "stage": stage}
        if stage == "Beendet":                            # 16.11 Rene (J): ended models lose the Drive share
            import threading
            def _revoke(rec=rec):
                try:
                    from . import drive_tree
                    row = next((r["fields"] for r in portal._fetch(portal.T_MODELS) if r["id"] == rec), {})
                    print("[manager] drive revoke", rec, drive_tree.revoke(_s(row.get("Email")), row))
                except Exception as e:  # noqa: BLE001
                    print("[manager] drive revoke failed", rec, e)
            threading.Thread(target=_revoke, daemon=True).start()
            out["driveRevoke"] = True
        if stage in ("Unterschrieben", "Onboarding"):     # the tree exists only after the signature (Rene: no work before the contract)
            import threading                              # 15.11 Rene: moving a card must be instant; Drive takes 10-40 s
            threading.Thread(target=_ensure_drive_quiet, args=(rec,), daemon=True).start()
            out["driveQueued"] = True
        return out
    if action == "tool.create":
        f = _clean(body, TOOL_FIELDS)
        if not f.get("Name") or not f.get("URL"):
            raise ValueError("Name und URL fehlen")
        return {"ok": True, "id": _create(T_TOOLS, f)["id"]}
    if action == "tool.update":
        _update(T_TOOLS, body.get("id", ""), _clean(body, TOOL_FIELDS))
        return {"ok": True}
    if action == "tool.delete":
        rec = body.get("id", "")
        if not re.fullmatch(r"rec[A-Za-z0-9]{14}", rec):
            raise ValueError("bad record id")
        r = requests.delete(_url(T_TOOLS, rec), headers=_hdr(), timeout=30)
        return {"ok": r.ok}
    if action == "model.delete":                             # 22.11 Rene: signed models end with Aktiv, Beenden or Löschen
        rec = body.get("id", "")
        if not re.fullmatch(r"rec[A-Za-z0-9]{14}", rec):
            raise ValueError("bad record id")
        row = next((r["fields"] for r in portal._fetch(portal.T_MODELS) if r["id"] == rec), None)
        if row is None:
            raise ValueError("Model nicht gefunden")
        try:
            from . import drive_tree
            print("[manager] drive revoke before delete", rec, drive_tree.revoke(_s(row.get("Email")), row))
        except Exception as e:  # noqa: BLE001
            print("[manager] drive revoke failed", rec, e)
        for t in (portal.T_TASKS, portal.T_PLAN):           # her tasks and plan go with her; invoices stay (bookkeeping)
            ids = [r["id"] for r in portal._fetch(t) if rec in (r["fields"].get("Model") or [])]
            for i in range(0, len(ids), 10):
                requests.delete(_url(t, ""), headers=_hdr(), params=[("records[]", x) for x in ids[i:i + 10]], timeout=30)
        r = requests.delete(_url(portal.T_MODELS, rec), headers=_hdr(), timeout=30)
        for t in (portal.T_MODELS, portal.T_TASKS, portal.T_PLAN):
            portal.bust(t)
        if not r.ok:
            raise ValueError(f"Airtable {r.status_code}")
        return {"ok": True}
    if action == "model.requestImages":                      # 15.11: ask a model for new reference images
        from . import onboarding_portal
        return onboarding_portal.request_images(body.get("id", ""), str(body.get("note", "")), email)
    if action == "model.confirmImages":                      # 16.11 Rene: the team checks the images, then ticks the task
        from . import onboarding_portal
        return onboarding_portal.confirm_images(body.get("id", ""), str(body.get("key", "")))
    if action == "model.languages":                          # 16.11 Rene: languages are set by us, not asked in her Steckbrief
        from . import onboarding_portal
        return onboarding_portal.set_languages(body.get("id", ""), str(body.get("languages", "")))
    if action == "model.contract":                           # 15.11: e-signature, Rene first (stored signature), model second
        from . import onboarding
        return onboarding.send_contract(body.get("id", ""))
    if action == "setup.status":                             # 15.11 Rene: what he still has to fix, in the settings
        from . import setup_checklist
        return {"ok": True, "items": setup_checklist.status()}
    if action == "setup.toggle":
        from . import setup_checklist
        return setup_checklist.toggle(str(body.get("key", "")), bool(body.get("done")))
    if action == "model.vault":                              # 15.11 Rene (H): access data, passwords only on reveal
        from . import access_vault
        return {"ok": True, **access_vault.public(body.get("id", ""))}
    if action == "model.vaultReveal":
        from . import access_vault
        return {"ok": True, "pw": access_vault.reveal(body.get("id", ""), int(body.get("i", 0)), email)}
    if action == "model.vaultRequest":
        from . import onboarding
        m = onboarding._model(body.get("id", ""))["fields"]
        if not m.get("Email"):
            raise ValueError("Model hat keine E-Mail")
        mailed = onboarding.send_mail(str(m["Email"]), "Bitte hinterlege deine Zugänge",
            f"Hi {onboarding._first(str(m.get('Name') or ''))},\n\ndamit wir deine Accounts betreuen können, trag bitte im Portal deine Zugänge ein "
            f"(Instagram, TikTok und so weiter). Die Passwörter werden verschlüsselt gespeichert. Dein OnlyFans-Passwort brauchen wir nicht.\n\n"
            f"https://portal.northfeed-management.com/zugaenge\n\nDein Northfeed-Team\n")
        return {"ok": True, "mailed": mailed}
    if action.startswith("chatter."):                          # 18.11 Rene: chatter pipeline (course → test → test shift → contract)
        from . import chatter_admin
        return chatter_admin.manager_api(action, body, email)
    if action.startswith("scripts."):                          # 16.11 Rene: OF scripts (PPV ladders) per model
        from . import scripts
        return scripts.manager_api(action, body, email)
    if action == "contract.template":                         # 17.11 Rene: the raw contract, editable in the settings
        from . import contract_template
        return {"ok": True, **contract_template.get()}
    if action == "contract.templateSave":
        from . import contract_template
        return contract_template.save(str(body.get("body") or ""), email)
    if action.startswith("billing."):                          # 15.11 Rene (I): weekly invoices
        from . import billing
        return billing.manager_api(action, body, email)
    if action == "model.dataRequest":                        # 15.11 Rene (D): model enters her own contract data
        from . import contract_data
        return contract_data.request(body.get("id", ""))
    if action == "model.access":                             # 15.11 Rene (E): login + welcome mail without a contract
        from . import onboarding
        return onboarding.send_access(body.get("id", ""))
    if action == "model.drive":
        return {"ok": True, "drive": _ensure_drive(body.get("id", ""))}
    if action == "plan.create":
        f = _clean(body, PLAN_FIELDS)
        if not f.get("Title") or not body.get("model"):
            raise ValueError("Titel und Model fehlen")
        f["Model"] = [body["model"]]
        f.setdefault("Status", "Planned")
        f.setdefault("Platform", "Instagram")
        f.setdefault("Type", "Reel")
        return {"ok": True, "id": _create(portal.T_PLAN, f)["id"]}
    if action == "plan.update":
        f = _clean(body, PLAN_FIELDS)
        if body.get("model"):
            f["Model"] = [body["model"]]
        _update(portal.T_PLAN, body.get("id", ""), f)
        return {"ok": True}
    if action == "task.create":
        f = _clean(body, TASK_FIELDS)
        if not f.get("Task") or not body.get("model"):
            raise ValueError("Aufgabe und Model fehlen")
        f["Model"] = [body["model"]]
        f["Visible to model"] = bool(body.get("visible", True))
        f.setdefault("Status", "Open")
        f.setdefault("Assigned to", "Model")
        f.setdefault("Due type", "Fixed day" if f.get("Due") else "This week")
        return {"ok": True, "id": _create(portal.T_TASKS, f)["id"]}
    if action == "task.delete":                              # 22.11 Rene: delete button on every task in the manager
        rec = body.get("id", "")
        if not re.fullmatch(r"rec[A-Za-z0-9]{14}", rec):
            raise ValueError("bad record id")
        r = requests.delete(_url(portal.T_TASKS, rec), headers=_hdr(), timeout=30)
        portal.bust(portal.T_TASKS)
        if not r.ok:
            raise ValueError(f"Airtable {r.status_code}")
        return {"ok": True}
    if action == "task.update":
        f = _clean(body, TASK_FIELDS)
        if "visible" in body:
            f["Visible to model"] = bool(body["visible"])
        if body.get("model"):
            f["Model"] = [body["model"]]
        _update(portal.T_TASKS, body.get("id", ""), f)
        return {"ok": True}
    if action == "app.status":
        aid = str(body.get("id", ""))
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9-]+", aid) or not (APPS_DIR / f"{aid}.json").exists():
            raise ValueError("unknown application")
        st = str(body.get("status", "new"))
        if st not in APP_STATUS:
            raise ValueError("bad status")
        cur = {}
        sp = APPS_DIR / f"{aid}.status.json"
        if sp.exists():
            try:
                cur = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                cur = {}
        cur.update({"status": st, "note": str(body.get("note", cur.get("note", "")))[:1000], "by": email,
                    "at": dt.datetime.now().isoformat(timespec="seconds")})
        if body.get("model"):
            cur["model"] = str(body["model"])[:80]
        sp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True}
    raise ValueError("unknown action")


# ---------------------------------------------------------------- page

def render(d: dict) -> str:
    import base64
    tpl = Path(__file__).with_name("manager.html").read_text(encoding="utf-8")
    uri = lambda f: "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode() if f.exists() else ""
    from . import course
    return (tpl.replace("__LOGO__", uri(LOGO)).replace("__COURSE_ADMIN__", course.admin_fragment())
            .replace("__DATA__", json.dumps(d, ensure_ascii=False).replace("</", "<\\/")))


def build(email: str) -> str:
    return render(overview(email))
