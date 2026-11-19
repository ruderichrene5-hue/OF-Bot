"""Own login for portal.northfeed-management.com (16.10, Rene: "soll nicht verbunden sein" -- no Cloudflare
Access, no RNX Viewer branding). A model signs in with the e-mail from the Models table and her "Portal password"
(sent in the welcome mail). 16.11 Rene: password only, no mailed login codes any more ("das beißt sich, wir brauchen
nur eins"); a forgotten password is reset through a 30-minute link by mail, and she can change it under Konto.
The session is a signed cookie (nf_session), 30 days.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import secrets
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

import requests

from . import portal

HEARTS = Path(__file__).with_name("portal_hearts.js").read_text(encoding="utf-8")
SECRET_FILE = Path(os.environ.get("PORTAL_SECRET_FILE", "/etc/adbbot/portal_secret"))
SESSION_DAYS = 30
RESET_TTL = 1800                                 # "Passwort vergessen" link, 30 min
MIN_PW = 8
_fails: dict[str, list[float]] = {}              # email -> failed sign-in times (10 in 15 min -> 15 min pause)
PRE_SIGN_STAGES = ("Neu", "Bewerbung", "Kontaktiert", "Call gebucht", "Vertrag offen")   # 15.11 Rene: no portal before the signed contract


def access_allowed(m: dict) -> bool:
    """A model may log in once the contract is signed (Stage past "Vertrag offen") and she is not ended.
    Rows without a Stage (the models from before the pipeline) keep their access."""
    if not m or m.get("status") in ("Ended",) or m.get("stage") in ("Beendet",):
        return False
    return m.get("stage", "") not in PRE_SIGN_STAGES


def session_allowed(m: dict) -> bool:
    """17.11: an existing session dies only with the end of the collaboration (Stage Beendet / Status Ended);
    the pre-signature stages only block a new login."""
    return not (m and (m.get("status") in ("Ended",) or m.get("stage") in ("Beendet",)))


def _secret() -> bytes:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_text().strip().encode()


def models_by_email(cached: bool = False) -> dict[str, dict]:
    out = {}
    for m in (portal.fetch_cached(portal.T_MODELS) if cached else portal._fetch(portal.T_MODELS)):
        f = m["fields"]
        mail = str(f.get("Email") or "").strip().lower()
        if mail:
            out[mail] = {"name": f.get("Name", ""), "password": str(f.get("Portal password") or ""), "status": f.get("Status", ""),
                         "stage": f.get("Stage", ""), "id": m["id"], "language": f.get("Language", "DE")}
    return out


def smtp_ready() -> bool:
    return bool(os.environ.get("PORTAL_SMTP_HOST") and os.environ.get("PORTAL_SMTP_FROM"))


def _send(email: str, subject: str, text: str) -> bool:
    if not smtp_ready():
        print(f"portal mail for {email}: {subject}\n{text}")    # journal only, until an SMTP sender exists
        return False
    msg = EmailMessage()
    msg["From"] = os.environ["PORTAL_SMTP_FROM"]
    msg["To"] = email
    msg["Subject"] = subject
    msg.set_content(text)
    host, port = os.environ["PORTAL_SMTP_HOST"], int(os.environ.get("PORTAL_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        if os.environ.get("PORTAL_SMTP_USER"):
            s.login(os.environ["PORTAL_SMTP_USER"], os.environ.get("PORTAL_SMTP_PASSWORD", ""))
        s.send_message(msg)
    return True


def _pw_tag(pw: str) -> str:
    """Part of the reset token: the link dies as soon as the password changes (used once)."""
    return hashlib.sha256(("nf-reset|" + (pw or "")).encode()).hexdigest()[:12]


def reset_token(email: str) -> str:
    email = email.strip().lower()
    m = models_by_email().get(email) or {}
    exp = int(time.time()) + RESET_TTL
    body = f"{email}|{exp}|{_pw_tag(m.get('password', ''))}"
    sig = hmac.new(_secret(), ("reset|" + body).encode(), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode().rstrip("=")


def reset_email(token: str) -> str | None:
    """Valid, unexpired, unused reset token -> e-mail, else None."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        email, exp, tag, sig = raw.split("|")
        body = f"{email}|{exp}|{tag}"
        if int(exp) < time.time() or not hmac.compare_digest(hmac.new(_secret(), ("reset|" + body).encode(), hashlib.sha256).hexdigest()[:40], sig):
            return None
        m = models_by_email().get(email)
        if not access_allowed(m) or not hmac.compare_digest(_pw_tag(m["password"]), tag):
            return None
        return email
    except Exception:  # noqa: BLE001
        return None


def send_reset(email: str) -> bool:
    """Mail the "neues Passwort festlegen" link. Silent for unknown e-mails (the page never says who has access)."""
    email = email.strip().lower()
    m = models_by_email().get(email)
    if not access_allowed(m):
        return False
    link = f"https://portal.northfeed-management.com/portal/reset?t={reset_token(email)}"
    if str(m.get("language") or "DE").upper().startswith("EN"):
        return _send(email, "Set a new password for your Northfeed portal",
                     f"Hi {m.get('name', '')},\n\nto set a new password for your Northfeed portal, open this link:\n{link}\n\n"
                     f"It is valid for 30 minutes and works once. If you did not ask for it, just ignore this mail; your password stays the same.\n\nThe Northfeed team")
    return _send(email, "Neues Passwort für dein Northfeed-Portal",
                 f"Hi {m.get('name', '')},\n\nüber diesen Link legst du ein neues Passwort für dein Northfeed-Portal fest:\n{link}\n\n"
                 f"Er gilt 30 Minuten und funktioniert einmal. Wenn du das nicht warst, ignorier diese Mail einfach, dein Passwort bleibt dann gleich.\n\nDein Northfeed-Team")


def set_password(email: str, new: str) -> tuple[bool, str]:
    """Write a new "Portal password" for the model with this e-mail. Returns (ok, error text in German)."""
    from . import manager
    new = (new or "").strip()
    if len(new) < MIN_PW:
        return False, f"Bitte mindestens {MIN_PW} Zeichen."
    m = models_by_email().get((email or "").strip().lower())
    if not m:
        return False, "Konto nicht gefunden."
    manager._update(portal.T_MODELS, m["id"], {"Portal password": new})
    portal.bust(portal.T_MODELS)
    _fails.pop(email, None)
    return True, ""


def throttled(email: str) -> bool:
    now = time.time()
    _fails[email] = [t for t in _fails.get(email, []) if now - t < 900]
    return len(_fails[email]) >= 10


def lookup(email: str) -> dict:
    """Session -> model row from the short cache; a miss (new model) reads fresh."""
    email = (email or "").strip().lower()
    d = models_by_email(cached=True)
    if email not in d:
        portal.bust(portal.T_MODELS)
        d = models_by_email(cached=True)
    return d.get(email, {})


def check(email: str, password: str) -> dict | None:
    """E-mail + "Portal password" -> model record, else None (10 failures in 15 min pause that e-mail)."""
    email = email.strip().lower()
    if throttled(email):
        return None
    m = models_by_email().get(email)
    if access_allowed(m) and m["password"] and hmac.compare_digest(m["password"], password.strip()):
        _fails.pop(email, None)
        return m
    _fails.setdefault(email, []).append(time.time())
    return None


def issue(email: str) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    body = f"{email.strip().lower()}|{exp}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{body}|{sig}"


def read(token: str) -> str | None:
    """Cookie value -> e-mail, or None."""
    try:
        email, exp, sig = token.split("|")
        body = f"{email}|{exp}"
        if int(exp) < time.time():
            return None
        if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:40], sig):
            return None
        return email
    except Exception:  # noqa: BLE001
        return None


def login_page(lang: str = "en", step: str = "login", email: str = "", error: str = "", token: str = "") -> str:
    """step: login (e-mail + password), forgot (ask for the reset link), sent (link is on its way), reset (new password)."""
    L = {
        "en": dict(title="Sign in", lead="Your plan, your tasks, your numbers.", email="E-mail", pw="Password", login="Sign in",
                   forgot="Forgot your password?", forgot_h="New password", forgot_lead="Enter your e-mail. We'll send you a link to set a new password.",
                   send="Send link", sent="If this e-mail has portal access, the link is on its way. Check your inbox (and spam). It is valid for 30 minutes.",
                   reset_lead="Choose your new password, at least 8 characters.", new_pw="New password", save="Save and sign in",
                   wrong="E-mail or password is not right. Check and try again.", unknown="This e-mail has no portal access yet. Ask your team.",
                   locked="Your access opens as soon as you have signed your contract. Check your e-mails for the signing link.",
                   ended="Your portal access has ended. If you have questions, message us on WhatsApp.",
                   throttled="Too many attempts. Please wait 15 minutes or set a new password.", expired="This link has expired or was already used. Request a new one.",
                   short="Please use at least 8 characters.", back="Back to sign in"),
        "de": dict(title="Anmelden", lead="Dein Plan, deine Aufgaben, deine Zahlen.", email="E-Mail", pw="Passwort", login="Anmelden",
                   forgot="Passwort vergessen?", forgot_h="Neues Passwort", forgot_lead="Gib deine E-Mail ein. Wir schicken dir einen Link, mit dem du ein neues Passwort festlegst.",
                   send="Link schicken", sent="Wenn diese E-Mail einen Portal-Zugang hat, ist der Link unterwegs. Schau in dein Postfach (auch in den Spam). Er gilt 30 Minuten.",
                   reset_lead="Wähle dein neues Passwort, mindestens 8 Zeichen.", new_pw="Neues Passwort", save="Speichern und anmelden",
                   wrong="E-Mail oder Passwort stimmt nicht. Prüfen und nochmal.", unknown="Diese E-Mail hat noch keinen Portal-Zugang. Frag dein Team.",
                   locked="Dein Zugang wird freigeschaltet, sobald du deinen Vertrag unterschrieben hast. Den Link zum Unterschreiben findest du in deinen Mails.",
                   ended="Dein Portal-Zugang ist beendet. Bei Fragen schreib uns auf WhatsApp.",
                   throttled="Zu viele Versuche. Warte 15 Minuten oder leg ein neues Passwort fest.", expired="Dieser Link ist abgelaufen oder wurde schon benutzt. Fordere einfach einen neuen an.",
                   short="Bitte mindestens 8 Zeichen.", back="Zurück zum Anmelden"),
        "th": dict(title="เข้าสู่ระบบ", lead="แผนของคุณ งานของคุณ ตัวเลขของคุณ", email="อีเมล", pw="รหัสผ่าน", login="เข้าสู่ระบบ",
                   forgot="ลืมรหัสผ่าน?", forgot_h="รหัสผ่านใหม่", forgot_lead="กรอกอีเมลของคุณ เราจะส่งลิงก์สำหรับตั้งรหัสผ่านใหม่",
                   send="ส่งลิงก์", sent="หากอีเมลนี้มีสิทธิ์เข้าพอร์ทัล ลิงก์กำลังส่งไป ตรวจสอบกล่องจดหมาย (และสแปม) ลิงก์ใช้ได้ 30 นาที",
                   reset_lead="ตั้งรหัสผ่านใหม่ อย่างน้อย 8 ตัวอักษร", new_pw="รหัสผ่านใหม่", save="บันทึกและเข้าสู่ระบบ",
                   wrong="อีเมลหรือรหัสผ่านไม่ถูกต้อง", unknown="อีเมลนี้ยังไม่มีสิทธิ์เข้าพอร์ทัล ติดต่อทีมของคุณ",
                   locked="สิทธิ์เข้าใช้งานจะเปิดหลังจากคุณเซ็นสัญญาแล้ว", ended="สิทธิ์เข้าพอร์ทัลของคุณสิ้นสุดแล้ว", throttled="ลองหลายครั้งเกินไป รอ 15 นาที", expired="ลิงก์หมดอายุหรือถูกใช้แล้ว",
                   short="อย่างน้อย 8 ตัวอักษร", back="กลับไปเข้าสู่ระบบ"),
    }.get(lang, None) or {}
    if not L:
        return login_page("en", step, email, error, token)
    import base64
    logo = "data:image/png;base64," + base64.b64encode(portal.LOGO.read_bytes()).decode() if portal.LOGO.exists() else ""
    err = L.get(error, "") if error else ""
    esc = __import__("html").escape
    if step == "forgot":
        form = f'''<p class="hint">{L["forgot_lead"]}</p><form method="post" action="/portal/forgot"><label>{L["email"]}<input name="email" type="email" required autocomplete="email" value="{esc(email)}"></label>
          <button class="btn primary" type="submit">{L["send"]}</button></form><a class="back" href="/">{L["back"]}</a>'''
    elif step == "sent":
        form = f'''<p class="ok">{L["sent"]}</p><a class="back" href="/">{L["back"]}</a>'''
    elif step == "reset":
        form = f'''<p class="hint">{L["reset_lead"]}</p><form method="post" action="/portal/reset"><input type="hidden" name="t" value="{esc(token)}">
          <input type="email" name="email" value="{esc(email)}" autocomplete="username" style="display:none">
          <label>{L["new_pw"]}<input name="password" type="password" required minlength="{MIN_PW}" autofocus autocomplete="new-password"></label>
          <button class="btn primary" type="submit">{L["save"]}</button></form>'''
    else:
        form = f'''<form method="post" action="/portal/login"><label>{L["email"]}<input name="email" type="email" required autocomplete="email" value="{esc(email)}"{"" if email else " autofocus"}></label>
          <label>{L["pw"]}<input name="secret" type="password" required autocomplete="current-password"{" autofocus" if email else ""}></label>
          <button class="btn primary" type="submit">{L["login"]}</button></form><a class="back" href="/portal/forgot{"?email=" + esc(email) if email else ""}">{L["forgot"]}</a>'''
    h1 = f'Northfeed <em>Portal</em>' if step == "login" else f'{L["forgot_h"]}'
    return f'''<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Northfeed · {L["title"]}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;1,9..144,500&family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,700&family=Noto+Sans+Thai:wght@400;600;700&display=swap">
<style>:root{{color-scheme:dark;--bg:#0b0d12;--panel:#161a22;--line2:#343c4d;--fg:#e6e8ee;--muted:#9aa3b2;--pink:#ff5c9a;--pink2:#ff9ec4}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.5 "Bricolage Grotesque","Noto Sans Thai",sans-serif;
background-image:radial-gradient(900px 500px at 85% -10%,rgba(255,92,154,.18),transparent 60%),radial-gradient(700px 400px at -10% 30%,rgba(255,158,196,.08),transparent 60%);padding:20px}}
.box{{position:relative;z-index:1;width:100%;max-width:400px;background:linear-gradient(180deg,rgba(26,23,38,.75),rgba(18,16,28,.85));border:1px solid var(--line2);border-radius:20px;padding:30px 28px;box-shadow:0 20px 60px rgba(0,0,0,.35)}}
.logo img{{height:30px;display:block;margin-bottom:28px}}h1{{font-family:Fraunces,"Noto Sans Thai",serif;font-weight:500;font-size:34px;margin:0 0 4px;letter-spacing:-.01em}}h1 em{{font-style:italic;color:var(--pink2)}}
.lead{{color:var(--muted);margin:0 0 22px}}label{{display:block;font-size:13px;color:var(--muted);margin-bottom:14px}}
input{{display:block;width:100%;margin-top:6px;background:var(--panel);color:var(--fg);border:1px solid var(--line2);border-radius:12px;padding:12px 14px;font:inherit;font-size:16px}}
input:focus{{outline:2px solid var(--pink);border-color:transparent}}.btn{{width:100%;border:0;border-radius:999px;padding:13px 18px;font:inherit;font-weight:700;font-size:15px;cursor:pointer;color:#fff;background:linear-gradient(135deg,var(--pink),#e03d7e);box-shadow:0 10px 30px rgba(255,92,154,.3)}}
.err{{color:#ff9b9b;font-size:13.5px;margin:0 0 14px}}.hint{{color:var(--muted);font-size:13.5px;margin:0 0 16px}}.back{{display:block;text-align:center;color:var(--muted);font-size:13px;margin-top:16px;text-decoration:none}}
.ok{{background:#10291c;color:#5fd39a;border-radius:12px;padding:12px 14px;font-size:14px;margin:0 0 6px}}
.foot{{color:#6b7486;font-size:12px;text-align:center;margin-top:22px}}</style></head>
<body><div class="box"><a class="logo" href="/"><img src="{logo}" alt="Northfeed"></a><h1>{h1}</h1>{f'<p class="lead">{L["lead"]}</p>' if step == "login" else ""}
{f'<p class="err">{err}</p>' if err else ''}{form}<div class="foot">Northfeed · OnlyFans Management</div></div><script>{HEARTS}</script></body></html>'''
