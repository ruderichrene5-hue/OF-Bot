"""Chatter mass messages (18.11 Rene: "jede 2 Stunden neue Massennachrichten, manuell, die natürlich zur Uhrzeit passen ...
checken, welche wir schon benutzt haben ... eine kleine Liste aufbauen").
Library: a Google Sheet (18.11 Rene: "Google Sheet besser", env MASS_SHEET_ID, in the info@ Drive, read through the Drive CSV export);
the Airtable table "Mass Messages" is only the fallback when the sheet can't be read. Log: Airtable "Mass Message Log", one row per send.
The sheet pulls "last used / how often" back from /mass/usage.csv (IMPORTDATA, secret key) - no Sheets API needed.
The chatter page /mass shows per creator when the next one is due (every 2 h), the fans' time of day, and suggestions that were
not used for that creator yet. "Mark as sent" writes the log row and posts to the Discord shift log.

    mass_messages.page(email, msg="")      -> bytes
    mass_messages.mark_sent(email, form)   -> (ok, message)
"""

from __future__ import annotations

import datetime as dt
import html
import os
from zoneinfo import ZoneInfo

from . import portal

T_LIB = os.environ.get("PORTAL_MASS_TABLE", "tblzx3wEaynmieBZu")
T_LOG = os.environ.get("PORTAL_MASS_LOG_TABLE", "tbl7VNAHHXjXjDBJz")
FAN_TZ = ZoneInfo(os.environ.get("MASS_FAN_TZ", "America/New_York"))   # fans are mostly US: the time of day is theirs
EVERY = dt.timedelta(hours=2)
REUSE_DAYS = int(os.environ.get("MASS_REUSE_DAYS", "30"))                # a message counts as "used" for this long per creator
SLOTS = [("Night", 0, 6), ("Morning", 6, 11), ("Midday", 11, 15), ("Afternoon", 15, 19), ("Evening", 19, 23), ("Night", 23, 24)]


def slot(t: dt.datetime) -> str:
    h = t.astimezone(FAN_TZ).hour
    return next(n for n, a, b in SLOTS if a <= h < b)


def _ts(s) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


SHEET_ID = os.environ.get("MASS_SHEET_ID", "")
SLOT_ALIAS = {"morgen": "Morning", "mittag": "Midday", "nachmittag": "Afternoon", "abend": "Evening", "nacht": "Night", "immer": "Any", "": "Any"}
_sheet = {"at": 0.0, "rows": None}


def _key(text: str) -> str:
    import hashlib
    return "s" + hashlib.sha1(" ".join(text.split()).encode()).hexdigest()[:12]


def _sheet_rows() -> list[dict] | None:
    """Rows of the first tab as dicts (Drive export as CSV, cached 60 s). None when there is no sheet or it can't be read."""
    import csv, io, time
    import requests
    if not SHEET_ID:
        return None
    if _sheet["rows"] is not None and time.time() - _sheet["at"] < 60:
        return _sheet["rows"]
    from . import drive_tree
    try:
        r = requests.get(f"https://www.googleapis.com/drive/v3/files/{SHEET_ID}/export", params={"mimeType": "text/csv"},
                         headers={"Authorization": f"Bearer {drive_tree._access_token()}"}, timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print("[mass] sheet:", e, flush=True)
        return _sheet["rows"]
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8"))))
    _sheet.update(at=time.time(), rows=rows)
    return rows


def _lib() -> list[dict]:
    rows = _sheet_rows()
    if rows is not None:
        out = []
        col = lambda r, *names: next((str(r[k]).strip() for k in r if k and any(k.lower().startswith(n) for n in names)), "")
        for r in rows:
            text = col(r, "text")
            if len(text) < 3 or col(r, "aktiv", "active").lower() in ("nein", "no", "0", "false", "x"):
                continue
            sl = col(r, "tageszeit", "slot")
            sl = SLOT_ALIAS.get(sl.lower(), sl.title() if sl.title() in ("Morning", "Midday", "Afternoon", "Evening", "Night", "Any") else "Any")
            typ = "PPV" if col(r, "typ", "type").upper() == "PPV" else "Free"
            try:
                price = float(col(r, "preis", "price").replace("$", "").replace(",", ".") or 0) or None
            except ValueError:
                price = None
            out.append({"id": _key(text), "name": "", "text": text, "slot": sl, "type": typ, "price": price, "media": col(r, "inhalt", "media"),
                        "creators": [c.strip().lower() for c in col(r, "creator").split(",") if c.strip()]})
        return out
    out = []
    for r in portal.fetch_cached(T_LIB, 60):
        f = r["fields"]
        if f.get("Active") and f.get("Text"):
            out.append({"id": r["id"], "name": f.get("Name", ""), "text": f["Text"], "slot": f.get("Slot", "Any"), "type": f.get("Type", "Free"),
                        "price": f.get("Price"), "media": f.get("Media", ""), "creators": f.get("Creators") or []})
    return out


def _log() -> list[dict]:
    out = []
    for r in portal.fetch_cached(T_LOG, 15):
        f = r["fields"]
        at = _ts(f.get("Sent at"))
        if at:
            out.append({"msg": (f.get("Message") or [""])[0], "creator": (f.get("Creator") or [""])[0], "chatter": (f.get("Chatter") or [""])[0],
                        "at": at, "text": f.get("Text sent", ""), "price": f.get("Price")})
    return sorted(out, key=lambda x: x["at"], reverse=True)


def state(cid: str, now: dt.datetime) -> dict:
    """Last send, next due, suggestions for one creator."""
    logs = [x for x in _log() if x["creator"] == cid]
    used = {}
    for x in logs:                                           # used = by text (the sheet has no ids), old rows by message link
        for k in (_key(x["text"]) if x["text"] else "", x["msg"]):
            if k and k not in used:
                used[k] = x["at"]
    from . import chatter_portal
    cname = next((c["name"] for c in chatter_portal._creators([cid])), "").lower()
    sl = slot(now)
    fresh = now - dt.timedelta(days=REUSE_DAYS)
    cand = [m for m in _lib() if (not m["creators"] or cid in m["creators"] or cname in m["creators"]) and m["slot"] in (sl, "Any")
            and (m["id"] not in used or used[m["id"]] < fresh)]
    cand.sort(key=lambda m: (m["slot"] != sl, m["id"] in used, used.get(m["id"], now)))
    last = logs[0] if logs else None
    return {"slot": sl, "last": last, "next": (last["at"] + EVERY) if last else None, "suggest": cand, "recent": logs[:6]}


def _when(t: dt.datetime, now: dt.datetime) -> str:
    from . import shifts
    mins = int((t - now).total_seconds() // 60)
    rel = f"in {mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"in {mins} min"
    return f"{t.astimezone(shifts.TZ):%H:%M} Manila · {rel}"


STYLE = ("<style>.mm{margin-top:12px;padding:14px 16px;border-radius:14px;background:#0f1218;border:1px solid #2a303d}"
         ".mm p{margin:0 0 8px;font-size:15.5px;line-height:1.45;white-space:pre-wrap}.mm .meta{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12.5px;color:#9aa3b2}"
         ".tag{padding:3px 9px;border-radius:999px;background:#1d2130;color:#e6e8ee;font-weight:600}.tag.ppv{background:#2a1520;color:#ff9ec4}"
         ".mm .acts{display:flex;gap:8px;margin-top:10px}.mm button{border:0;cursor:pointer;font:600 13.5px 'Bricolage Grotesque',sans-serif;border-radius:999px;padding:8px 14px}"
         ".cp{background:#1d2130;color:#e6e8ee}.sd{background:#ff5c9a;color:#fff}.due{display:inline-block;padding:6px 12px;border-radius:999px;font-weight:700;font-size:13.5px}"
         ".due.now{background:#ff5c9a;color:#fff}.due.wait{background:#1d2130;color:#e6e8ee}.rec{list-style:none;margin:0;padding:0;display:grid;gap:6px;font-size:13.5px}"
         ".rec li{padding:8px 12px;border-radius:12px;background:#0f1218;color:#c9ced8}.rec li span{color:#9aa3b2}</style>"
         "<script>function cp(b){navigator.clipboard.writeText(b.closest('.mm').querySelector('p').innerText);b.textContent='Copied ✓';}</script>")


def page(email: str, msg: str = "") -> bytes:
    from . import chatter_portal
    esc = html.escape
    me = chatter_portal.lookup(email)
    now = dt.datetime.now(dt.timezone.utc)
    team = {c["id"]: (c.get("name") or "").split(" ")[0] for c in chatter_portal.team_by_email(cached=True).values()}
    cards = []
    for c in chatter_portal._creators(me.get("models") or []):
        s = state(c["id"], now)
        due = (f'<span class="due now">Due now</span>' if not s["next"] or s["next"] <= now
               else f'<span class="due wait">Next {esc(_when(s["next"], now))}</span>')
        last = (f'<p class="mut" style="font-size:13.5px;margin:10px 0 0">Last: {s["last"]["at"].astimezone(FAN_TZ):%I:%M %p} fan time by '
                f'{esc(team.get(s["last"]["chatter"], "–"))}</p>') if s["last"] else '<p class="mut" style="font-size:13.5px;margin:10px 0 0">No mass message logged yet.</p>'
        sug = "".join(
            f'<div class="mm"><p>{esc(m["text"])}</p><div class="meta">'
            f'<span class="tag {"ppv" if m["type"] == "PPV" else ""}">{"PPV $" + str(int(m["price"])) if m["type"] == "PPV" and m["price"] else esc(m["type"])}</span>'
            f'<span class="tag">{esc(m["slot"])}</span>{f"<span>📎 {esc(m["media"])}</span>" if m["media"] else ""}</div>'
            f'<form method="post" action="/mass/sent" class="acts"><input type="hidden" name="creator" value="{esc(c["id"])}">'
            f'<input type="hidden" name="msg" value="{esc(m["id"])}"><button type="button" class="cp" onclick="cp(this)">Copy text</button>'
            f'<button class="sd" type="submit">Mark as sent</button></form></div>' for m in s["suggest"][:4]) or \
            '<p class="mut">Every message for this time of day was used recently. Write your own below, or ask your supervisor for new ones.</p>'
        own = (f'<details style="margin-top:12px"><summary class="mut" style="cursor:pointer">Wrote your own?</summary>'
               f'<form method="post" action="/mass/sent"><input type="hidden" name="creator" value="{esc(c["id"])}">'
               f'<label class="fl">Text you sent<textarea name="text" rows="2" minlength="5" required></textarea></label>'
               f'<label class="fl">Price in $ (empty = free)<input name="price" type="number" min="0" max="100" step="1"></label>'
               f'<button class="bt" type="submit" style="border:0;cursor:pointer;font:inherit;font-weight:700">Log it</button></form></details>')
        rec = "".join(f'<li><span>{x["at"].astimezone(FAN_TZ):%a %I:%M %p} · {esc(team.get(x["chatter"], "–"))}{" · $" + str(int(x["price"])) if x["price"] else ""}</span><br>'
                      f'{esc(x["text"][:90])}</li>' for x in s["recent"])
        cards.append(f'<section class="cc"><div class="crh"><div class="av">{esc(c["name"][:1] or "?")}</div><div><h2>{esc(c["name"])}</h2>'
                     f'<span class="mut">Fan time {now.astimezone(FAN_TZ):%I:%M %p} · {esc(s["slot"])}</span></div></div>{due}{last}'
                     f'<h3 class="sub">Not used yet – fits the time</h3>{sug}{own}'
                     + (f'<h3 class="sub">Sent recently</h3><ul class="rec">{rec}</ul>' if rec else "") + "</section>")
    inner = (f'{STYLE}<span class="eyebrow">Chatter</span><h1>Mass <em>messages.</em></h1>'
             f'<p class="lead">Every 2 hours one new mass message per creator, matching the fans\' time of day. Pick one that was not used yet, '
             f'send it in Infloww (exclude the TW and OC lists), then press Mark as sent.</p>'
             + (f'<p class="ok">{esc(msg)}</p>' if msg else "")
             + f'<div class="cg">{"".join(cards) or "<section class=cc><p class=mut>No creator assigned yet.</p></section>"}</div>'
             + _all_list())
    return chatter_portal._wrap(inner, email, "mass")


def _all_list() -> str:
    """18.11 Rene wants the whole list visible (Google Sheet): every text, grouped by time of day, folded away."""
    esc = html.escape
    lib = _lib()
    order = ["Morning", "Midday", "Afternoon", "Evening", "Night", "Any"]
    body = "".join(
        f'<h3 class="sub">{esc(sl)}</h3>' + "".join(
            f'<div class="mm"><p>{esc(m["text"])}</p><div class="meta"><span class="tag {"ppv" if m["type"] == "PPV" else ""}">'
            f'{"PPV $" + str(int(m["price"])) if m["type"] == "PPV" and m["price"] else esc(m["type"])}</span>'
            f'{f"<span>📎 {esc(m["media"])}</span>" if m["media"] else ""}</div>'
            f'<div class="acts"><button type="button" class="cp" onclick="cp(this)">Copy text</button></div></div>' for m in lib if m["slot"] == sl)
        for sl in order if any(m["slot"] == sl for m in lib))
    return (f'<details class="cc" style="margin-top:18px"><summary style="cursor:pointer;font-weight:700">All mass messages ({len(lib)})</summary>'
            f'<p class="mut" style="font-size:13.5px">The full list. Take the suggestions above first – they are the ones not used yet for your creator.</p>{body}</details>')


def mark_sent(email: str, form: dict) -> tuple[bool, str]:
    from . import chatter_portal, manager, shifts
    me = chatter_portal.team_by_email().get((email or "").strip().lower())
    if not me:
        return False, "Not signed in."
    g = lambda k: (form.get(k) or [""])[0].strip()
    cid, mid = g("creator"), g("msg")
    if cid not in (me.get("models") or []):
        return False, "This creator is not assigned to you."
    lib = {m["id"]: m for m in _lib()}
    if mid and mid not in lib:
        return False, "Unknown message."
    m = lib.get(mid, {})
    text = m.get("text") or g("text")
    if len(text) < 5:
        return False, "Please paste the text you sent."
    try:
        price = float(g("price")) if g("price") else (float(m["price"]) if m.get("type") == "PPV" and m.get("price") else None)
    except ValueError:
        price = None
    now = dt.datetime.now(dt.timezone.utc)
    cname = next((c["name"] for c in chatter_portal._creators([cid])), "")
    first = (me.get("name") or "").split(" ")[0]
    f = {"Name": f"{cname} · {now.astimezone(shifts.TZ):%Y-%m-%d %H:%M}", "Creator": [cid], "Chatter": [me["id"]], "Sent at": now.isoformat(),
         "Slot": slot(now), "Text sent": text}
    if mid and mid.startswith("rec"):                        # Airtable fallback only; sheet rows are matched by text
        f["Message"] = [mid]
    if price:
        f["Price"] = price
    manager._create(T_LOG, f)
    portal.bust(T_LOG)
    shifts._post("CHATTER_WH_LOG", f"📢 **Mass message** · {cname} · {first}{f' · ${price:,.0f}' if price else ' · free'}\n> {text[:300]}")
    return True, f"Logged for {cname}. Next one is due in 2 hours."


def usage_csv() -> bytes:
    """18.11: read by the Google Sheet (=IMPORTDATA) to fill "last used / how often / where" per text."""
    import csv, io
    from . import chatter_portal, shifts
    names = {}
    agg: dict[str, list] = {}
    for x in _log():                                         # newest first
        t = " ".join((x["text"] or "").split())
        if not t:
            continue
        if x["creator"] not in names:
            names[x["creator"]] = next((c["name"] for c in chatter_portal._creators([x["creator"]])), "")
        a = agg.setdefault(t, [x["at"].astimezone(shifts.TZ).strftime("%Y-%m-%d %H:%M"), 0, names[x["creator"]]])
        a[1] += 1
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Text", "Zuletzt benutzt (Manila)", "Wie oft", "Zuletzt bei"])
    for t, (last, n, who) in agg.items():
        w.writerow([t, last, n, who])
    return buf.getvalue().encode("utf-8")
