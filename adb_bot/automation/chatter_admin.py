"""Manager tab "Chatter" (18.11, Rene): add a chatter (portal login mail in English), read the test, move him through
Course → Test → Review → Test shift (4 h, unpaid, "wie ein Bewerbungsgespräch") → Contract → Active, or Rejected.
Data: Airtable Team (chatter_portal.T_TEAM) + Shifts (shifts.T_SHIFTS).

    chatter_admin.manager_api(action, body, email)
      chatter.list | chatter.create {name, email, shift, models, supervisor} | chatter.stage {id, stage} | chatter.update {id, shift, models, supervisor}
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from . import portal

STAGES = ["Course", "Test", "Review", "Test shift", "Contract", "Active", "Rejected"]
CONTRACTS = Path(os.environ.get("CHATTER_CONTRACT_DIR", "/opt/northfeed/chatter_contracts"))


def contract_pdf(rec: str) -> Path | None:
    p = CONTRACTS / f"{rec}.pdf"
    return p if rec and p.exists() else None


def send_contract(c: dict) -> dict:
    """Stage Contract: DocuSeal chatter agreement, company part signed automatically, sign link by mail."""
    from . import docuseal
    sub = docuseal.create_chatter_submission(c)
    first = (c.get("name") or "").split(" ")[0]
    mailed = _mail(c["email"], "Your Northfeed agreement – please sign",
                   f"Hi {first},\n\ncongratulations, you passed your test shift! Here is your agreement:\n{sub['sign_url']}\n\n"
                   "Please read it, fill in your date of birth, address and ID number, and sign. As soon as you have signed, "
                   "your account is activated and you get the invitation to our Discord.\n\nThe Northfeed team")
    return {"submission": sub.get("submission_id"), "mailed": mailed}


def contract_signed(res: dict) -> dict:
    """DocuSeal webhook for role Contractor: store the PDF, chatter → Active, welcome mail with Discord, alert."""
    import datetime as _dt
    import shutil
    from . import chatter_portal, manager, shifts
    email = (res.get("model_email") or "").lower()
    c = chatter_portal.team_by_email().get(email)
    if not c:
        return {"ok": False, "error": f"no chatter for {email}"}
    if res.get("pdf_path"):
        CONTRACTS.mkdir(parents=True, exist_ok=True)
        shutil.copy(res["pdf_path"], CONTRACTS / f"{c['id']}.pdf")
    manager._update(chatter_portal.T_TEAM, c["id"], {"Stage": "Active", "Status": "Active", "Contract signed": _dt.date.today().isoformat(),
                                                     "Start date": _dt.date.today().isoformat()})
    portal.bust(chatter_portal.T_TEAM)
    first = (c.get("name") or "").split(" ")[0]
    _mail(email, "You're in – welcome to the team",
          f"Hi {first},\n\nthank you for signing – welcome to the Northfeed chat team!\n"
          f"Join our Discord: {os.environ.get('CHATTER_DISCORD_URL', '')} and send us your Discord name.\n"
          f"Your shift and your creators are in your portal: {PORTAL}\nYour signed agreement is in the portal under your account.\n\nThe Northfeed team")
    shifts._post("CHATTER_WH_ALERT", f"✍️ **{first}** signed the agreement – now Active.")
    return {"ok": True, "chatter": c["id"]}
PORTAL = "https://portal.northfeed-management.com"


def _mail(to: str, subject: str, text: str) -> bool:
    from . import portal_auth
    try:
        return portal_auth._send(to, subject, text)
    except Exception as e:  # noqa: BLE001
        print("[chatter] mail:", e, flush=True)
        return False


def _rows() -> list[dict]:
    from . import chatter_portal, shifts
    models = {r["id"]: r["fields"].get("Name", "") for r in portal.fetch_cached(portal.T_MODELS, 60)}
    sh = portal.fetch_cached(shifts.T_SHIFTS, 30)
    out = []
    for r in portal._fetch(chatter_portal.T_TEAM):
        f = r["fields"]
        if f.get("Role") not in chatter_portal.ROLES:
            continue
        mine = [x["fields"] for x in sh if r["id"] in (x["fields"].get("Chatter") or [])]
        done = [x for x in mine if x.get("Status") == "Done"]
        c = {"id": r["id"], "name": f.get("Name", ""), "email": f.get("Email", ""), "role": f.get("Role", ""), "status": f.get("Status", ""),
             "shift": f.get("Shift", ""), "supervisor": f.get("Supervisor", ""), "models": [{"id": m, "name": models.get(m, "")} for m in f.get("Models") or []],
             "testScore": f.get("Test score"), "testAnswers": f.get("Test answers", ""), "testAt": f.get("Test at", ""),
             "shifts": len(done), "revenue": round(sum(float(x.get("Revenue") or 0) for x in done), 2),
             "running": any(x.get("Status") == "Running" for x in mine)}
        c["stage"] = chatter_portal.stage({"stage": f.get("Stage", ""), "course_done": bool(f.get("Course done"))})
        out.append(c)
    return out


def manager_api(action: str, body: dict, email: str) -> dict:
    from . import chatter_portal, manager
    if action == "chatter.list":
        ms = [{"id": r["id"], "name": r["fields"].get("Name", "")} for r in portal.fetch_cached(portal.T_MODELS, 60)
              if r["fields"].get("Status") != "Ended" and r["fields"].get("Stage") != "Beendet" and not r["fields"].get("Alte Agentur")]
        return {"ok": True, "chatters": _rows(), "models": ms, "stages": STAGES}
    if action == "chatter.create":
        name, mail = str(body.get("name") or "").strip(), str(body.get("email") or "").strip().lower()
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", mail):
            raise ValueError("Name und gültige E-Mail fehlen")
        if mail in chatter_portal.team_by_email():
            raise ValueError("Diese E-Mail ist schon angelegt")
        pw = secrets.token_urlsafe(9)
        f = {"Name": name, "Email": mail, "Role": "Chatter", "Status": "Onboarding", "Stage": "Course", "Language": "EN", "Portal password": pw,
             "Shift": str(body.get("shift") or "").strip(), "Supervisor": str(body.get("supervisor") or "René").strip()}
        if body.get("models"):
            f["Models"] = [m for m in body["models"] if re.fullmatch(r"rec\w{14}", str(m))]
        rec = manager._create(chatter_portal.T_TEAM, f)
        portal.bust(chatter_portal.T_TEAM)
        mailed = _mail(mail, "Welcome to Northfeed – your chatter portal",
                       f"Hi {name.split(' ')[0]},\n\nwelcome to Northfeed! Your next step is our chatting course.\n\n"
                       f"Portal: {PORTAL}\nE-mail: {mail}\nPassword: {pw}\n\n"
                       "Watch every lesson (you can rewatch them any time), then take the short test at the end. "
                       "After that we plan your 4-hour test shift together.\n\nYou can change your password in the portal under your account.\n\n"
                       "The Northfeed team")
        return {"ok": True, "id": rec.get("id"), "mailed": mailed}
    if action == "chatter.stage":
        rid, st = str(body.get("id") or ""), str(body.get("stage") or "")
        if st not in STAGES:
            raise ValueError("unbekannte Stufe")
        c = next(({**x, "email": em} for em, x in chatter_portal.team_by_email().items() if x["id"] == rid), None)
        if not c:
            raise ValueError("Chatter nicht gefunden")
        f = {"Stage": st}
        if st == "Active":
            f["Status"] = "Active"
        if st == "Contract":                                  # 18.11: the agreement goes out right away (DocuSeal)
            r = send_contract({**c})
            manager._update(chatter_portal.T_TEAM, rid, f)
            portal.bust(chatter_portal.T_TEAM)
            return {"ok": True, "mailed": r["mailed"]}
        manager._update(chatter_portal.T_TEAM, rid, f)
        portal.bust(chatter_portal.T_TEAM)
        first = (c.get("name") or "").split(" ")[0]
        mails = {
            "Test shift": ("You passed the test – next: your test shift",
                           f"Hi {first},\n\ngreat news: you passed the test! Next is your 4-hour test shift. We will send you the date and time.\n"
                           f"On the day: log in at {PORTAL}, press Start shift, and End shift with your report when you finish.\n\nThe Northfeed team"),
            "Active": ("You're in – welcome to the team",
                       f"Hi {first},\n\nwelcome to the Northfeed chat team! Your regular shifts start now.\n"
                       f"Join our Discord: {os.environ.get('CHATTER_DISCORD_URL', '')} and send us your Discord name.\n"
                       f"Your shift and your creators are in your portal: {PORTAL}\n\nThe Northfeed team"),
            "Rejected": ("Your application at Northfeed",
                         f"Hi {first},\n\nthank you for your time and for doing the course and the test. This time we decided not to continue. "
                         "We wish you all the best.\n\nThe Northfeed team"),
        }
        mailed = _mail(c["email"], *mails[st]) if st in mails and body.get("mail", True) else False
        return {"ok": True, "mailed": mailed}
    if action == "chatter.update":
        rid = str(body.get("id") or "")
        f = {}
        for k, fld in (("shift", "Shift"), ("supervisor", "Supervisor")):
            if k in body:
                f[fld] = str(body[k] or "").strip()
        if "models" in body:
            f["Models"] = [m for m in body["models"] if re.fullmatch(r"rec\w{14}", str(m))]
        manager._update(chatter_portal.T_TEAM, rid, f)
        portal.bust(chatter_portal.T_TEAM)
        return {"ok": True}
    raise ValueError(f"unknown action {action}")
