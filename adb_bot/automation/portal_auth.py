"""Own login for portal.northfeed-management.com (16.10, Rene: "soll nicht verbunden sein" -- no Cloudflare
Access, no RNX Viewer branding). A model signs in with the e-mail from the Models table plus either a 6-digit
code sent to that e-mail (when PORTAL_SMTP_* is configured) or the "Portal password" from her Models row.
The session is a signed cookie (nf_session), 30 days.
"""

from __future__ import annotations

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

SECRET_FILE = Path(os.environ.get("PORTAL_SECRET_FILE", "/etc/adbbot/portal_secret"))
SESSION_DAYS = 30
CODE_TTL = 600
_codes: dict[str, tuple[str, float]] = {}        # email -> (code, expires)


def _secret() -> bytes:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_text().strip().encode()


def models_by_email() -> dict[str, dict]:
    out = {}
    for m in portal._fetch(portal.T_MODELS):
        f = m["fields"]
        mail = str(f.get("Email") or "").strip().lower()
        if mail:
            out[mail] = {"name": f.get("Name", ""), "password": str(f.get("Portal password") or ""), "status": f.get("Status", "")}
    return out


def smtp_ready() -> bool:
    return bool(os.environ.get("PORTAL_SMTP_HOST") and os.environ.get("PORTAL_SMTP_FROM"))


def send_code(email: str) -> bool:
    code = f"{secrets.randbelow(900000) + 100000}"
    _codes[email] = (code, time.time() + CODE_TTL)
    if not smtp_ready():
        print(f"portal login code for {email}: {code}")           # journal only, until an SMTP sender exists
        return False
    msg = EmailMessage()
    msg["From"] = os.environ["PORTAL_SMTP_FROM"]
    msg["To"] = email
    msg["Subject"] = f"{code} is your Northfeed code"
    msg.set_content(f"Your Northfeed login code: {code}\n\nIt is valid for 10 minutes. If you did not request it, ignore this mail.")
    host, port = os.environ["PORTAL_SMTP_HOST"], int(os.environ.get("PORTAL_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        if os.environ.get("PORTAL_SMTP_USER"):
            s.login(os.environ["PORTAL_SMTP_USER"], os.environ.get("PORTAL_SMTP_PASSWORD", ""))
        s.send_message(msg)
    return True


def check(email: str, secret_or_code: str) -> dict | None:
    """Password from the Models row or a sent code -> model record, else None."""
    email = email.strip().lower()
    m = models_by_email().get(email)
    if not m or m["status"] in ("Ended",):
        return None
    given = secret_or_code.strip()
    c = _codes.get(email)
    if c and time.time() < c[1] and hmac.compare_digest(c[0], given):
        _codes.pop(email, None)
        return m
    if m["password"] and hmac.compare_digest(m["password"], given):
        return m
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


def login_page(lang: str = "en", step: str = "email", email: str = "", error: str = "", mode: str = "password") -> str:
    L = {
        "en": dict(title="Sign in", lead="Your plan, your tasks, your numbers.", email="E-mail", next="Continue", code="Code from your e-mail", pw="Password",
                   sent="We sent a code to", enter="Enter it below.", login="Sign in", wrong="That did not work. Check and try again.", unknown="This e-mail has no portal access yet. Ask your team.", back="Other e-mail"),
        "de": dict(title="Anmelden", lead="Dein Plan, deine Aufgaben, deine Zahlen.", email="E-Mail", next="Weiter", code="Code aus deiner E-Mail", pw="Passwort",
                   sent="Wir haben einen Code geschickt an", enter="Gib ihn unten ein.", login="Anmelden", wrong="Das hat nicht geklappt. Prüfen und nochmal.", unknown="Diese E-Mail hat noch keinen Portal-Zugang. Frag dein Team.", back="Andere E-Mail"),
        "th": dict(title="เข้าสู่ระบบ", lead="แผนของคุณ งานของคุณ ตัวเลขของคุณ", email="อีเมล", next="ต่อไป", code="รหัสจากอีเมลของคุณ", pw="รหัสผ่าน",
                   sent="เราส่งรหัสไปที่", enter="กรอกด้านล่าง", login="เข้าสู่ระบบ", wrong="ไม่สำเร็จ ตรวจสอบแล้วลองอีกครั้ง", unknown="อีเมลนี้ยังไม่มีสิทธิ์เข้าพอร์ทัล ติดต่อทีมของคุณ", back="อีเมลอื่น"),
    }.get(lang, None) or {}
    if not L:
        return login_page("en", step, email, error, mode)
    import base64
    logo = "data:image/png;base64," + base64.b64encode(portal.LOGO.read_bytes()).decode() if portal.LOGO.exists() else ""
    err = {"wrong": L["wrong"], "unknown": L["unknown"]}.get(error, "")
    if step == "email":
        form = f'''<form method="post" action="/portal/login"><label>{L["email"]}<input name="email" type="email" required autocomplete="email" value="{email}"></label>
          <button class="btn primary" type="submit">{L["login"]}</button></form>'''
    else:
        field = f'<label>{L["code"]}<input name="secret" type="text" inputmode="numeric" autocomplete="one-time-code" required autofocus></label>' if mode == "code" \
            else f'<label>{L["pw"]}<input name="secret" type="password" required autofocus autocomplete="current-password"></label>'
        hint = f'<p class="hint">{L["sent"]} <b>{email}</b>. {L["enter"]}</p>' if mode == "code" else ""
        form = f'''{hint}<form method="post" action="/portal/verify"><input type="hidden" name="email" value="{email}">{field}
          <button class="btn primary" type="submit">{L["login"]}</button></form><a class="back" href="/">{L["back"]}</a>'''
    return f'''<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Northfeed · {L["title"]}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;1,9..144,500&family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,700&family=Noto+Sans+Thai:wght@400;600;700&display=swap">
<style>:root{{color-scheme:dark;--bg:#0b0d12;--panel:#161a22;--line2:#343c4d;--fg:#e6e8ee;--muted:#9aa3b2;--pink:#ff5c9a;--pink2:#ff9ec4}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:15px/1.5 "Bricolage Grotesque","Noto Sans Thai",sans-serif;
background-image:radial-gradient(900px 500px at 85% -10%,rgba(255,92,154,.18),transparent 60%),radial-gradient(700px 400px at -10% 30%,rgba(255,158,196,.08),transparent 60%);padding:20px}}
.box{{width:100%;max-width:400px;background:linear-gradient(180deg,rgba(26,23,38,.75),rgba(18,16,28,.85));border:1px solid var(--line2);border-radius:20px;padding:30px 28px;box-shadow:0 20px 60px rgba(0,0,0,.35)}}
.logo img{{height:30px;display:block;margin-bottom:28px}}h1{{font-family:Fraunces,"Noto Sans Thai",serif;font-weight:500;font-size:34px;margin:0 0 4px;letter-spacing:-.01em}}h1 em{{font-style:italic;color:var(--pink2)}}
.lead{{color:var(--muted);margin:0 0 22px}}label{{display:block;font-size:13px;color:var(--muted);margin-bottom:14px}}
input{{display:block;width:100%;margin-top:6px;background:var(--panel);color:var(--fg);border:1px solid var(--line2);border-radius:12px;padding:12px 14px;font:inherit;font-size:16px}}
input:focus{{outline:2px solid var(--pink);border-color:transparent}}.btn{{width:100%;border:0;border-radius:999px;padding:13px 18px;font:inherit;font-weight:700;font-size:15px;cursor:pointer;color:#fff;background:linear-gradient(135deg,var(--pink),#e03d7e);box-shadow:0 10px 30px rgba(255,92,154,.3)}}
.err{{color:#ff9b9b;font-size:13.5px;margin:0 0 14px}}.hint{{color:var(--muted);font-size:13.5px;margin:0 0 16px}}.back{{display:block;text-align:center;color:var(--muted);font-size:13px;margin-top:16px;text-decoration:none}}
.foot{{color:#6b7486;font-size:12px;text-align:center;margin-top:22px}}</style></head>
<body><div class="box"><a class="logo" href="/"><img src="{logo}" alt="Northfeed"></a><h1>Northfeed <em>Portal</em></h1><p class="lead">{L["lead"]}</p>
{f'<p class="err">{err}</p>' if err else ''}{form}<div class="foot">Northfeed · OnlyFans Management</div></div></body></html>'''
