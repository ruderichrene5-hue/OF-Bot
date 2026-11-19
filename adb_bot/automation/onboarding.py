"""Onboarding automation (15.11, Rene: "alles automatisiert, keine Arbeit pro Model"). The manager only moves
the stage; everything else is a side effect of the stage:

    Vertrag offen   -> docuseal.create_submission (Rene's signature first, the model's second), Contract sent = today,
                       the model's signing link is mailed from info@ (when PORTAL_SMTP_* is set) and shown to Rene.
    webhook signed  -> Contract signed, PDF into Drive Intern/Vertrag, Stage Unterschrieben, Drive tree + shares,
                       portal password, welcome mail with login + Drive links.
    (Onboarding / Material da / Aktiv are set by hand or by later automations.)

Nothing here runs before the signature: no portal access, no Drive, no mail. All mail goes through the same SMTP
settings the portal login uses (PORTAL_SMTP_HOST/PORT/USER/PASSWORD/FROM in /etc/adbbot/env).
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import smtplib
import subprocess
import tempfile
from email.message import EmailMessage
from pathlib import Path

import requests

from . import portal, drive_tree

PORTAL_URL = "https://portal.northfeed-management.com"
_AT = lambda: {"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"}  # noqa: E731


def _patch(rec: str, fields: dict) -> None:
    r = requests.patch(f"https://api.airtable.com/v0/{portal.BASE}/{portal.T_MODELS}/{rec}", headers=_AT(),
                       json={"fields": fields, "typecast": True}, timeout=40)
    r.raise_for_status()
    portal.bust(portal.T_MODELS)                             # 16.11 launch check: the manager saw "Daten fehlen" for 20 s after she sent them


def _model(rec: str) -> dict:
    r = requests.get(f"https://api.airtable.com/v0/{portal.BASE}/{portal.T_MODELS}/{rec}", headers=_AT(), timeout=40)
    r.raise_for_status()
    return r.json()


def _model_by_email(email: str) -> dict | None:
    email = email.strip().lower()
    for r in portal._fetch(portal.T_MODELS):
        if str(r["fields"].get("Email") or "").strip().lower() == email:
            return r
    return None


# ---------------------------------------------------------------- mail

def mail_ready() -> bool:
    return bool(os.environ.get("PORTAL_SMTP_HOST") and os.environ.get("PORTAL_SMTP_FROM"))


def send_mail(to: str, subject: str, text: str, attach: Path | None = None) -> bool:
    """One plain-text mail from info@. Returns False (and logs) when SMTP is not configured yet."""
    if not mail_ready():
        print(f"[onboarding] SMTP not configured; would mail {to}: {subject}")
        return False
    msg = EmailMessage()
    msg["From"] = os.environ["PORTAL_SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if attach and attach.exists():
        msg.add_attachment(attach.read_bytes(), maintype="application", subtype="pdf", filename=attach.name)
    host, port = os.environ["PORTAL_SMTP_HOST"], int(os.environ.get("PORTAL_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        if os.environ.get("PORTAL_SMTP_USER"):
            s.login(os.environ["PORTAL_SMTP_USER"], os.environ.get("PORTAL_SMTP_PASSWORD", ""))
        s.send_message(msg)
    return True


def _first(name: str) -> str:
    return (name or "").strip().split(" ")[0] or "du"


def contract_mail(name: str, sign_url: str) -> tuple[str, str]:
    return (f"Dein Northfeed-Vertrag, {_first(name)}",
            f"Hi {_first(name)},\n\nschön, dass du dabei bist. Hier ist dein Management-Vertrag zum Durchlesen und Unterschreiben:\n\n{sign_url}\n\n"
            f"Wir haben schon unterschrieben. Du musst nur noch die Felder prüfen und unterschreiben.\n"
            f"Direkt danach bekommst du eine zweite Mail mit deinem Portal-Zugang.\n\n"
            f"Dein Northfeed-Team\n")   # 18.11 Rene: "wir sind ein Team", no time estimate, no WhatsApp line


def welcome_mail(name: str, email: str, password: str, links: dict) -> tuple[str, str]:
    """15.11 Rene: no Drive links in the mail, the portal and the tutorials carry everything; the mail only
    opens the door and names the one next step. 16.11: says that the portal opens after the onboarding videos."""
    return (f"Willkommen bei Northfeed, {_first(name)}",
            f"Hi {_first(name)},\n\nder Vertrag ist durch, willkommen im Team!\n\n"
            f"Dein Portal: {PORTAL_URL}\nLogin: {email}\nPasswort: {password}\n\n"
            f"So geht es los: Wenn du dich einloggst, siehst du zuerst nur deine Onboarding-Videos. Schau sie dir bitte der Reihe nach "
            f"komplett an. Sie erklären dir, wie unsere Zusammenarbeit läuft, wo du was findest und was deine Aufgaben sind.\n\n"
            f"Sobald du alle Videos angeschaut hast, wird dein ganzes Portal automatisch freigeschaltet: dein Plan, deine Skripte, "
            f"deine Aufgaben und alle Links. Vorher macht das noch keinen Sinn, weil du erst verstehen sollst, wie alles zusammenhängt.\n\n"
            f"Die Videos bleiben danach im Portal unter „Onboarding“, falls du später etwas nachschauen willst.\n\n"
            f"Lade dir am besten gleich die Google-Drive-App aufs Handy, darüber lädst du später alles hoch.\n\n"
            f"Und falls du noch keins hast: Bitte bestell dir gleich bei Amazon ein Stativ mit Ringlicht und Selbstauslöser. "
            f"Das brauchst du für deine ersten Skripte.\n\n"   # 17.11 Rene: equipment in the mail, not in the onboarding videos
            f"Wichtig sind die Fristen: Steckbrief, Bilder und Zugänge erledigst du bitte in den ersten 3 Tagen, "
            f"und deine ersten zwei Skripte in den ersten 7 Tagen. Im Portal steht bei jeder Aufgabe das Datum dazu.\n\n"
            f"Dein nächster Schritt: Einloggen und das erste Video ansehen.\n\n"
            f"Dein Northfeed-Team\n")


# ---------------------------------------------------------------- stage side effects

def send_contract(rec: str) -> dict:
    """Stage "Vertrag offen": create the DocuSeal submission and mail the model her link."""
    from . import docuseal
    m = _model(rec)
    f = m["fields"]
    missing = [k for k in ("Legal name", "Birthdate", "Email") if not f.get(k)]
    if not ((f.get("Street") and f.get("City")) or f.get("Address")):
        missing.append("Straße + PLZ + Ort")
    if missing:
        raise ValueError("Vertrag braucht noch: " + ", ".join(missing))
    if not f.get("Package"):
        _patch(rec, {"Package": "Full Management", "Management split %": 60})
        f["Package"], f["Management split %"] = "Full Management", 60
    sub = docuseal.create_submission(m)                     # Rene's part is completed with the stored signature
    model_url = sub.get("sign_url", "")
    fields = {"Contract sent": dt.date.today().isoformat(), "Stage": "Vertrag offen"}
    if sub.get("submission_id"):
        fields["Notes"] = (str(f.get("Notes") or "") + f"\nDocuSeal submission {sub['submission_id']}").strip()
    _patch(rec, fields)
    try:
        from . import contract_data
        contract_data.close_review_task(rec)
    except Exception as e:  # noqa: BLE001
        print("[onboarding] review task:", e)
    mailed = False
    if model_url:
        subj, text = contract_mail(_s(f.get("Name")), model_url)
        mailed = send_mail(str(f.get("Email")), subj, text)
    return {"ok": True, "submission": sub.get("submission_id"), "signUrl": model_url, "agencySigned": sub.get("agency_completed"), "mailed": mailed}


def _s(v) -> str:
    return "" if v is None else str(v)


def _store_pdf(model_name: str, pdf_path: str) -> str:
    """Copy the signed PDF into MODELS/<Name>/Intern/Vertrag and return its Drive link."""
    name = drive_tree._safe(model_name)
    dest = f"{drive_tree.ROOT}/{name}/Intern/Vertrag"
    fname = f"Vertrag_{name}_{dt.date.today().isoformat()}.pdf"
    subprocess.run(["rclone", "copyto", pdf_path, f"{drive_tree.REMOTE}:{dest}/{fname}"], check=True, timeout=300)
    out = subprocess.run(["rclone", "lsjson", "--files-only", f"{drive_tree.REMOTE}:{dest}"], capture_output=True, text=True, timeout=120).stdout
    import json
    for e in json.loads(out or "[]"):
        if e.get("Name") == fname:
            return f"https://drive.google.com/file/d/{e['ID']}/view"
    return ""


CONTRACT_DIR = Path("/opt/northfeed-contracts")


def contract_pdf(rec: str) -> Path | None:
    """The signed contract of one model: local copy, else fetched once from DocuSeal (submission id in Notes)."""
    p = CONTRACT_DIR / f"{rec}.pdf"
    if p.exists():
        return p
    try:
        import re as _re
        from . import docuseal
        notes = _s(_model(rec)["fields"].get("Notes"))
        m = _re.findall(r"DocuSeal submission (\d+)", notes)
        if not m:
            return None
        docs = docuseal._api("GET", f"/submissions/{m[-1]}/documents").json().get("documents") or []
        if not docs:
            return None
        url = docs[0]["url"]
        pub, internal = docuseal._cfg("DOCUSEAL_PUBLIC_URL"), docuseal._cfg("DOCUSEAL_URL", "http://127.0.0.1:3800")
        if pub and url.startswith(pub):
            url = internal + url[len(pub):]
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        return p
    except Exception as e:  # noqa: BLE001
        print("[onboarding] contract pdf:", e)
        return None


def contract_signed(email: str, pdf_path: str | None, submission_id: str = "") -> dict:
    """Webhook "completed": everything that may only happen after the signature."""
    m = _model_by_email(email)
    if not m:
        raise ValueError(f"no model with e-mail {email}")
    rec, f = m["id"], m["fields"]
    name = _s(f.get("Name")).strip()
    today = dt.date.today().isoformat()
    if f.get("Contract signed") and f.get("Welcome sent"):   # 16.11: DocuSeal retries the webhook; the first run already did everything
        return {"ok": True, "model": name, "rec": rec, "already": True}
    _tg_signed = f"✍️ Vertrag unterschrieben · {name}\nDrive-Ordner, Portal-Zugang und Willkommensmail laufen automatisch.\n{PORTAL_URL}/manager#model/{rec}"
    try:
        from . import cal_booking
        cal_booking._tg(_tg_signed)
    except Exception:  # noqa: BLE001
        pass
    tree = drive_tree.ensure_tree(name, email=email, model_rec_id=rec)
    from . import scripts
    scripts.ensure_folders_later(rec)                        # 17.11 Rene: complete tree incl. Skript 1 + 2
    pdf_link = ""
    if pdf_path and Path(pdf_path).exists():
        try:                                                 # 16.11 Rene: the model sees her contract in the portal ("Dein Konto")
            CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copyfile(pdf_path, CONTRACT_DIR / f"{rec}.pdf")
        except Exception as e:  # noqa: BLE001
            print("[onboarding] local contract copy failed:", e)
        try:
            pdf_link = _store_pdf(name, pdf_path)
        except Exception as e:  # noqa: BLE001
            print("[onboarding] pdf to drive failed:", e)
    password = str(f.get("Portal password") or "") or secrets.token_urlsafe(8)
    fields = {"Stage": "Unterschrieben", "Contract signed": today, "Portal password": password}
    if pdf_link:
        fields["Contract PDF"] = pdf_link
    _patch(rec, fields)
    subj, text = welcome_mail(name, email, password, tree["links"])
    mailed = send_mail(email, subj, text, Path(pdf_path) if pdf_path and Path(pdf_path).exists() else None)
    if mailed:
        _patch(rec, {"Welcome sent": today, "Stage": "Onboarding"})
    return {"ok": True, "model": name, "rec": rec, "drive": tree["links"]["root"], "pdf": pdf_link, "mailed": mailed}


def send_access(rec: str) -> dict:
    """15.11 Rene (E): portal access for a model without a contract (existing models). Password kept or created,
    stage moved past the contract gate, welcome mail sent now; the Drive tree is built in the background."""
    import threading
    from .portal_auth import PRE_SIGN_STAGES
    m = _model(rec)
    f = m["fields"]
    email = _s(f.get("Email")).strip()
    if not email:
        raise ValueError("Model hat keine E-Mail")
    name = _s(f.get("Name")).strip()
    password = str(f.get("Portal password") or "") or secrets.token_urlsafe(8)
    fields = {"Portal password": password}
    if _s(f.get("Stage")) in PRE_SIGN_STAGES or not f.get("Stage"):
        fields["Stage"] = "Onboarding"
    if f.get("Status") not in ("Active", "Paused"):
        fields["Status"] = "Onboarding"
    _patch(rec, fields)

    def _drive():
        try:
            drive_tree.ensure_tree(name, email=email, model_rec_id=rec)
        except Exception as e:  # noqa: BLE001
            print("[onboarding] access drive failed:", e)
    threading.Thread(target=_drive, daemon=True).start()
    subj, text = welcome_mail(name, email, password, {})
    subj = subj.replace("Willkommen bei Northfeed", "Dein Zugang zu Northfeed")
    text = text.replace("der Vertrag ist durch, willkommen im Team!", "hier ist dein Zugang zu unserem Portal.")
    mailed = send_mail(email, subj, text)
    if mailed:
        _patch(rec, {"Welcome sent": dt.date.today().isoformat()})
    return {"ok": True, "mailed": mailed, "stage": fields.get("Stage", f.get("Stage"))}


# ---------------------------------------------------------------- SMTP setup from the manager page

ENV_FILE = Path(os.environ.get("ADBBOT_ENV_FILE", "/etc/adbbot/env"))


def store_smtp(user: str, password: str) -> dict:
    """Write PORTAL_SMTP_* for Gmail (info@) into the env file and the running process, then send a test mail
    to the same address. The password never leaves the server; the response only says whether it worked."""
    user = user.strip().lower()
    password = password.replace(" ", "").strip()
    if "@" not in user or len(password) < 12:
        raise ValueError("E-Mail oder App-Passwort fehlt (App-Passwort hat 16 Zeichen)")
    vals = {"PORTAL_SMTP_HOST": "smtp.gmail.com", "PORTAL_SMTP_PORT": "587", "PORTAL_SMTP_USER": user,
            "PORTAL_SMTP_PASSWORD": password, "PORTAL_SMTP_FROM": f"\"Northfeed <{user}>\""}
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if not any(ln.startswith(k + "=") for k in vals)]
    lines += [f"{k}={v}" for k, v in vals.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(vals)
    try:
        send_mail(user, "Northfeed Portal: Mailversand eingerichtet", "Diese Mail kommt vom Portal-Server. Verträge und Logins gehen ab jetzt von dieser Adresse raus.")
    except Exception as e:  # noqa: BLE001
        for k in vals:
            os.environ.pop(k, None)
        raise ValueError(f"Gmail hat den Login abgelehnt: {str(e)[:120]}")
    return {"ok": True, "from": vals["PORTAL_SMTP_FROM"]}
