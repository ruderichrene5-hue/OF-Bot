"""Weekly billing (15.11, Rene "I"). Contract § 7/§ 8: period Mon-Sun, invoice on Monday, transfer + proof by
Wednesday 23:59, credited by Friday; the very first invoice is due on the Wednesday of the third week after the
contract started; without revenue data the management may estimate from the last four weeks.

Airtable "Abrechnungen" (one row per model and week). Flow:
    Monday 09:00 (timer)  rows for last week are created for every model in stage "Aktiv"; Telegram to Rene.
    Manager tab           Rene enters the net revenue (or "Schätzung"), presses "Rechnung senden":
                          PDF (headless Chromium) -> mail from info@ with PDF -> Drive Intern/Abrechnung -> portal.
    Portal /abrechnung    model sees amount, bank details, due date, downloads the PDF, uploads the transfer proof
                          (or a revenue screenshot); Rene gets Telegram, status "Nachweis da".
    Thursday 00:30        no proof by the deadline -> reminder mail to the model + Telegram to Rene.
    Saturday 09:00        still not paid -> status "Überfällig" + Telegram (contract § 9: services may pause).
Bank details and the tax note come from /etc/northfeed/env (INVOICE_*), set by Rene in the manager settings.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import subprocess
from pathlib import Path

from . import portal

T_BILL = "tbl7ULFAhsky2PHWU"
ENV = Path("/etc/northfeed/env")
FILES = Path("/opt/northfeed-billing")
RENDER = Path("/root/northfeed-invoices")          # snap Chromium can only write below /root
COUNTER = FILES / "invoice_counter.json"
ISSUER = ["Multi-Ecom Empire LLC", "handelnd als Northfeed Management", "1209 Mountain Road PL NE, STE N",
          "Albuquerque, NM 87110, USA", "info@northfeed-management.com"]
SETTINGS = {"holder": "INVOICE_HOLDER", "iban": "INVOICE_IBAN", "bic": "INVOICE_BIC", "bank": "INVOICE_BANK", "taxnote": "INVOICE_TAX_NOTE"}
DEFAULT_TAXNOTE = "Keine Umsatzsteuer ausgewiesen: leistendes Unternehmen ist die Multi-Ecom Empire LLC mit Sitz in den USA."   # 15.11 Rene
ACCOUNTS = Path("/etc/northfeed/bank_accounts.json")   # 15.11 Rene: several accounts, labelled, one active; never typed by Claude


# ---------------------------------------------------------------- helpers

def _accounts() -> list:
    try:
        return json.loads(ACCOUNTS.read_text())
    except Exception:  # noqa: BLE001
        return []


def _save_accounts(a: list) -> None:
    ACCOUNTS.write_text(json.dumps(a, ensure_ascii=False, indent=1))
    os.chmod(ACCOUNTS, 0o600)


def _taxnote() -> str:
    if ENV.exists():
        for l in ENV.read_text().splitlines():
            if l.startswith("INVOICE_TAX_NOTE="):
                return l.split("=", 1)[1].strip().strip('"') or DEFAULT_TAXNOTE
    return DEFAULT_TAXNOTE


def _cfg() -> dict:
    """Active bank account + tax note. Each sent invoice keeps its own snapshot (<no>.json), so switching accounts
    later never changes what an old invoice or the portal shows for it."""
    act = next((a for a in _accounts() if a.get("active")), {})
    return {"label": act.get("label", ""), "holder": act.get("holder", ""), "iban": act.get("iban", ""), "bic": act.get("bic", ""),
            "bank": act.get("bank", ""), "country": act.get("country", ""), "taxnote": _taxnote()}


def bank_ready() -> bool:
    c = _cfg()
    return bool(c["holder"] and c["iban"])


def _snap(no: str) -> dict:
    p = FILES / f"{no}.json"
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return _cfg()


def _mask(iban: str) -> str:
    x = iban.replace(" ", "")
    return f"{x[:4]} … {x[-4:]}" if len(x) > 8 else x


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _kw(d: dt.date) -> int:
    return d.isocalendar()[1]


def _de(d: str | dt.date) -> str:
    if isinstance(d, str):
        try:
            d = dt.date.fromisoformat(d[:10])
        except ValueError:
            return d
    return d.strftime("%d.%m.%Y")


def _eur(v) -> str:
    try:
        return f"{float(v):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "–"


def _rows(formula: str | None = None) -> list:
    return portal._fetch(T_BILL, formula)


def _models() -> dict:
    return {r["id"]: r["fields"] for r in portal.fetch_cached(portal.T_MODELS, 30)}   # 16.11: Abrechnung tab was slow; edits bust the cache


def _tg(text: str) -> None:
    from . import cal_booking
    cal_booking._tg(text)


def _due(week_start: dt.date, contract_signed: str) -> dt.date:
    normal = week_start + dt.timedelta(days=9)                      # Wednesday of the invoice week
    if contract_signed:
        try:
            first = _monday(dt.date.fromisoformat(contract_signed[:10])) + dt.timedelta(days=16)   # Wednesday of week 3
            return max(normal, first)
        except ValueError:
            pass
    return normal


# ---------------------------------------------------------------- weekly rows

def ensure_week(week_start: dt.date | None = None) -> list:
    """Create the rows for one week (default: last week) for every model in stage "Aktiv". Idempotent."""
    from . import manager
    ws = week_start or (_monday(dt.date.today()) - dt.timedelta(days=7))
    have = {(tuple(r["fields"].get("Model") or []), r["fields"].get("Week start")) for r in _rows()}
    made = []
    for rec, f in _models().items():
        if str(f.get("Stage") or "") != "Aktiv" or f.get("Alte Agentur"):   # 16.11 Rene: old-agency models get no Northfeed invoice
            continue
        if ((rec,), ws.isoformat()) in have:
            continue
        r = manager._create(T_BILL, {"Name": f"{f.get('Name', '')} · KW {_kw(ws)}", "Model": [rec], "Week start": ws.isoformat(),
                                     "Share %": f.get("Management split %") or 60, "Status": "Offen",
                                     "Due": _due(ws, str(f.get("Contract signed") or "")).isoformat()})
        made.append(r["id"])
    return made


def estimate(rec: str, before: str) -> float | None:
    vals = sorted(((r["fields"].get("Week start"), r["fields"].get("Net revenue")) for r in _rows()
                   if rec in (r["fields"].get("Model") or []) and r["fields"].get("Net revenue") is not None
                   and str(r["fields"].get("Week start") or "") < before), reverse=True)[:4]
    return round(sum(v for _, v in vals) / len(vals), 2) if vals else None


# ---------------------------------------------------------------- invoice

def _next_no() -> str:
    FILES.mkdir(parents=True, exist_ok=True)
    year = dt.date.today().year
    c = json.loads(COUNTER.read_text()) if COUNTER.exists() else {}
    n = int(c.get(str(year), 0)) + 1
    c[str(year)] = n
    COUNTER.write_text(json.dumps(c))
    return f"NF-{year}-{n:04d}"


def _address(f: dict) -> list:
    street, zip_, city, country = (str(f.get(k) or "").strip() for k in ("Street", "ZIP", "City", "Country"))
    if street or city:
        return [x for x in (street, " ".join(x for x in (zip_, city) if x), country) if x]
    return [x.strip() for x in str(f.get("Address") or "").replace(",", "\n").splitlines() if x.strip()]


def invoice_html(no: str, bill: dict, model: dict, cfg: dict) -> str:
    ws = dt.date.fromisoformat(bill["Week start"])
    we = ws + dt.timedelta(days=6)
    name = model.get("Legal name") or model.get("Name", "")
    rows = "".join(f"<div>{html.escape(x)}</div>" for x in [name] + _address(model))
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><style>
@page{{size:A4;margin:22mm 20mm}}body{{font-family:Georgia,"Times New Roman",serif;color:#111;font-size:10.5pt;line-height:1.45}}
.top{{display:flex;justify-content:space-between;align-items:flex-start}}.brand{{font-size:20pt;letter-spacing:.3px}}.brand em{{color:#c2416f;font-style:italic}}
.small{{font-size:8.5pt;color:#555}}h1{{font-size:15pt;margin:26mm 0 4mm}}table{{width:100%;border-collapse:collapse;margin-top:6mm}}
th,td{{text-align:left;padding:3mm 2mm;border-bottom:1px solid #ccc;vertical-align:top}}th{{font-size:8.5pt;text-transform:uppercase;letter-spacing:.06em;color:#555}}
td.r,th.r{{text-align:right}}.sum td{{border-bottom:2px solid #111;font-weight:bold}}.box{{margin-top:9mm;padding:4mm;border:1px solid #ccc}}</style></head><body>
<div class="top"><div class="brand">North<em>feed</em></div><div class="small" style="text-align:right">{"<br>".join(html.escape(x) for x in ISSUER)}</div></div>
<div class="small" style="margin-top:14mm">{html.escape(ISSUER[0])} · {html.escape(ISSUER[2])} · {html.escape(ISSUER[3])}</div>
<div style="margin-top:2mm">{rows}</div>
<h1>Rechnung {html.escape(no)}</h1>
<div>Rechnungsdatum: {_de(dt.date.today())}<br>Leistungszeitraum: {_de(ws)} bis {_de(we)} (KW {_kw(ws)})</div>
<table><thead><tr><th>Leistung</th><th class="r">Betrag</th></tr></thead><tbody>
<tr><td>Management-Vergütung gemäß § 7 Managementvertrag<br><span class="small">{int(bill.get("Share %") or 60)} % des Nettoumsatzes von {_eur(bill.get("Net revenue"))}{" (geschätzt nach § 8 Abs. 1)" if bill.get("Revenue source") == "Schätzung" else ""}</span></td><td class="r">{_eur(bill.get("Amount"))}</td></tr>
<tr class="sum"><td>Rechnungsbetrag</td><td class="r">{_eur(bill.get("Amount"))}</td></tr></tbody></table>
<p class="small" style="margin-top:4mm">{html.escape(cfg["taxnote"])}</p>
<div class="box"><b>Zahlbar bis {_de(bill["Due"])}, 23:59 Uhr</b> per SEPA-Überweisung:<br>
Kontoinhaber: {html.escape(cfg["holder"])}<br>IBAN: {html.escape(cfg["iban"])}{"<br>BIC: " + html.escape(cfg["bic"]) if cfg["bic"] else ""}{"<br>Bank: " + html.escape(cfg["bank"]) if cfg["bank"] else ""}<br>
Verwendungszweck: {html.escape(no)}<br><br>Bitte schick den Überweisungsnachweis bis zum Fälligkeitstag per WhatsApp oder lade ihn im Portal unter „Abrechnung“ hoch.</div>
</body></html>"""


def _render_pdf(no: str, doc: str) -> Path:
    RENDER.mkdir(parents=True, exist_ok=True)
    src, out = RENDER / f"{no}.html", RENDER / f"{no}.pdf"
    src.write_text(doc, encoding="utf-8")
    subprocess.run(["timeout", "90", "chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out}", src.as_uri()], capture_output=True)
    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError("PDF konnte nicht erzeugt werden")
    dest = FILES / f"{no}.pdf"
    dest.write_bytes(out.read_bytes())
    return dest


def _to_drive(model_name: str, local: Path, fname: str) -> str:
    from . import drive_tree
    name = drive_tree._safe(model_name)
    folder = f"{drive_tree.ROOT}/{name}/Intern/Abrechnung"
    try:
        drive_tree.ensure_folder(f"{drive_tree.ROOT}/{name}/Intern", "Abrechnung")
        subprocess.run(["rclone", "copyto", str(local), f"{drive_tree.REMOTE}:{folder}/{fname}"], check=True, timeout=300)
        out = subprocess.run(["rclone", "lsjson", "--files-only", f"{drive_tree.REMOTE}:{folder}"], capture_output=True, text=True, timeout=120).stdout
        for e in json.loads(out or "[]"):
            if e.get("Name") == fname:
                return f"https://drive.google.com/file/d/{e['ID']}/view"
    except Exception as e:  # noqa: BLE001
        print("[billing] drive copy failed:", e)
    return ""


def send_invoice(bill_id: str) -> dict:
    from . import manager, onboarding
    if not bank_ready():
        raise ValueError("Bankdaten fehlen: Einstellungen → Abrechnung")
    b = _get(bill_id)["fields"]
    if b.get("Amount") in (None, ""):
        raise ValueError("Erst den Nettoumsatz eintragen")
    rec = (b.get("Model") or [""])[0]
    m = onboarding._model(rec)["fields"]
    if not m.get("Email"):
        raise ValueError("Model hat keine E-Mail")
    no = b.get("Invoice no") or _next_no()
    cfg = _cfg()
    FILES.mkdir(parents=True, exist_ok=True)
    (FILES / f"{no}.json").write_text(json.dumps(cfg, ensure_ascii=False))
    os.chmod(FILES / f"{no}.json", 0o600)
    pdf = _render_pdf(no, invoice_html(no, b, m, cfg))
    first = onboarding._first(str(m.get("Name") or ""))
    ws = dt.date.fromisoformat(b["Week start"])
    mailed = onboarding.send_mail(str(m["Email"]), f"Deine Northfeed-Abrechnung KW {_kw(ws)}",
        f"Hi {first},\n\nanbei deine Abrechnung für die Woche {_de(ws)} bis {_de(ws + dt.timedelta(days=6))}.\n\n"
        f"Betrag: {_eur(b.get('Amount'))}\nZahlbar bis: {_de(b['Due'])}, 23:59 Uhr\nVerwendungszweck: {no}\n\n"
        f"Die Bankverbindung steht in der Rechnung und im Portal unter „Abrechnung“. Den Überweisungsnachweis lädst du dort hoch.\n\n"
        f"https://portal.northfeed-management.com/abrechnung\n\nDein Northfeed-Team\n", pdf)
    link = _to_drive(str(m.get("Name") or ""), pdf, f"Rechnung_{no}.pdf")
    manager._update(T_BILL, bill_id, {"Invoice no": no, "Invoice sent": dt.date.today().isoformat(), "Status": "Rechnung raus",
                                      "Notes": f"Konto: {cfg['label']} ({_mask(cfg['iban'])})",
                                      **({"Invoice PDF": link} if link else {})})
    return {"ok": True, "no": no, "mailed": mailed, "drive": link}


def _get(bill_id: str) -> dict:
    import requests
    r = requests.get(f"https://api.airtable.com/v0/{portal.BASE}/{T_BILL}/{bill_id}",
                     headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}"}, timeout=40)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- manager API

def overview(week: str | None = None) -> dict:
    ws = dt.date.fromisoformat(week) if week else _monday(dt.date.today()) - dt.timedelta(days=7)
    models = _models()
    rows = []
    for r in portal.fetch_cached(T_BILL, 20):                # 16.11: warm cache; every write goes through manager._update and busts it
        f = r["fields"]
        rec = (f.get("Model") or [""])[0]
        rows.append({"id": r["id"], "model": rec, "modelName": models.get(rec, {}).get("Name", ""), "week": f.get("Week start", ""),
                     "kw": _kw(dt.date.fromisoformat(f["Week start"])) if f.get("Week start") else "", "net": f.get("Net revenue"),
                     "share": f.get("Share %") or 60, "amount": f.get("Amount"), "status": f.get("Status", "Offen"), "source": f.get("Revenue source", ""),
                     "no": f.get("Invoice no", ""), "sent": f.get("Invoice sent", ""), "due": f.get("Due", ""), "pdf": f.get("Invoice PDF", ""),
                     "revProof": f.get("Revenue proof", ""), "payProof": f.get("Payment proof", ""), "paid": f.get("Paid", "")})
    rows.sort(key=lambda x: (x["week"], x["modelName"]), reverse=True)
    c = _cfg()
    return {"ok": True, "week": ws.isoformat(), "kw": _kw(ws), "rows": rows, "bankReady": bank_ready(),
            "settings": {"taxnote": c["taxnote"]},
            "accounts": [{"id": a["id"], "label": a.get("label", ""), "holder": a.get("holder", ""), "iban": _mask(a.get("iban", "")), "bic": a.get("bic", ""),
                          "bank": a.get("bank", ""), "active": bool(a.get("active"))} for a in _accounts()],
            "active": sum(1 for f in models.values() if str(f.get("Stage") or "") == "Aktiv" and not f.get("Alte Agentur"))}


def _write_env(vals: dict) -> None:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    lines = [l for l in lines if not any(l.startswith(k + "=") for k in vals)]
    lines += [f'{k}="{v}"' for k, v in vals.items()]
    ENV.write_text("\n".join(lines) + "\n")


def manager_api(action: str, body: dict, email: str) -> dict:
    from . import manager
    if action == "billing.overview":
        return overview(body.get("week") or None)
    if action == "billing.settings":                         # settings page: bank accounts + tax note only, no Airtable round trip
        return {"ok": True, "settings": {"taxnote": _taxnote()},
                "accounts": [{"id": a["id"], "label": a.get("label", ""), "holder": a.get("holder", ""), "iban": _mask(a.get("iban", "")), "bic": a.get("bic", ""),
                              "bank": a.get("bank", ""), "active": bool(a.get("active"))} for a in _accounts()]}
    if action == "billing.ensure":
        made = ensure_week(dt.date.fromisoformat(body["week"]) if body.get("week") else None)
        return {"ok": True, "created": len(made)}
    if action == "billing.set":
        b = _get(body["id"])["fields"]
        raw = str(body.get("net", "")).strip().replace("€", "").replace(" ", "")
        if "," in raw:
            raw = raw.replace(".", "").replace(",", ".")
        net = round(float(raw), 2)
        share = float(b.get("Share %") or 60)
        manager._update(T_BILL, body["id"], {"Net revenue": net, "Amount": round(net * share / 100, 2), "Revenue source": body.get("source") or "CRM"})
        return {"ok": True, "amount": round(net * share / 100, 2)}
    if action == "billing.estimate":
        b = _get(body["id"])["fields"]
        v = estimate((b.get("Model") or [""])[0], b.get("Week start", ""))
        if v is None:
            raise ValueError("Keine Vorwochen zum Schätzen")
        return manager_api("billing.set", {"id": body["id"], "net": v, "source": "Schätzung"}, email)
    if action == "billing.send":
        return send_invoice(body["id"])
    if action == "billing.paid":
        manager._update(T_BILL, body["id"], {"Status": "Bezahlt", "Paid": dt.date.today().isoformat()})
        return {"ok": True}
    if action == "billing.settings":                         # tax note only; bank accounts have their own actions
        _write_env({"INVOICE_TAX_NOTE": str(body.get("taxnote", "")).strip().replace('"', "")})
        return {"ok": True}
    if action == "billing.accountAdd":
        holder, iban = str(body.get("holder", "")).strip(), re.sub(r"\s+", "", str(body.get("iban", "")).upper())
        if not holder or len(iban) < 15:
            raise ValueError("Kontoinhaber und IBAN fehlen")
        acc = _accounts()
        new = {"id": f"acc{int(dt.datetime.now().timestamp())}", "label": str(body.get("label", "")).strip() or "Konto",
               "holder": holder, "iban": " ".join(iban[i:i + 4] for i in range(0, len(iban), 4)), "bic": str(body.get("bic", "")).strip().upper(),
               "bank": str(body.get("bank", "")).strip(), "country": str(body.get("country", "")).strip(),
               "active": bool(body.get("active")) or not any(a.get("active") for a in acc), "added": dt.date.today().isoformat()}
        if new["active"]:
            for a in acc:
                a["active"] = False
        acc.append(new)
        _save_accounts(acc)
        return {"ok": True}
    if action == "billing.accountActivate":
        acc = _accounts()
        for a in acc:
            a["active"] = a["id"] == body.get("id")
        _save_accounts(acc)
        return {"ok": True}
    if action == "billing.accountRemove":
        acc = [a for a in _accounts() if a["id"] != body.get("id")]
        if acc and not any(a.get("active") for a in acc):
            acc[0]["active"] = True
        _save_accounts(acc)
        return {"ok": True}
    raise ValueError("unknown billing action")


# ---------------------------------------------------------------- model portal

def model_page(rec: str, name: str, ok: str = "") -> bytes:
    from .contract_data import _shell
    rows = [r for r in portal.fetch_cached(T_BILL, 20) if rec in (r["fields"].get("Model") or []) and r["fields"].get("Status") != "Offen"]
    rows.sort(key=lambda r: r["fields"].get("Week start", ""), reverse=True)
    open_ = [r for r in rows if r["fields"].get("Status") in ("Rechnung raus", "Überfällig")]
    def card(r, big=False):
        f = r["fields"]
        st = f.get("Status", "")
        col = {"Bezahlt": "#5fd39a", "Nachweis da": "#7aa8ff", "Überfällig": "#ff9b9b"}.get(st, "#ffb35c")
        ws = dt.date.fromisoformat(f["Week start"])
        up = f'''<form method="post" action="/abrechnung/upload" enctype="multipart/form-data" style="margin-top:12px"><input type="hidden" name="bill" value="{r["id"]}"><input type="hidden" name="kind" value="payment">
          <label>Überweisungsnachweis hochladen (Screenshot oder PDF)<input type="file" name="file" accept="image/*,application/pdf" required></label><button class="btn" type="submit">Nachweis senden</button></form>''' if big else ""
        c = _snap(f.get("Invoice no", ""))
        bank = (f'''<div class="bank">Kontoinhaber: {html.escape(c["holder"])}<br>IBAN: {html.escape(c["iban"])}{"<br>BIC: " + html.escape(c["bic"]) if c["bic"] else ""}<br>Verwendungszweck: <b>{html.escape(f.get("Invoice no", ""))}</b></div>'''
                if c["holder"] and c["iban"] else f'<div class="bank">Bankverbindung steht in der Rechnung. Verwendungszweck: <b>{html.escape(f.get("Invoice no", ""))}</b></div>') if big else ""
        return f'''<div class="inv"><div class="row"><b>KW {_kw(ws)} · {_eur(f.get("Amount"))}</b><span style="color:{col}">{html.escape(st)}</span></div>
          <div class="sub">{_de(ws)} bis {_de(ws + dt.timedelta(days=6))} · fällig {_de(f.get("Due", ""))} · <a href="/abrechnung/pdf/{r["id"]}">Rechnung {html.escape(f.get("Invoice no", ""))} (PDF)</a></div>{bank}{up}</div>'''
    msg = {"payment": "Nachweis ist angekommen. Danke!", "revenue": "Screenshot ist angekommen. Danke!"}.get(ok, "")
    inner = f'''<a href="/" style="color:#9aa3b2;text-decoration:none;font-size:13px">&larr; Portal</a>
<h1>Deine <em>Abrechnung</em></h1><p class="lead">Jeden Montag kommt die Rechnung für die Vorwoche. Überweisen und Nachweis schicken bis Mittwoch, 23:59 Uhr.</p>
{f'<p class="ok">{msg}</p>' if msg else ''}
{"".join(card(r, True) for r in open_) or '<p class="lead">Gerade ist nichts offen.</p>'}
<!-- 17.11 Rene: no revenue screenshot in a normal collaboration (we have access to the account); upload route stays for the rare case -->
{"<h2>Frühere Rechnungen</h2>" + "".join(card(r) for r in rows if r not in open_) if len(rows) > len(open_) else ""}'''
    extra = '<style>h2{font-family:Fraunces,serif;font-weight:500;font-size:20px;margin:24px 0 8px}.inv{border:1px solid #343c4d;border-radius:14px;padding:14px;margin-bottom:10px}.row{display:flex;justify-content:space-between;gap:10px}.sub{color:#9aa3b2;font-size:13px;margin-top:4px}.sub a{color:#ff9ec4}.bank{margin-top:10px;background:#161a22;border-radius:10px;padding:10px 12px;font-size:14px}.ok{background:#10291c;color:#5fd39a;border-radius:10px;padding:10px 12px}input[type=file]{padding:10px}</style>'
    return _shell(inner).replace(b"</head>", extra.encode() + b"</head>", 1)


def send_pdf(handler, rec: str, bill_id: str):
    try:
        f = _get(bill_id)["fields"]
    except Exception:  # noqa: BLE001
        f = {}
    p = FILES / f"{f.get('Invoice no', '')}.pdf"
    if rec not in (f.get("Model") or []) or not f.get("Invoice no") or not p.exists():
        handler.send_response(404); handler.end_headers(); return
    data = p.read_bytes()
    handler.send_response(200); handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Disposition", f'inline; filename="Rechnung_{f["Invoice no"]}.pdf"')
    handler.send_header("Content-Length", str(len(data))); handler.end_headers(); handler.wfile.write(data)


def handle_upload(handler, rec: str, name: str) -> str:
    from . import course, manager
    mp = course.parse_multipart(handler, max_bytes=25 * 1024 * 1024)
    f = mp["files"].get("file")
    if not f:
        raise ValueError("Keine Datei")
    kind = "payment" if mp["fields"].get("kind") == "payment" else "revenue"
    try:
        ext = (Path(f["filename"]).suffix or ".jpg").lower()[:6]
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        local = FILES / rec / f"{kind}-{stamp}{ext}"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(Path(f["path"]).read_bytes())
    finally:
        try:
            os.unlink(f["path"])
        except OSError:
            pass
    link = _to_drive(name, local, local.name) or str(local)
    today = dt.date.today().isoformat()
    if kind == "payment":
        bill = mp["fields"].get("bill", "")
        b = _get(bill)["fields"]
        if rec not in (b.get("Model") or []):
            raise ValueError("falsche Rechnung")
        manager._update(T_BILL, bill, {"Payment proof": link, "Proof uploaded": today, "Status": "Nachweis da"})
        _tg(f"💶 Zahlungsnachweis · {name} · {b.get('Invoice no', '')} · {_eur(b.get('Amount'))}\n{link}")
    else:
        rows = [r for r in _rows() if rec in (r["fields"].get("Model") or []) and r["fields"].get("Status") == "Offen"]
        rows.sort(key=lambda r: r["fields"].get("Week start", ""), reverse=True)
        if rows:
            manager._update(T_BILL, rows[0]["id"], {"Revenue proof": link, "Revenue source": "Screenshot"})
        _tg(f"📊 Umsatz-Screenshot · {name}\n{link}")
    return kind


# ---------------------------------------------------------------- timer

def run(step: str) -> dict:
    """systemd timer entry: monday | thursday | saturday."""
    from . import manager, onboarding
    today = dt.date.today()
    if step == "monday":
        made = ensure_week()
        ws = _monday(today) - dt.timedelta(days=7)
        _tg(f"🧾 Abrechnung KW {_kw(ws)}: {len(made)} neue Zeile(n). Umsätze eintragen und Rechnungen senden:\nhttps://portal.northfeed-management.com/manager#abrechnung")
        return {"created": len(made)}
    models = _models()
    out = []
    for r in _rows():
        f = r["fields"]
        due = str(f.get("Due") or "")
        if not due:
            continue
        rec = (f.get("Model") or [""])[0]
        m = models.get(rec, {})
        if step == "thursday" and f.get("Status") == "Rechnung raus" and due < today.isoformat():
            if m.get("Email"):
                onboarding.send_mail(str(m["Email"]), f"Erinnerung: Rechnung {f.get('Invoice no', '')}",
                    f"Hi {onboarding._first(str(m.get('Name') or ''))},\n\nfür die Rechnung {f.get('Invoice no', '')} über {_eur(f.get('Amount'))} "
                    f"ist bei uns noch kein Überweisungsnachweis angekommen. Die Frist war gestern, 23:59 Uhr. Bitte überweise heute und lade "
                    f"den Nachweis im Portal hoch:\n\nhttps://portal.northfeed-management.com/abrechnung\n\nDein Northfeed-Team\n")
            out.append(m.get("Name", ""))
        if step == "saturday" and f.get("Status") in ("Rechnung raus", "Nachweis da") and due < today.isoformat():
            manager._update(T_BILL, r["id"], {"Status": "Überfällig"})
            out.append(m.get("Name", ""))
    if out:
        _tg(("⏰ Kein Zahlungsnachweis: " if step == "thursday" else "🔴 Überfällig (nicht als bezahlt markiert): ") + ", ".join(out))
    return {"step": step, "models": out}


if __name__ == "__main__":
    import sys
    print(run(sys.argv[1] if len(sys.argv) > 1 else "monday"))
