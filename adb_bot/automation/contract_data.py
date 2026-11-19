"""Models enter their own contract data (15.11, Rene: "dass ich das nicht machen muss").

    request(rec)      -> signed link https://portal.northfeed-management.com/vertragsdaten/<token>, mailed from info@
    page(token)       -> public form (no login): legal name, birth date, street, ZIP, city, country, e-mail, WhatsApp
    submit(token, f)  -> Airtable, "Contract data submitted" = today, Telegram to Rene + a task "prüfen -> Vertrag senden"

Rene checks the data on the model page and presses "Vertrag senden"; the rest is the normal contract flow.
The token is an HMAC over the record id and an expiry (21 days) with the portal secret; it opens only this form.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import html
import time

from . import portal, portal_auth

BASE_URL = "https://portal.northfeed-management.com/vertragsdaten/"
TTL = 21 * 86400
F_SUBMITTED = "Contract data submitted"


def _sig(msg: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(portal_auth._secret(), msg.encode(), hashlib.sha256).digest()[:18]).decode().rstrip("=")


def token(rec: str) -> str:
    msg = f"{rec}.{int(time.time()) + TTL}"
    return base64.urlsafe_b64encode(msg.encode()).decode().rstrip("=") + "." + _sig(msg)


def verify(tok: str) -> str | None:
    try:
        body, sig = tok.rsplit(".", 1)
        msg = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        if not hmac.compare_digest(_sig(msg), sig):
            return None
        rec, exp = msg.rsplit(".", 1)
        return rec if int(exp) > time.time() and rec.startswith("rec") else None
    except Exception:  # noqa: BLE001
        return None


def link(rec: str) -> str:
    return BASE_URL + token(rec)


def request(rec: str) -> dict:
    from . import onboarding
    m = onboarding._model(rec)
    f = m["fields"]
    url = link(rec)
    first = onboarding._first(onboarding._s(f.get("Name")))
    mailed = False
    if f.get("Email"):
        mailed = onboarding.send_mail(str(f["Email"]), "Deine Daten für den Northfeed-Vertrag",
            f"Hi {first},\n\nfür deinen Vertrag brauchen wir noch ein paar Angaben: deinen Namen wie im Ausweis, dein Geburtsdatum und deine Adresse.\n\n"
            f"{url}\n\nSobald du das abgeschickt hast, prüfen wir alles und du bekommst den Vertrag zum Unterschreiben.\n\n"
            f"Dein Northfeed-Team\n")   # 18.11 Rene: team voice, no time estimates, no "schreib mir auf WhatsApp"
    return {"ok": True, "link": url, "mailed": mailed, "email": f.get("Email", "")}


FIELDS = [("legal", "Vor- und Nachname (wie im Ausweis)", "text", "Legal name", "name"),
          ("birth", "Geburtsdatum", "date", "Birthdate", "bday"),
          ("street", "Straße und Hausnummer", "text", "Street", "street-address"),
          ("zip", "PLZ", "text", "ZIP", "postal-code"),
          ("city", "Ort", "text", "City", "address-level2"),
          ("country", "Land", "text", "Country", "country-name"),
          ("email", "E-Mail (am besten Gmail, darüber bekommst du deinen Google-Drive-Ordner)", "email", "Email", "email"),
          ("whatsapp", "WhatsApp-Nummer mit Ländervorwahl", "tel", "WhatsApp", "tel")]


def _shell(inner: str) -> bytes:
    logo = ""
    try:
        logo = "data:image/png;base64," + base64.b64encode(portal.LOGO.read_bytes()).decode()
    except Exception:  # noqa: BLE001
        pass
    return f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Northfeed · Vertragsdaten</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;1,9..144,500&family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,700&display=swap">
<style>:root{{color-scheme:dark;--bg:#0b0d12;--panel:#161a22;--line2:#343c4d;--fg:#e6e8ee;--muted:#9aa3b2;--pink:#ff5c9a;--pink2:#ff9ec4}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.5 "Bricolage Grotesque",sans-serif;
background-image:radial-gradient(900px 500px at 85% -10%,rgba(255,92,154,.18),transparent 60%);padding:20px}}
.box{{width:100%;max-width:520px;background:linear-gradient(180deg,rgba(26,23,38,.75),rgba(18,16,28,.85));border:1px solid var(--line2);border-radius:20px;padding:30px 26px}}
.logo{{height:28px;display:block;margin-bottom:24px}}h1{{font-family:Fraunces,serif;font-weight:500;font-size:32px;margin:0 0 6px}}h1 em{{font-style:italic;color:var(--pink2)}}
.lead{{color:var(--muted);margin:0 0 20px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}}.full{{grid-column:1/-1}}
label{{display:block;font-size:13px;color:var(--muted);margin-bottom:14px}}input{{display:block;width:100%;margin-top:6px;background:var(--panel);color:var(--fg);border:1px solid var(--line2);border-radius:12px;padding:12px 14px;font:inherit;font-size:16px}}
input:focus{{outline:2px solid var(--pink);border-color:transparent}}.btn{{width:100%;border:0;border-radius:999px;padding:14px 18px;font:inherit;font-weight:700;font-size:15px;cursor:pointer;color:#fff;background:linear-gradient(135deg,#ff5c9a,#c2416f);margin-top:6px}}
.err{{color:#ff9b9b;font-size:14px;margin:0 0 14px}}.note{{color:var(--muted);font-size:12.5px;margin-top:14px}}@media(max-width:480px){{.grid{{grid-template-columns:1fr}}}}</style></head>
<body><div class="box">{f'<img class="logo" src="{logo}" alt="Northfeed">' if logo else ''}{inner}</div></body></html>'''.encode("utf-8")


def page(tok: str, err: str = "", values: dict | None = None) -> bytes:
    rec = verify(tok)
    if not rec:
        return _shell('<h1>Link <em>abgelaufen</em></h1><p class="lead">Dieser Link ist nicht mehr gültig. Sag deiner Ansprechperson bei Northfeed Bescheid, dann bekommst du einen neuen.</p>')
    from . import onboarding
    f = onboarding._model(rec)["fields"]
    if f.get("Contract sent") or f.get("Contract signed"):
        return _shell('<h1>Schon <em>erledigt</em></h1><p class="lead">Dein Vertrag ist bereits unterwegs. Schau in dein E-Mail-Postfach.</p>')
    v = values or {k: str(f.get(col) or "") for k, _, _, col, _ in FIELDS}
    if not v.get("country"):
        v["country"] = "Deutschland"
    rows = "".join(f'<label class="{"full" if k in ("legal", "street", "email") else ""}">{html.escape(lbl)}<input name="{k}" type="{typ}" autocomplete="{ac}" required value="{html.escape(v.get(k, ""))}"></label>'
                   for k, lbl, typ, _, ac in FIELDS)
    first = html.escape(onboarding._first(str(f.get("Name") or "")))
    return _shell(f'''<h1>Hi {first}, <em>kurz deine Daten</em></h1><p class="lead">Die brauchen wir für deinen Vertrag. Danach prüfen wir alles und du bekommst den Vertrag zum Unterschreiben per Mail.</p>
{f'<p class="err">{html.escape(err)}</p>' if err else ''}<form method="post" action="/vertragsdaten/{html.escape(tok)}"><div class="grid">{rows}</div><button class="btn" type="submit">Daten abschicken</button></form>
<p class="note">Deine Angaben stehen nur im Vertrag und werden nicht veröffentlicht.</p>''')


def submit(tok: str, form: dict) -> bytes:
    rec = verify(tok)
    if not rec:
        return page(tok)
    from . import manager, onboarding
    v = {k: str((form.get(k) or [""])[0] if isinstance(form.get(k), list) else form.get(k) or "").strip() for k, *_ in FIELDS}
    missing = [lbl.split(" (")[0] for k, lbl, *_ in FIELDS if not v[k]]
    if missing:
        return page(tok, "Bitte noch ausfüllen: " + ", ".join(missing), v)
    try:
        bd = dt.date.fromisoformat(v["birth"])
        today = dt.date.today()
        if (today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))) < 18:
            return page(tok, "Du musst mindestens 18 Jahre alt sein.", v)
    except ValueError:
        return page(tok, "Bitte das Geburtsdatum prüfen.", v)
    if "@" not in v["email"]:
        return page(tok, "Bitte die E-Mail-Adresse prüfen.", v)
    fields = {col: v[k] for k, _, _, col, _ in FIELDS}
    fields["Email"] = fields["Email"].lower()
    fields[F_SUBMITTED] = dt.date.today().isoformat()
    onboarding._patch(rec, fields)
    name = str(onboarding._model(rec)["fields"].get("Name") or v["legal"])
    try:
        manager._create(portal.T_TASKS, {"Task": f"Vertragsdaten von {name} prüfen, dann Vertrag senden", "Model": [rec], "Category": "Admin",
                                         "Status": "Open", "Priority": "High", "Assigned to": "Team", "Due": dt.date.today().isoformat(),
                                         "Due type": "Fixed day", "Visible to model": False,
                                         "Description": "Das Model hat die Daten selbst eingetragen. Model-Seite öffnen, Daten ansehen, „Vertrag senden“."})
    except Exception as e:  # noqa: BLE001
        print("[contract_data] task failed:", e)
    try:
        from . import cal_booking
        cal_booking._tg(f"📝 Vertragsdaten eingetragen · {name}\n{v['legal']} · {v['zip']} {v['city']}\nPrüfen und Vertrag senden: https://portal.northfeed-management.com/manager#model/{rec}")
    except Exception:  # noqa: BLE001
        pass
    return _shell(f'<h1>Danke, <em>{html.escape(onboarding._first(name))}</em></h1><p class="lead">Deine Daten sind angekommen. Wir prüfen sie kurz, dann bekommst du den Vertrag zum Unterschreiben an {html.escape(fields["Email"])}.</p>')


def close_review_task(rec: str) -> None:
    """Called when the contract goes out: the "prüfen" task for this model is done."""
    from . import manager
    for r in portal._fetch(portal.T_TASKS):
        f = r["fields"]
        if rec in (f.get("Model") or []) and str(f.get("Task", "")).startswith("Vertragsdaten von") and f.get("Status") != "Done":
            manager._update(portal.T_TASKS, r["id"], {"Status": "Done"})
