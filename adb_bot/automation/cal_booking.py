"""Cal.com bookings -> pipeline (15.11, Rene: "Call gebucht" should happen by itself).

Cal.com (account info@northfeed-management.com, event cal.com/northfeed/call, Google Meet) posts a webhook to
https://portal.northfeed-management.com/cal/webhook. A booking moves the matching card to "Call gebucht" and stores
the time; an application or an unknown booker becomes a model at that stage. Rene gets a Telegram message.

No shared secret is configured on purpose (we do not type secrets into third-party forms). The handler can only
move a card forward to "Call gebucht", set a call time and create a pre-contract model -- nothing that grants access.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from . import portal

CAL_LINK = "https://cal.com/northfeed/call"
EARLY = ("", "Neu", "Bewerbung", "Kontaktiert")


def link_for(name: str = "", email: str = "") -> str:
    q = {k: v for k, v in (("name", name), ("email", email)) if v}
    return CAL_LINK + ("?" + urllib.parse.urlencode(q) if q else "")


def _tg(text: str) -> None:
    tok, chat, topic = (os.environ.get(k, "").strip() for k in ("NORTHFEED_TG_TOKEN", "NORTHFEED_TG_CHAT", "NORTHFEED_TG_TOPIC"))
    if not tok or not chat:
        return
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if topic:
        data["message_thread_id"] = topic
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=urllib.parse.urlencode(data).encode(), timeout=15).read()
    except Exception as e:  # noqa: BLE001
        print("[cal] telegram failed:", e)


def _berlin(iso: str) -> str:
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Berlin"))
        return t.strftime("%a %d.%m. %H:%M")
    except Exception:  # noqa: BLE001
        return iso


def handle(payload: dict) -> dict:
    from . import manager
    ev = payload.get("triggerEvent", "")
    p = payload.get("payload") or {}
    att = (p.get("attendees") or [{}])[0]
    email, name = str(att.get("email") or "").strip().lower(), str(att.get("name") or "").strip()
    start = str(p.get("startTime") or "")
    meet = str((p.get("metadata") or {}).get("videoCallUrl") or p.get("location") or "")
    if ev == "PING":
        return {"ok": True, "ping": True}
    if not email:
        return {"ok": True, "ignored": "no attendee"}
    if ev == "BOOKING_CANCELLED":
        _tg(f"❌ Call abgesagt · {name or email} · war {_berlin(start)}")
        return {"ok": True, "cancelled": True}
    if ev not in ("BOOKING_CREATED", "BOOKING_RESCHEDULED"):
        return {"ok": True, "ignored": ev}

    call = start[:19] + ".000Z" if start.endswith("Z") or "+" not in start else start
    model = next((r for r in portal._fetch(portal.T_MODELS) if str(r["fields"].get("Email") or "").strip().lower() == email), None)
    action = ""
    if model:
        f = {"Call date": call}
        if str(model["fields"].get("Stage") or "") in EARLY:
            f["Stage"] = "Call gebucht"
        manager._update(portal.T_MODELS, model["id"], f)
        action, mname = "verschoben" if "Stage" in f else "Termin aktualisiert", model["fields"].get("Name", name)
    else:
        app = next((a for a in manager.applications() if a["email"].strip().lower() == email and a["status"] in ("new", "contacted")), None)
        mname = (" ".join(x for x in (app["firstName"], app["lastName"]) if x) if app else "") or name or email.split("@")[0]
        fields = {"Name": mname, "Email": email, "Stage": "Call gebucht", "Status": "Onboarding", "Call date": call,
                  "Package": "Full Management", "Management split %": 60, "Proof channel": "WhatsApp",
                  "Language": "EN" if app and (app.get("lang") or "de").lower() != "de" else "DE",
                  "Portal password": secrets.token_urlsafe(8)}
        if app and app.get("phone"):
            fields["WhatsApp"] = app["phone"]
        manager._create(portal.T_MODELS, fields)
        if app:
            manager.api("app.status", {"id": app["id"], "status": "accepted", "model": mname}, "cal-webhook")
        action = "neu aus Bewerbung" if app else "neu (direkt gebucht)"
    _tg(f"📅 Call gebucht · {mname} · {_berlin(start)} (Berlin)\n{email}\n{meet}".strip() + f"\nKarte: {action}")
    return {"ok": True, "model": mname, "action": action}
