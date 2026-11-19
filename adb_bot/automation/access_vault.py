"""Model access data (15.11, Rene "H": Zugänge bei den Models).

Contract § 4 Abs. 3/4: the model keeps her OnlyFans login and grants CRM access (Infloww/Supercreator); for the
marketing platforms she hands over the logins. She enters them in the portal (/zugaenge); passwords are encrypted
with a server key (/etc/northfeed/vault.key, Fernet) and stored in /opt/northfeed-vault/<rec>.json -- never in
Airtable, never in a mail. Airtable only gets "Access saved" (date). The manager sees platform + username and can
reveal a password on click; every reveal is appended to /opt/northfeed-vault/audit.log.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILE = Path("/etc/northfeed/vault.key")
DIR = Path("/opt/northfeed-vault")
PLATFORMS = ["Instagram", "TikTok", "X", "Reddit", "Threads", "Snapchat", "Anderes"]
FAN_PLATFORMS = ["OnlyFans", "Andere"]                               # 16.11 Rene: OnlyFans is what counts; "Andere" for the rare extra page
SOCIAL_PLATFORMS = ["Instagram", "TikTok", "Andere"]                 # her OWN accounts only, not the ones the agency creates


def _fernet() -> Fernet:
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
        os.chmod(KEY_FILE, 0o600)
    return Fernet(KEY_FILE.read_bytes().strip())


def _path(rec: str) -> Path:
    if not rec.startswith("rec") or "/" in rec:
        raise ValueError("bad record")
    return DIR / f"{rec}.json"


def load(rec: str) -> dict:
    p = _path(rec)
    if not p.exists():
        return {"onlyfans": "", "crm": False, "accounts": [], "updated": ""}
    return json.loads(p.read_text())


def _save(rec: str, d: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    p = _path(rec)
    p.write_text(json.dumps(d, ensure_ascii=False))
    os.chmod(p, 0o600)


def public(rec: str) -> dict:
    """Without passwords: what the model page and the manager list show."""
    d = load(rec)
    return {"onlyfans": d.get("onlyfans", ""), "crm": bool(d.get("crm")), "updated": d.get("updated", ""),
            "accounts": [{"i": i, "platform": a["platform"], "username": a["username"], "note": a.get("note", ""), "hasPassword": bool(a.get("pw")),
                          "kind": a.get("kind") or ("fan" if a["platform"] in FAN_PLATFORMS else "social"), "link": a.get("link", ""), "email": a.get("email", "")}
                         for i, a in enumerate(d.get("accounts", []))]}


def reveal(rec: str, idx: int, by: str) -> str:
    a = load(rec)["accounts"][int(idx)]
    with (DIR / "audit.log").open("a") as fh:
        fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {by} revealed {rec} #{idx} {a['platform']} {a['username']}\n")
    return _fernet().decrypt(a["pw"].encode()).decode() if a.get("pw") else ""


def submit(rec: str, form: dict) -> dict:
    """16.11 form: fan pages fplat_N/fuser_N/fpw_N/flink_N/fnote_N and own socials splat_N/suser_N/spw_N/snote_N.
    An empty password keeps the stored one; the old onlyfans/crm fields are derived/kept for the manager view."""
    g = lambda k: (form.get(k) or [""])[0].strip() if isinstance(form.get(k), list) else str(form.get(k) or "").strip()  # noqa: E731
    old = load(rec)
    olda = {(a["platform"], a["username"].lower()): a for a in old.get("accounts", [])}
    f = _fernet()
    accounts = []
    for kind, pre, n_max in (("fan", "f", 8), ("social", "s", 12)):
        for n in range(n_max):
            plat, user = g(f"{pre}plat_{n}"), g(f"{pre}user_{n}").lstrip("@")
            mail = g(f"{pre}email_{n}")[:200] if kind == "fan" else ""     # 17.11 Rene: OnlyFans needs username AND e-mail, two fields
            if not plat or not (user or mail):
                continue
            pw = g(f"{pre}pw_{n}")
            prev = olda.get((plat, user.lower()), {}) or olda.get((plat, mail.lower()), {})
            accounts.append({"platform": plat, "username": user, "email": mail, "kind": kind, "note": g(f"{pre}note_{n}")[:200],
                             "link": g(f"{pre}link_{n}")[:300] if kind == "fan" else "",
                             "pw": f.encrypt(pw.encode()).decode() if pw else prev.get("pw", "")})
    of = next((a for a in accounts if a["platform"] == "OnlyFans"), None)
    d = {"onlyfans": (of or {}).get("link") or old.get("onlyfans", ""), "crm": bool(old.get("crm")), "accounts": accounts,
         "updated": dt.date.today().isoformat()}
    _save(rec, d)
    return d


def page(rec: str, name: str, saved: bool = False, lang: str = "DE") -> bytes:
    from .contract_data import _shell
    d = public(rec)
    esc = html.escape
    fans = [a for a in d["accounts"] if a["kind"] == "fan"]
    socials = [a for a in d["accounts"] if a["kind"] == "social"]
    if not fans and d.get("onlyfans"):                        # data from the old form: the OnlyFans link alone
        fans = [{"platform": "OnlyFans", "username": "", "note": "", "hasPassword": False, "link": d["onlyfans"]}]

    DEL = '<button type="button" class="del" onclick="drop(this)">Entfernen</button>'   # 16.11 Rene: fan pages and accounts can be removed

    def chips(pre, n, plats, cur):
        return (f'<input type="hidden" name="{pre}plat_{n}" value="{esc(cur)}"><div class="chips">' + "".join(
            f'<button type="button" class="{"on" if p == cur else ""}" onclick="pick(this,\'{p}\')">{p}</button>' for p in plats) + "</div>")

    def fan_row(n, a, hidden):
        return (f'<div class="acc" {"hidden" if hidden else ""}><div class="grid"><div class="full"><div class="lrow"><span class="lbl">Seite</span>{DEL if n else ""}</div>{chips("f", n, FAN_PLATFORMS, a.get("platform") or ("OnlyFans" if n == 0 else ""))}</div>'
                f'<label>Benutzername<input name="fuser_{n}" value="{esc(a.get("username", ""))}" autocomplete="off" placeholder="ohne @"></label>'
                f'<label>E-Mail<input name="femail_{n}" type="email" value="{esc(a.get("email", ""))}" autocomplete="off"></label>'
                f'<label>Passwort{" (gespeichert, leer lassen = behalten)" if a.get("hasPassword") else ""}<input name="fpw_{n}" type="password" autocomplete="new-password" placeholder="{"••••••••" if a.get("hasPassword") else ""}"></label>'
                f'<label>Link zu deinem Profil<input name="flink_{n}" value="{esc(a.get("link", ""))}" placeholder="https://onlyfans.com/…"></label>'
                f'<label class="full">Hinweis, z. B. wohin der Login-Code kommt<input name="fnote_{n}" value="{esc(a.get("note", ""))}"></label></div></div>')

    def social_row(n, a, hidden):
        return (f'<div class="acc" {"hidden" if hidden else ""}><div class="grid"><div class="full"><div class="lrow"><span class="lbl">Plattform</span>{DEL if n else ""}</div>{chips("s", n, SOCIAL_PLATFORMS, a.get("platform") or ("Instagram" if n == 0 else ""))}</div>'
                f'<label>Benutzername<input name="suser_{n}" value="{esc(a.get("username", ""))}" autocomplete="off" placeholder="ohne @"></label>'
                f'<label>Passwort{" (gespeichert, leer lassen = behalten)" if a.get("hasPassword") else ""}<input name="spw_{n}" type="password" autocomplete="new-password" placeholder="{"••••••••" if a.get("hasPassword") else ""}"></label>'
                f'<label class="full">Hinweis, z. B. wohin der Login-Code kommt<input name="snote_{n}" value="{esc(a.get("note", ""))}"></label></div></div>')

    fan_html = "".join(fan_row(n, fans[n] if n < len(fans) else {}, n >= max(len(fans), 1)) for n in range(max(3, len(fans))))   # 16.11: a few, not thousands
    soc_html = "".join(social_row(n, socials[n] if n < len(socials) else {}, n >= max(len(socials), 1)) for n in range(max(6, len(socials))))
    first = esc((name or "").split(" ")[0])
    inner = f'''<h1>Deine <em>Zugänge</em></h1><p class="lead">Hi {first}, damit wir uns für dich einloggen und deine Seiten betreuen können. Deine Passwörter werden verschlüsselt gespeichert und nur von unserem Team genutzt.</p>
{'<div class="ok"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg><div><b>Gespeichert</b><span>Deine Zugänge sind sicher hinterlegt.</span></div></div>' if saved else ''}
<form method="post" action="/zugaenge">
<section class="sec" data-add="+ Weitere Fanseite"><h2>Dein OnlyFans</h2><p class="sub">Dein Benutzername, deine E-Mail und dein Passwort für OnlyFans. Falls wir noch eine andere Fanseite für dich betreuen, fügst du sie darunter hinzu.</p>
<div class="rows">{fan_html}</div><button type="button" class="add" onclick="more(this)">+ Weitere Fanseite</button></section>
<section class="sec" data-add="+ Weiterer Account"><h2>Instagram &amp; Marketing-Accounts <span class="opt">nach Absprache</span></h2><p class="sub">Die meisten Accounts führen wir für dich, die trägst du hier nicht ein. Nur wenn wir mit dir abgesprochen haben, dass wir einen Account nutzen, den du selbst führst, oder den eines größeren Influencers, trägst du ihn hier ein.</p>
<div class="rows">{soc_html}</div><button type="button" class="add" onclick="more(this)">+ Weiterer Account</button></section>
<button class="btn" type="submit">Speichern</button></form>
<script>function pick(b,p){{const g=b.parentElement;g.previousElementSibling.value=p;g.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));}}
function drop(b){{const a=b.closest('.acc');a.querySelectorAll('input').forEach(i=>{{i.value='';}});a.querySelectorAll('.chips button').forEach(x=>x.classList.remove('on'));a.hidden=true;
  const sec=a.closest('.sec');if(!sec.querySelector('.add')){{const n=document.createElement('button');n.type='button';n.className='add';n.textContent=sec.dataset.add;n.onclick=()=>more(n);sec.querySelector('.rows').after(n);}}}}
function more(b){{const h=b.previousElementSibling.querySelector('.acc[hidden]');if(h){{h.hidden=false;const c=h.querySelector('.chips button');if(c&&!h.querySelector('input[type=hidden]').value)pick(c,c.textContent);}}if(!b.previousElementSibling.querySelector('.acc[hidden]'))b.remove();}}</script>'''
    extra = ('<style>.sec{background:#161a22;border:1px solid #2a303d;border-radius:20px;padding:22px 22px 18px;margin:0 0 18px}'
             'h2{font-family:Fraunces,serif;font-weight:500;font-size:24px;margin:0 0 4px}.sub{color:#9aa3b2;font-size:14.5px;margin:0 0 14px}'
             '.opt{font-family:"Bricolage Grotesque",sans-serif;font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#9aa3b2;border:1px solid #343c4d;border-radius:999px;padding:3px 9px;vertical-align:middle;margin-left:6px}'
             '.acc{position:relative;border-top:1px solid #2a303d;padding-top:14px;margin-top:4px}'
             '.lrow{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}.lrow .lbl{margin:0}'
             '.del{font:inherit;font-size:13px;font-weight:600;color:#9aa3b2;background:none;border:1px solid #343c4d;border-radius:999px;padding:6px 12px;cursor:pointer;flex:none}'
             '.del:hover{color:#ff8a8a;border-color:#ff8a8a}.acc:first-child{border-top:0;padding-top:0}'
             '.lbl{display:block;font-size:13px;color:#9aa3b2;margin-bottom:6px}.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}'
             '.chips button{font:inherit;font-size:14px;font-weight:600;padding:8px 14px;border-radius:999px;border:1px solid #343c4d;background:#0f1218;color:#9aa3b2;cursor:pointer}'
             '.chips button.on{background:rgba(255,92,154,.18);border-color:#ff5c9a;color:#fff}'
             '.add{font:inherit;font-weight:700;font-size:14px;color:#ff9ec4;background:none;border:1px dashed rgba(255,92,154,.5);border-radius:999px;padding:9px 16px;cursor:pointer;margin-top:4px}'
             '.ok{display:flex;align-items:center;gap:14px;background:rgba(16,41,28,.7);border:1px solid rgba(95,211,154,.35);color:#e6f7ee;border-radius:16px;padding:14px 18px;margin:0 0 18px}'
             '.ok svg{width:26px;height:26px;flex:none;color:#5fd39a;background:rgba(95,211,154,.14);border-radius:50%;padding:5px}'
             '.ok b{display:block;font-size:16px}.ok span{display:block;color:#9fd8b9;font-size:14px}'
             '@media(min-width:760px){.grid{grid-template-columns:1fr 1fr}}</style>')
    return _shell(inner).replace(b"</head>", extra.encode() + b"</head>", 1)
