"""Manager settings: "Was noch zu tun ist" (15.11, Rene: everything he still has to fix, in one place).
Automatic items are checked live; manual items are ticked by Rene and stored in /etc/northfeed/setup_checklist.json."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import portal

STORE = Path("/etc/northfeed/setup_checklist.json")
MANUAL = [
    ("voice", "Stimme festlegen", "Für Tutorials und Ads: 1–2 Minuten frei ins Handy sprechen (Stimme wird geklont) oder eine Nummer aus Hörprobe 3 wählen. Davon hängen Tutorials und der Ad-Neuschnitt ab."),
    ("drive_rec", "Google-Drive-App aufnehmen", "2–3 iPhone-Bildschirmaufnahmen à 20–30 s: App öffnen, „Für mich freigegeben“, ein Foto hochladen. Für Tutorial 2."),
    ("profile_pic", "Google-Profilbild für info@", "Im Google-Admin einmal dein Passwort eingeben, dann setzt Claude das Bild."),
]


def _manual() -> dict:
    try:
        return json.loads(STORE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def toggle(key: str, done: bool) -> dict:
    d = _manual()
    d[key] = bool(done)
    STORE.write_text(json.dumps(d))
    return {"ok": True}


def _drive_own_client() -> bool:
    try:
        dump = json.loads(subprocess.run(["rclone", "config", "dump"], capture_output=True, text=True, timeout=30).stdout or "{}")
        return bool(dump.get(os.environ.get("MODELS_DRIVE_REMOTE", "veralta"), {}).get("client_id"))
    except Exception:  # noqa: BLE001
        return False


def status() -> list:
    from . import billing, docuseal
    items = []
    add = lambda key, title, ok, text, link="", kind="auto": items.append({"key": key, "title": title, "done": bool(ok), "text": text, "link": link, "kind": kind})  # noqa: E731
    add("mail", "Mailversand von info@", bool(os.environ.get("PORTAL_SMTP_HOST") and os.environ.get("PORTAL_SMTP_PASSWORD")),
        "Verträge, Logins und Rechnungen gehen von info@ raus.", "#einstellungen")
    add("bank", "Bankverbindung für Rechnungen", billing.bank_ready(), "Ohne aktives Konto gehen keine Rechnungen raus. Unten unter „Bankverbindungen“ eintragen.", "#einstellungen")
    try:
        ts = docuseal.template_status()
        add("contract", "Vertragsvorlage mit Unterschrift", ts.get("ok") and ts.get("signature_png"), "Vorlage und deine Unterschrift sind hinterlegt.")
    except Exception:  # noqa: BLE001
        add("contract", "Vertragsvorlage mit Unterschrift", False, "DocuSeal antwortet nicht.")
    add("drive_client", "Google-Drive-Zugang absichern", _drive_own_client(),
        "Der Server nutzt einen geteilten Drive-Zugang, den Google 2026 abschaltet. Danach brechen Ordner, Freigaben, Rechnungen und Nachweise weg. Lösung: eigener Zugang in deiner Google Cloud Console, ca. 10 Minuten zusammen mit Claude.")
    for r in portal.fetch_cached(portal.T_MODELS):              # same 30 s cache as the login lookups; model edits bust it
        f = r["fields"]
        from . import manager
        if str(f.get("Stage") or "") in ("Beendet",) or f.get("Status") == "Ended" or str(f.get("Name", "")).startswith(("Demo", "ZZ")) \
                or f.get("Alte Agentur") \
                or (f.get("Email") and manager.is_manager(str(f["Email"]))):
            continue
        name = f.get("Name", "")
        miss = [lbl for key, lbl in (("Email", "E-Mail"), ("WhatsApp", "WhatsApp")) if not f.get(key)]
        if miss:
            add(f"model_{r['id']}", f"{name}: Kontaktdaten fehlen", False, "Fehlt: " + ", ".join(miss) + ". Ohne E-Mail kein Login, keine Rechnung.", f"#model/{r['id']}")
        if str(f.get("Stage") or "") == "Aktiv" and not f.get("Contract signed") and not f.get("Welcome sent"):
            add(f"access_{r['id']}", f"{name}: weder Vertrag noch Portal-Zugang", False, "Auf der Model-Seite „Vertrag senden“ oder „Zugang senden“.", f"#model/{r['id']}")
    m = _manual()
    for key, title, text in MANUAL:
        add(key, title, m.get(key), text, kind="manual")
    items.sort(key=lambda x: x["done"])
    return items
