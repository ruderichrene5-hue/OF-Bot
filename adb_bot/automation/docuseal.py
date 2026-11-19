"""Contract e-signature for Northfeed models via self-hosted DocuSeal (15.11, Rene: "automatisches
Unterschreiben, Verträge, bevor man sich einloggt"). DocuSeal runs as docker on 127.0.0.1:3800
(systemd docuseal.service, data /opt/docuseal), public name sign.northfeed-management.com.
Template "Managementvertrag Nordfield" has two submitters in fixed order: "Northfeed" (the agency,
pre-filled and auto-completed with the stored signature image) and "Creatorin" (the model, signs
second). Config lives in /etc/northfeed/env (DOCUSEAL_URL, DOCUSEAL_PUBLIC_URL, DOCUSEAL_API_TOKEN,
DOCUSEAL_TEMPLATE_ID, DOCUSEAL_WEBHOOK_SECRET, DOCUSEAL_SIGNER_NAME, DOCUSEAL_SIGNATURE_PNG).

    create_submission(model_rec) -> {"submission_id", "sign_url", "model_submitter_id", "agency_completed"}
    verify_webhook(headers, body) -> bool
    handle_webhook(payload) -> dict | None   (form.completed of the model -> signed PDF on disk)
    template_status() -> dict                 (health check)
No Airtable writes here; the caller sets Contract sent / signed / PDF.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import tempfile
from pathlib import Path

import requests

ENV_FILE = Path("/etc/northfeed/env")
TIMEOUT = 60
PACKAGE_SPLIT = {"Full Management": 60, "Marketing": 40, "Chatting": 40}


def _env() -> dict:
    """Process env, overridden by /etc/northfeed/env (the file is the source of truth; a stale value
    sourced into a shell must not win over it)."""
    out = {k: v for k, v in os.environ.items() if k.startswith("DOCUSEAL_")}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip().startswith("DOCUSEAL_"):
                    out[k.strip()] = v.strip()
    return out


def _cfg(key: str, default: str = "") -> str:
    return _env().get(key, default)


def _api(method: str, path: str, **kw) -> requests.Response:
    r = requests.request(method, _cfg("DOCUSEAL_URL", "http://127.0.0.1:3800") + "/api" + path,
                         headers={"X-Auth-Token": _cfg("DOCUSEAL_API_TOKEN")}, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _date_de(v: str) -> str:
    try:
        return dt.date.fromisoformat(v[:10]).strftime("%d.%m.%Y")
    except Exception:
        return v


def _address(f: dict) -> str:
    """Street, ZIP City, Country from the four fields (15.11 Rene); falls back to the free-text Address."""
    street, zip_, city, country = (_s(f.get(k)) for k in ("Street", "ZIP", "City", "Country"))
    if street or city:
        parts = [street, " ".join(x for x in (zip_, city) if x), country]
        return ", ".join(x for x in parts if x)
    return " ".join(_s(f.get("Address")).split())


def _signature_data_uri() -> str | None:
    p = Path(_cfg("DOCUSEAL_SIGNATURE_PNG", "/opt/docuseal/rene_signature.png"))
    if not p.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def template_status() -> dict:
    """Health check: template reachable, two submitters, 14 fields, signature image present."""
    t = _api("GET", f"/templates/{_cfg('DOCUSEAL_TEMPLATE_ID', '3')}").json()
    return {"ok": [s["name"] for s in t.get("submitters", [])] == ["Northfeed", "Creatorin"] and len(t.get("fields", [])) >= 14,
            "template": t.get("name"), "fields": len(t.get("fields", [])), "submitters": [s["name"] for s in t.get("submitters", [])],
            "signature_png": _signature_data_uri() is not None, "archived": bool(t.get("archived_at"))}


def create_submission(model_rec: dict) -> dict:
    """model_rec = Airtable Models record ({"id", "fields": {...}}) or just its fields dict.
    Returns the model's signing link; the agency part is completed automatically with the stored signature."""
    f = model_rec.get("fields", model_rec)
    email = _s(f.get("Email")).lower()
    if not email:
        raise ValueError("model has no Email")
    legal = _s(f.get("Legal name")) or _s(f.get("Name"))
    package = _s(f.get("Package")) or "Full Management"
    split = f.get("Management split %") or PACKAGE_SPLIT.get(package, 60)
    signer = _cfg("DOCUSEAL_SIGNER_NAME", "Rene Ruderich")
    today = dt.date.today().strftime("%d.%m.%Y")
    agency_values = {"Vertreter Management": signer, "Vertreter Management 2": signer, "Ort Datum Management": f"Albuquerque, {today}"}
    sig = _signature_data_uri()
    if sig:
        agency_values["Unterschrift Management"] = sig
    model_values = {"Name Model": legal, "Name Model 2": legal, "Geburtsdatum": _date_de(_s(f.get("Birthdate"))),
                    "Anschrift": _address(f), "E-Mail": email, "Paket": package,
                    "Anteil Management": str(int(split)), "Nachweis-Kanal": "WhatsApp"}       # 15.11 Rene: always WhatsApp
    body = {"template_id": int(_cfg("DOCUSEAL_TEMPLATE_ID", "3")), "send_email": False, "order": "preserved",
            "submitters": [
                {"role": "Northfeed", "email": _cfg("DOCUSEAL_ADMIN_EMAIL", "info@northfeed-management.com"), "name": signer,
                 "values": agency_values, "completed": bool(sig)},
                {"role": "Creatorin", "email": email, "name": legal, "values": model_values,
                 "metadata": {"airtable_id": model_rec.get("id", "")}}]}
    subs = _api("POST", "/submissions", json=body).json()
    model_sub = next(s for s in subs if s.get("role") == "Creatorin")
    agency_sub = next(s for s in subs if s.get("role") == "Northfeed")
    return {"submission_id": model_sub["submission_id"], "model_submitter_id": model_sub["id"],
            "sign_url": f"{_cfg('DOCUSEAL_PUBLIC_URL', 'https://sign.northfeed-management.com')}/s/{model_sub['slug']}",
            "agency_completed": agency_sub.get("status") == "completed"}


def create_chatter_submission(c: dict) -> dict:
    """18.11 Rene: chatter agreement (template DOCUSEAL_CHATTER_TEMPLATE_ID, roles Northfeed + Contractor). c = chatter_portal row
    plus "email". Company part is completed with the stored signature; the chatter fills birth date, address, ID no., place/date."""
    from . import shifts
    email = _s(c.get("email")).lower()
    if not email:
        raise ValueError("chatter has no e-mail")
    signer = _cfg("DOCUSEAL_SIGNER_NAME", "Rene Ruderich")
    today = dt.date.today()
    sch = shifts.schedule(c.get("shift", "")) or {}
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    rest = ", ".join(days[d] for d in sorted(sch.get("off") or [])) or "agreed with the supervisor"
    shift = (f"{sch['start'] // 60:02d}:{sch['start'] % 60:02d}–{(sch['end'] // 60) % 24 or 24:02d}:{sch['end'] % 60:02d} Manila time" if sch else (c.get("shift") or "to be agreed"))
    company = {"Date": today.strftime("%B %d, %Y"), "Company representative": signer, "Signer name": signer,
               "Company place date": f"Albuquerque, {today.strftime('%B %d, %Y')}"}
    sig = _signature_data_uri()
    if sig:
        company["Company signature"] = sig
    contractor = {"Contractor name": c.get("name", ""), "Contractor name 2": c.get("name", ""), "E-mail": email, "Shift": shift, "Rest day": rest,
                  "Base fee USD": str(int(c.get("base") or 200)), "Commission percent": f"{float(c.get('commission') or 3):g}",
                  "Start date": today.strftime("%B %d, %Y")}
    body = {"template_id": int(_cfg("DOCUSEAL_CHATTER_TEMPLATE_ID", "12")), "send_email": False, "order": "preserved",
            "submitters": [{"role": "Northfeed", "email": _cfg("DOCUSEAL_ADMIN_EMAIL", "info@northfeed-management.com"), "name": signer,
                            "values": company, "completed": bool(sig)},
                           {"role": "Contractor", "email": email, "name": c.get("name", ""), "values": contractor,
                            "metadata": {"airtable_id": c.get("id", ""), "kind": "chatter"}}]}
    subs = _api("POST", "/submissions", json=body).json()
    me = next(x for x in subs if x.get("role") == "Contractor")
    return {"submission_id": me["submission_id"], "sign_url": f"{_cfg('DOCUSEAL_PUBLIC_URL', 'https://sign.northfeed-management.com')}/s/{me['slug']}"}


def verify_webhook(headers, body: bytes | str) -> bool:
    """DocuSeal signs nothing; it can send a fixed secret header (Settings -> Webhooks -> Secret).
    With DOCUSEAL_WEBHOOK_SECRET set, that header must match. handle_webhook re-reads the submission
    from the API anyway, so a forged call can at most trigger a harmless re-check."""
    secret = _cfg("DOCUSEAL_WEBHOOK_SECRET")
    if not secret:
        return True
    get = headers.get if hasattr(headers, "get") else dict(headers).get
    return (get("X-Webhook-Secret") or get("x-webhook-secret") or "") == secret


def handle_webhook(payload: dict) -> dict | None:
    """Returns None for events we ignore; for the model's form.completed (or submission.completed)
    returns {"model_email", "submission_id", "pdf_path", "completed_at", "airtable_id"} with the signed PDF
    downloaded to a temp file. Everything is re-read from the API, the payload is only a pointer."""
    ev = payload.get("event_type", "")
    data = payload.get("data") or {}
    if ev == "form.completed":
        if data.get("role") not in ("Creatorin", "Contractor"):           # 18.11: Contractor = chatter agreement
            return None
        sid = data.get("submission_id")
    elif ev == "submission.completed":
        sid = data.get("id")
    else:
        return None
    sub = _api("GET", f"/submissions/{sid}").json()
    model = next((s for s in sub.get("submitters", []) if s.get("role") in ("Creatorin", "Contractor")), None)
    if not model or model.get("status") != "completed":
        return None
    docs = _api("GET", f"/submissions/{sid}/documents").json().get("documents") or []
    if not docs:
        return None
    url = docs[0]["url"]
    pub, internal = _cfg("DOCUSEAL_PUBLIC_URL"), _cfg("DOCUSEAL_URL", "http://127.0.0.1:3800")
    if pub and url.startswith(pub):
        url = internal + url[len(pub):]
    pdf = requests.get(url, timeout=TIMEOUT)
    pdf.raise_for_status()
    fd, path = tempfile.mkstemp(prefix="vertrag_", suffix=".pdf")
    os.write(fd, pdf.content)
    os.close(fd)
    return {"model_email": _s(model.get("email")).lower(), "submission_id": sid, "pdf_path": path,
            "completed_at": model.get("completed_at"), "airtable_id": (model.get("metadata") or {}).get("airtable_id", ""), "role": model.get("role")}
