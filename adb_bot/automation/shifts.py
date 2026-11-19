"""Chatter shifts (18.11, Rene: "zu 100 % sicher, dass jeder seine Shifts macht, dass immer eingetragen wird, wer was wie
gemacht hat, wie viel Umsatz und dass er raus ist"). The chatter presses Start shift / End shift in the portal; End shift
needs the report (revenue per creator from Infloww, PPVs sent/sold, handover note). Every shift is one row in Airtable
"Shifts" (basis for the commission) and is posted to Discord through channel webhooks. A check every 5 minutes alerts the
supervisor when a scheduled shift was not started 15 min after its start, or has no report 30 min after its end.

    shifts.running(team_id)              -> open row or None
    shifts.start(team_row)               -> row
    shifts.end(team_row, report)         -> row     (report: {"revenue": {creator: $}, "sent": n, "sold": n, "handover": str})
    shifts.schedule(shift_text)          -> {"start": min, "end": min, "off": {weekday}} or None
    shifts.check_loop()                  -> runs forever (thread in platform_server)
Discord webhooks (env): CHATTER_WH_ONLINE (#online-offline), CHATTER_WH_LOG (#shift-log), CHATTER_WH_ALERT (#admin).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import portal

T_SHIFTS = os.environ.get("PORTAL_SHIFTS_TABLE", "tblUrhkROP79JOMn5")
TZ = ZoneInfo("Asia/Manila")                     # the shift times are Manila time (rules in Discord)
NAMED = {"1": (0, 480), "2": (480, 960), "3": (960, 1440)}   # "Shift 1/2/3" = 00-08 / 08-16 / 16-24
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
STATE = Path(os.environ.get("SHIFT_ALERT_STATE", "/var/lib/adbbot/shift_alerts.json"))
_lock = threading.Lock()


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def schedule(text: str) -> dict | None:
    """Free-text Team "Shift" -> minutes. Understands "Shift 2", "08:00–16:00", "Mon–Sat 08–16 (Sun off)"."""
    t = (text or "").lower()
    m = re.search(r"shift\s*([123])\b", t)
    if m:
        start, end = NAMED[m.group(1)]
    else:
        m = re.search(r"(\d{1,2})(?:[:.](\d{2}))?\s*[–-]\s*(\d{1,2})(?:[:.](\d{2}))?", t)
        if not m:
            return None
        start = int(m.group(1)) * 60 + int(m.group(2) or 0)
        end = int(m.group(3)) * 60 + int(m.group(4) or 0)
        if end <= start:
            end += 1440
    off = {DAYS.index(d[:3]) for d in re.findall(r"\b(mon|tue|wed|thu|fri|sat|sun)\w*\s+off\b", t)}
    off |= {DAYS.index(d[:3]) for d in re.findall(r"\boff\W+(mon|tue|wed|thu|fri|sat|sun)", t)}
    return {"start": start, "end": end, "off": off}


def label(text: str) -> str:
    s = schedule(text)
    if not s:
        return (text or "").strip()[:40]
    return f"{s['start'] // 60:02d}–{(s['end'] // 60) % 24 or 24:02d} PH"


def _rows() -> list:
    return portal.fetch_cached(T_SHIFTS, 15)


def _hdr() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"}


def _write(fields: dict, rec: str = "") -> dict:
    url = f"https://api.airtable.com/v0/{portal.BASE}/{T_SHIFTS}" + (f"/{rec}" if rec else "")
    r = (requests.patch if rec else requests.post)(url, headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=30)
    r.raise_for_status()
    portal.bust(T_SHIFTS)
    return r.json()


def running(team_id: str) -> dict | None:
    for r in _rows():
        f = r["fields"]
        if team_id in (f.get("Chatter") or []) and f.get("Status") == "Running":
            return r
    return None


def _post(env: str, text: str) -> None:
    url = os.environ.get(env, "")
    if not url:
        print(f"[shifts] {env} not set:", text.replace("\n", " | ")[:200], flush=True)
        return
    try:
        requests.post(url, json={"content": text[:1990], "username": "Northfeed Portal", "allowed_mentions": {"parse": []}}, timeout=15)
    except Exception as e:  # noqa: BLE001
        print("[shifts] webhook:", e, flush=True)


def _creator_names(team: dict) -> list[str]:
    from . import chatter_portal
    return [c["name"] for c in chatter_portal._creators(team.get("models") or [])]


def start(team: dict) -> dict:
    with _lock:
        cur = running(team["id"])
        if cur:
            return cur
        n = now()
        first = (team.get("name") or "").split(" ")[0]
        test = team.get("stage") == "Test shift"                  # 18.11: the unpaid 4-hour test shift is marked as such
        row = _write({"Name": f"{'TEST · ' if test else ''}{first} · {n.date().isoformat()} · {label(team.get('shift', ''))}", "Chatter": [team["id"]],
                      "Date": n.date().isoformat(), "Shift": label(team.get("shift", "")), "Started at": n.isoformat(), "Status": "Running"})
    _post("CHATTER_WH_ONLINE", f"🟢 **{first}** online{' · TEST SHIFT' if test else ''} · {label(team.get('shift', ''))} · {', '.join(_creator_names(team)) or 'no creator yet'}")
    return row


def end(team: dict, report: dict) -> dict:
    with _lock:
        cur = running(team["id"])
        if not cur:
            raise ValueError("No running shift.")
        rev = {k: round(float(v or 0), 2) for k, v in (report.get("revenue") or {}).items()}
        total = round(sum(rev.values()), 2)
        handover = (report.get("handover") or "").strip()
        if len(handover) < 10:
            raise ValueError("Please write a short handover note for the next chatter.")
        n = now()
        row = _write({"Ended at": n.isoformat(), "Revenue": total, "Revenue by creator": "\n".join(f"{k}: ${v:,.2f}" for k, v in rev.items()),
                      "PPVs sent": int(report.get("sent") or 0), "PPVs sold": int(report.get("sold") or 0), "Handover": handover,
                      "Status": "Done"}, cur["id"])
    f = cur["fields"]; first = (team.get("name") or "").split(" ")[0]
    started = dt.datetime.fromisoformat(f["Started at"].replace("Z", "+00:00")).astimezone(TZ)
    _post("CHATTER_WH_ONLINE", f"🔴 **{first}** offline · report sent")
    _post("CHATTER_WH_LOG", (f"**SHIFT · {f.get('Shift', '')} · {n.date().isoformat()}**\nChatter: **{first}** · {started:%H:%M}–{n:%H:%M}\n"
                             + "".join(f"{k}: **${v:,.2f}**\n" for k, v in rev.items()) + f"TOTAL: **${total:,.2f}** · PPVs {int(report.get('sold') or 0)} sold / {int(report.get('sent') or 0)} sent\n"
                             f"Handover: {handover}"))
    return row


def _alerted() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def check() -> list[str]:
    """Scheduled but not started (15 min after start) / running without report (30 min after end) -> one alert each."""
    from . import chatter_portal
    n = now(); today = n.date().isoformat(); mins = n.hour * 60 + n.minute
    seen = _alerted(); out = []
    for c in chatter_portal.team_by_email(cached=True).values():
        if c.get("status") != "Active":
            continue
        s = schedule(c.get("shift", ""))
        first = (c.get("name") or "").split(" ")[0]
        mine = [r for r in _rows() if c["id"] in (r["fields"].get("Chatter") or [])]
        if s and n.weekday() not in s["off"] and s["start"] + 15 <= mins < s["end"]:
            if not any(r["fields"].get("Date") == today for r in mine) and seen.get(f"{c['id']}|{today}|start") is None:
                out.append(f"⚠️ **{first}** has not started the shift ({label(c['shift'])}) – 15 min late.")
                seen[f"{c['id']}|{today}|start"] = int(time.time())
        for r in mine:
            f = r["fields"]
            if f.get("Status") != "Running" or not s:
                continue
            st = dt.datetime.fromisoformat(f["Started at"].replace("Z", "+00:00")).astimezone(TZ)
            due = st.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=s["end"] + 30)
            if n >= due and seen.get(f"{r['id']}|report") is None:
                out.append(f"⚠️ **{first}**: shift {f.get('Shift', '')} ended 30 min ago – still no End shift / report.")
                seen[f"{r['id']}|report"] = int(time.time())
    for msg in out:
        _post("CHATTER_WH_ALERT", msg)
    if out:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({k: v for k, v in seen.items() if time.time() - v < 14 * 86400}))
    return out


def check_loop(every: int = 300) -> None:
    while True:
        try:
            res = check()
            if res:
                print("[shifts] alerts:", res, flush=True)
        except Exception as e:  # noqa: BLE001
            print("[shifts] check failed:", e, flush=True)
        time.sleep(every)


def store_webhooks(online: str, log: str, alert: str) -> dict:
    """18.11: Rene pastes the three Discord webhook URLs once (Manager › Einstellungen); each gets a test post, then the env file."""
    from .onboarding import ENV_FILE
    vals = {"CHATTER_WH_ONLINE": online.strip(), "CHATTER_WH_LOG": log.strip(), "CHATTER_WH_ALERT": alert.strip()}
    for k, v in vals.items():
        if v and not re.fullmatch(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+", v):
            raise ValueError(f"{k}: das ist keine Discord-Webhook-URL")
    vals = {k: v for k, v in vals.items() if v}
    if not vals:
        raise ValueError("Keine URL eingetragen")
    for k, v in vals.items():
        r = requests.post(v, json={"content": "✅ Northfeed Portal connected", "username": "Northfeed Portal"}, timeout=15)
        if r.status_code >= 300:
            raise ValueError(f"{k}: Discord lehnt ab ({r.status_code})")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if not any(ln.startswith(k + "=") for k in vals)] + [f"{k}={v}" for k, v in vals.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.update(vals)
    return {"ok": True, "saved": sorted(vals)}
