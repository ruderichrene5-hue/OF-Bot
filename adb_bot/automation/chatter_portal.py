"""Chatter side of portal.northfeed-management.com (18.11, Rene: "Chatter-Ansicht, weil wir ja Models, Chatter und so haben").
A chatter (Airtable Team, Role Chatter/Supervisor) signs in on the same login page with the e-mail and the "Portal password"
of the Team row. English only. Like the models (16.11): first the course and nothing else; once every lesson of the active
Chatter courses is watched, the home page shows the shift card and one card per assigned creator. Rene 18.11: everything a
chatter sees is English -- the persona comes only from the Steckbrief field "Persona EN" ("Label: value" per line, written by
the team), never from the German fields; never legal name, address, city, payout or handles.

    chatter_portal.lookup(email)          -> team row dict or {}
    chatter_portal.check(email, password) -> team row or None
    chatter_portal.course_open(email)     -> lessons still to watch (0 = everything open)
    chatter_portal.page(email)            -> home page HTML (str)
"""

from __future__ import annotations

import hmac
import html
import os
import time

from . import portal

T_TEAM = os.environ.get("PORTAL_TEAM_TABLE", "tblSKxPAPN8SPKVg9")
T_STECKBRIEF = os.environ.get("PORTAL_STECKBRIEF_TABLE", "tblAz1EpHsOh7JDuR")
ROLES = ("Chatter", "Supervisor")
# German Steckbrief field -> English label, i.e. what "Persona EN" should cover (left out on purpose: Herkunft / Stadt = her real
# city, Social Handles, Auszahlungsweg, Notizen, Team legt fest). Prices are numbers and shown as they are.
PERSONA = [("Alter", "Age"), ("Sprachen", "Languages"), ("Zeitzone", "Time zone"), ("Persönlichkeit in 3 Worten", "Personality"),
           ("Hobbys / Interessen", "Hobbies"), ("Beruf-Story", "Job story"), ("Beziehungsstatus-Story", "Relationship story"),
           ("Lieblingsoutfits", "Favourite outfits"), ("Tattoos / Piercings", "Tattoos / piercings"), ("Tonfall im Chat", "Tone in chat"),
           ("Emoji-Stil", "Emoji style"), ("Sätze die sie nie sagen würde", "She would never say"),
           ("Content-Arten ok", "Content she does"), ("Tabus / No-Gos", "No-gos")]
PRICES = [("Preis Abo", "Subscription"), ("Preis PPV", "PPV"), ("Preis Customs", "Customs")]
LANGS = {"DE": "German", "EN": "English", "TH": "Thai"}
_fails: dict[str, list[float]] = {}


def team_by_email(cached: bool = False) -> dict[str, dict]:
    out = {}
    for r in (portal.fetch_cached(T_TEAM, 30) if cached else portal._fetch(T_TEAM)):
        f = r["fields"]
        mail = str(f.get("Email") or "").strip().lower()
        if mail and f.get("Role") in ROLES:
            out[mail] = {"id": r["id"], "name": f.get("Name", ""), "role": f.get("Role", ""), "status": f.get("Status", ""),
                         "password": str(f.get("Portal password") or ""), "models": f.get("Models") or [], "shift": f.get("Shift", ""),
                         "supervisor": f.get("Supervisor", ""), "start": f.get("Start date", ""), "course_done": bool(f.get("Course done")),
                         "stage": f.get("Stage", ""), "test_score": f.get("Test score"), "test_at": f.get("Test at", ""),
                         "base": f.get("Base pay USD"), "commission": f.get("Commission %"), "contract_signed": f.get("Contract signed", "")}
    return out


def access_allowed(c: dict) -> bool:
    return bool(c) and c.get("status") != "Ended" and c.get("stage") != "Rejected"


def stage(c: dict) -> str:
    """18.11 pipeline: Course → Test → Review → Test shift → Contract → Active. Rows without a Stage: a ticked "Course done"
    (set by hand, e.g. Rene's own account) counts as Active, everything else starts with the course."""
    return c.get("stage") or ("Active" if c.get("course_done") else "Course")


def set_stage(c: dict, st: str) -> None:
    from . import manager
    manager._update(T_TEAM, c["id"], {"Stage": st})
    portal.bust(T_TEAM)


def lookup(email: str) -> dict:
    email = (email or "").strip().lower()
    d = team_by_email(cached=True)
    if email not in d:
        portal.bust(T_TEAM)
        d = team_by_email(cached=True)
    return d.get(email, {})


def check(email: str, password: str) -> dict | None:
    """E-mail + Team "Portal password" -> team row, else None (10 failures in 15 min pause that e-mail, like the models)."""
    email = email.strip().lower()
    now = time.time()
    _fails[email] = [t for t in _fails.get(email, []) if now - t < 900]
    if len(_fails[email]) >= 10:
        return None
    c = team_by_email().get(email)
    if access_allowed(c) and c["password"] and hmac.compare_digest(c["password"], password.strip()):
        _fails.pop(email, None)
        return c
    _fails[email].append(now)
    return None


def course_open(email: str) -> int:
    """Lessons of the active Chatter courses not yet watched. Marks Team "Course done" once everything is watched; a ticked
    "Course done" (set by hand in Airtable, e.g. Rene's own account) opens everything regardless of the lessons."""
    from . import course
    me = lookup(email)
    if me.get("course_done"):
        return 0
    cat = course.catalog(email, "Chatter")
    left = sum(c["total"] - c["done"] for c in cat["courses"])
    if not left and me and not me.get("course_done") and sum(c["total"] for c in cat["courses"]):
        try:
            from . import manager
            manager._update(T_TEAM, me["id"], {"Course done": True})
            portal.bust(T_TEAM)
        except Exception as e:  # noqa: BLE001
            print("[chatter] course done flag:", e)
    return left


def _start_min(shift: str) -> int:
    """First "HH:MM" of the free-text Shift field in minutes (sorting the handover order); unknown shifts go last."""
    import re
    m = re.search(r"(\d{1,2})[:.](\d{2})", shift or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 9999


def _team_on(model_id: str) -> list[dict]:
    """18.11 Rene: everybody who chats for this creator, in shift order, so a chatter knows who hands over to whom."""
    rows = [c for c in team_by_email(cached=True).values() if access_allowed(c) and model_id in c["models"]]
    return sorted(rows, key=lambda c: (_start_min(c["shift"]), c["name"]))


def _translate_persona(s: dict) -> str:
    """18.11 Rene: an English chatter never sees German. "Persona EN" empty -> DeepL translates her German Steckbrief once and the
    result is stored in "Persona EN" (the team can still edit it there). No key yet -> empty, the card says it is being prepared."""
    from . import translate
    pairs = [(lab, ", ".join(map(str, v)) if isinstance(v, list) else str(v)) for k, lab in PERSONA if (v := s.get(k)) not in (None, "", [])]
    if not pairs or not translate.ready():
        return ""
    try:
        en = translate.texts([v for _, v in pairs])
        txt = "\n".join(f"{lab}: {v}" for (lab, _), v in zip(pairs, en))
        from . import manager
        manager._update(T_STECKBRIEF, s["_id"], {"Persona EN": txt})
        portal.bust(T_STECKBRIEF)
        return txt
    except Exception as e:  # noqa: BLE001
        print("[chatter] persona translation:", e)
        return ""


def _creators(model_ids: list) -> list[dict]:
    models = {r["id"]: r["fields"] for r in portal.fetch_cached(portal.T_MODELS, 60)}
    steck = {}
    for r in portal.fetch_cached(T_STECKBRIEF, 60):
        for mid in r["fields"].get("Model") or []:
            steck[mid] = {**r["fields"], "_id": r["id"]}
    out = []
    for mid in model_ids:
        m, s = models.get(mid, {}), steck.get(mid, {})
        if not m or m.get("Status") == "Ended" or m.get("Stage") == "Beendet":
            continue
        if not s.get("Persona EN") and s.get("_id"):
            s["Persona EN"] = _translate_persona(s)
        persona = [tuple(x.strip() for x in ln.split(":", 1)) for ln in str(s.get("Persona EN") or "").splitlines() if ":" in ln]
        out.append({"id": mid, "name": str(s.get("Künstlername") or m.get("Name") or ""), "lang": LANGS.get(str(m.get("Language") or ""), ""),
                    "persona": [(k, v) for k, v in persona if k and v],
                    "prices": [(lab, s.get(k)) for k, lab in PRICES if isinstance(s.get(k), (int, float))]})
    return out


def _v(x) -> str:
    return html.escape(", ".join(map(str, x)) if isinstance(x, list) else str(x))


def ctx(email: str) -> dict:
    """portal_frame context for a chatter: same bar as the models (18.11 Rene: "alles gleich, auch Hintergrund")."""
    from . import course
    me = lookup(email)
    cat = course.catalog(email, "Chatter")
    return {"chatter": True, "lang": "en", "name": (me.get("name") or "").split(" ")[0], "open": course_open(email) if stage(me) == "Course" else 0,
            "videos": (sum(c["done"] for c in cat["courses"]), sum(c["total"] for c in cat["courses"])),
            "discord": os.environ.get("CHATTER_DISCORD_URL", ""), "mass": stage(me) in ("Active", "Test shift")}


STYLE = ("<style>.cg{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-top:10px}"
         ".cc{background:#161a22;border:1px solid #2a303d;border-radius:20px;padding:22px}.cc h2{font-family:Fraunces,serif;font-weight:500;font-size:24px;margin:0 0 12px}"
         ".kv{display:flex;justify-content:space-between;gap:14px;padding:10px 0;font-size:15px;border-top:1px solid #2a303d}.kv span{color:#9aa3b2}.kv b{text-align:right;font-weight:600}"
         ".mut{color:#9aa3b2}.crh{display:flex;align-items:center;gap:14px;margin-bottom:14px}.crh h2{margin:0}"
         ".av{width:46px;height:46px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:19px;color:#0b0d12;background:linear-gradient(135deg,#ff5c9a,#ffc2da)}"
         ".prices{display:flex;gap:10px;margin-bottom:12px}.prices div{flex:1;background:#0f1218;border-radius:14px;padding:10px 12px}.prices span{display:block;color:#9aa3b2;font-size:12px}"
         "dl{margin:0;display:grid;grid-template-columns:150px 1fr;gap:8px 12px;font-size:14.5px}dt{color:#9aa3b2}dd{margin:0}"
         "h3.sub{margin:18px 0 8px;font-size:13px;color:#9aa3b2;font-weight:600;letter-spacing:.08em;text-transform:uppercase}"
         ".team{list-style:none;margin:0;padding:0;display:grid;gap:6px}.team li{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:12px;background:#0f1218;font-size:14px}"
         ".team li span{color:#9aa3b2;margin-left:auto}.team li i{font-style:normal;font-size:11px;padding:2px 8px;border-radius:999px;background:#2a1520;color:#ff9ec4}"
         ".team li.me{outline:1px solid #ff5c9a}.sec{font-family:Fraunces,serif;font-weight:500;font-size:28px;margin:34px 0 10px}"
         ".bt{display:inline-block;margin-top:14px;padding:12px 22px;border-radius:999px;background:#ff5c9a;color:#fff;text-decoration:none;font-weight:700}"
         ".bt.gh{background:transparent;border:1px solid #343c4d;color:#e6e8ee}.ok{background:#10291c;color:#5fd39a;border-radius:10px;padding:10px 12px}"
         ".fl{display:block;font-size:13.5px;color:#9aa3b2;margin:10px 0 0}.fl input,.fl textarea{display:block;width:100%;box-sizing:border-box;margin-top:6px;background:#0f1218;border:1px solid #343c4d;border-radius:12px;color:#e6e8ee;padding:11px 12px;font:15px 'Bricolage Grotesque',sans-serif}"
         ".two{display:grid;grid-template-columns:1fr 1fr;gap:10px}.err{background:#2a1218;color:#ff9b9b;border-radius:10px;padding:10px 12px}"
         ".live{border-color:#ff5c9a}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#5fd39a;margin-left:6px;vertical-align:middle}"
         ".kl{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}.kl a{color:#e6e8ee;text-decoration:none;font-weight:600;font-size:14px;border:1px solid #343c4d;border-radius:999px;padding:10px 16px}</style>")


def _wrap(inner: str, email: str, active: str) -> bytes:
    from .contract_data import _shell
    from . import portal_frame
    page = _shell(inner).replace(b"</head>", STYLE.encode() + b"</head>", 1).replace(b'<html lang="de">', b'<html lang="en">', 1)
    return portal_frame.wrap_shell(page, ctx(email), active)


def _shift_card(me: dict) -> str:
    return (f'<section class="cc"><h2>Your shift</h2><div class="kv"><span>Shift</span><b>{_v(me.get("shift") or "not set yet")}</b></div>'
            f'<div class="kv"><span>Supervisor</span><b>{_v(me.get("supervisor") or "–")}</b></div>'
            f'<div class="kv"><span>Start date</span><b>{_v(me.get("start") or "–")}</b></div></section>')


def _shift_live(me: dict, err: str = "") -> str:
    """18.11 Rene: Start shift / End shift with the report, right on the overview."""
    from . import shifts
    esc = html.escape
    cur = shifts.running(me.get("id", ""))
    head = (f'<div class="kv"><span>Shift</span><b>{_v(me.get("shift") or "not set yet")}</b></div>'
            f'<div class="kv"><span>Supervisor</span><b>{_v(me.get("supervisor") or "–")}</b></div>')
    if not cur:
        return (f'<section class="cc"><h2>Your shift</h2>{head}{f"<p class=err>{esc(err)}</p>" if err else ""}'
                '<form method="post" action="/shift/start"><button class="bt" type="submit" style="border:0;cursor:pointer;font:inherit;font-weight:700">'
                'Start shift</button></form><p class="mut" style="font-size:13px;margin:10px 0 0">Press it when you start – it posts you online in Discord.</p></section>')
    import datetime as _dt
    st = _dt.datetime.fromisoformat(cur["fields"]["Started at"].replace("Z", "+00:00")).astimezone(shifts.TZ)
    rows = "".join(f'<label class="fl">{esc(c["name"])} – revenue in $<input name="rev:{esc(c["name"])}" type="number" step="0.01" min="0" required placeholder="0.00"></label>'
                   for c in _creators(me.get("models") or []))
    return (f'<section class="cc live"><h2>Shift running <span class="dot"></span></h2>{head}'
            f'<div class="kv"><span>Started</span><b>{st:%H:%M} (Manila)</b></div>{f"<p class=err>{esc(err)}</p>" if err else ""}'
            f'<form method="post" action="/shift/end" class="rep"><h3 class="sub">End shift – your report</h3>{rows}'
            '<div class="two"><label class="fl">PPVs sent<input name="sent" type="number" min="0" required></label>'
            '<label class="fl">PPVs sold<input name="sold" type="number" min="0" required></label></div>'
            '<label class="fl">Handover for the next chatter<textarea name="handover" rows="3" minlength="10" required '
            'placeholder="Who is in a sale, who to watch, what happened"></textarea></label>'
            '<p class="mut" style="font-size:13px">Revenue: take it from Infloww (Creator reports) for your shift.</p>'
            '<button class="bt" type="submit" style="border:0;cursor:pointer;font:inherit;font-weight:700">End shift &amp; send report</button></form></section>')


def page(email: str, err: str = "") -> bytes:
    me = lookup(email)
    from . import course
    cat = course.catalog(email, "Chatter")
    done, total = sum(c["done"] for c in cat["courses"]), sum(c["total"] for c in cat["courses"])
    discord = os.environ.get("CHATTER_DISCORD_URL", "")
    cards = "".join(
        f'<section class="cc"><div class="crh"><div class="av">{html.escape(c["name"][:1] or "?")}</div><div><h2>{html.escape(c["name"])}</h2>'
        f'<span class="mut">{html.escape(str(c["lang"] or ""))}</span></div></div>'
        + ('<div class="prices">' + "".join(f'<div><span>{html.escape(l)}</span><b>{_v(v)}</b></div>' for l, v in c["prices"]) + "</div>" if c["prices"] else "")
        + ("<dl>" + "".join(f"<dt>{html.escape(l)}</dt><dd>{_v(v)}</dd>" for l, v in c["persona"]) + "</dl>" if c["persona"]
           else '<p class="mut">Her persona is being prepared. Ask your supervisor before your first shift.</p>')
        + '<h3 class="sub">Team on her chats</h3><ul class="team">' + "".join(
            f'<li class="{"me" if t["id"] == me.get("id") else ""}"><b>{html.escape((t["name"] or "").split(" ")[0])}'
            f'{" (you)" if t["id"] == me.get("id") else ""}</b><span>{_v(t["shift"] or "shift not set")}</span>'
            f'{"<i>Supervisor</i>" if t["role"] == "Supervisor" else ""}</li>' for t in _team_on(c["id"])) + "</ul>"
        + "</section>" for c in _creators(me.get("models") or []))
    crs = (f'<section class="cc"><h2>Course</h2><div class="kv"><span>Progress</span><b>{done} / {total} lessons</b></div>'
           f'<a class="bt gh" href="/course">Open course</a></section>')
    dc = (f'<section class="cc"><h2>Discord</h2><p class="mut" style="margin:0">Shift handovers, questions and news from the team.</p>'
          f'<a class="bt" href="{html.escape(discord)}" target="_blank" rel="noopener">Open Discord</a></section>') if discord else ""
    st = stage(me)
    note = {"Test shift": "Next step: your <b>4-hour test shift</b>. Your supervisor tells you the time – then press Start shift here and End shift with your report when you finish.",
            "Contract": "Test shift done – your contract is on its way by e-mail. Sign it, then you start your regular shifts.",
            "Review": "Your test is in review. Your supervisor reads your answers and tells you about the test shift."}.get(st, "")
    if st == "Review":
        return _wrap(f'<span class="eyebrow">Chatter</span><h1>Almost <em>there.</em></h1><p class="lead">{note}</p><div class="cg">{crs}{dc}</div>', email, "home")
    inner = (f'<span class="eyebrow">Chatter</span><h1>Hi {html.escape((me.get("name") or "").split(" ")[0])}, <em>welcome.</em></h1>'
             + (f'<p class="lead" style="background:#26121a;border:1px solid #ff5c9a;border-radius:14px;padding:12px 16px">{note}</p>' if note else "") +
             f'<p class="lead">Your shift, your creators and the course. Questions go to your supervisor.</p>'
             f'<div class="cg">{_shift_live(me, err)}{crs}{dc}</div><h2 class="sec">Your creators</h2>'
             f'<div class="cg">{cards or "<section class=cc><p class=mut>No creator assigned yet. Your supervisor will assign you before your first shift.</p></section>"}</div>')
    return _wrap(inner, email, "home")


def set_password(email: str, new: str) -> tuple[bool, str]:
    from . import manager
    new = (new or "").strip()
    if len(new) < 8:
        return False, "Please use at least 8 characters."
    me = team_by_email().get((email or "").strip().lower())
    if not me:
        return False, "Account not found."
    manager._update(T_TEAM, me["id"], {"Portal password": new})
    portal.bust(T_TEAM)
    _fails.pop(email, None)
    return True, ""


def konto(email: str, pw_saved: bool = False, pw_err: str = "") -> bytes:
    """18.11 Rene: the avatar opens the account - shift, contract, change password (no e-mail change, the team sets it)."""
    me = lookup(email)
    esc = html.escape
    login = (f'<form method="post" action="/konto/password"><input type="email" name="email" value="{esc(email)}" autocomplete="username" style="display:none">'
             f'<label>New password<input type="password" name="password" minlength="8" required autocomplete="new-password" placeholder="at least 8 characters"></label>'
             f'{"<p class=ok>New password saved.</p>" if pw_saved else ""}{f"<p class=err>{esc(pw_err)}</p>" if pw_err else ""}'
             f'<button class="btn" type="submit" style="width:auto;padding:12px 22px">Change password</button></form>'
             f'<div class="kv" style="margin-top:16px"><span>Sign-in e-mail</span><b>{esc(email)}</b></div>')
    from . import chatter_admin
    signed = me.get("contract_signed")
    contract = ('<div class="kv"><span>Agreement</span><b>Fan Messaging Services</b></div>'
                + (f'<div class="kv"><span>Signed on</span><b>{esc(str(signed))}</b></div>' if signed else "")
                + ('<a class="bt gh" href="/konto/agreement.pdf" target="_blank">Download agreement (PDF)</a>' if chatter_admin.contract_pdf(me.get("id", "")) else
                   '<p class="mut" style="font-size:13.5px">Your signed agreement will be available here after the test shift.</p>'))
    inner = (f'<h1>Your <em>account</em></h1><div class="cg">{_shift_card(me)}'
             f'<section class="cc"><h2>Your contract</h2>{contract}</section><section class="cc"><h2>Your login</h2>{login}</section></div>'
             f'<div class="kl"><a href="/">Overview →</a><a href="/course">Course →</a><a href="/logout">Sign out</a></div>')
    return _wrap(inner, email, "konto")
