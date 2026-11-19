"""„Dein Konto" (16.11 Rene): opened from the avatar. The invoices (open one + PDF, link to all), the contract (signed on + PDF), the login e-mail, which she
may change herself (Rene gets a Telegram and sees the current address on her model page), and her password
(16.11: password-only login). No package, no split, no personal data on this page.
"""

from __future__ import annotations

import datetime as dt
import html
import re

from . import portal


def _row(rec: str) -> dict:
    return next((r["fields"] for r in portal.fetch_cached(portal.T_MODELS, 20) if r["id"] == rec), {})


def _date(v: str, de: bool) -> str:
    try:
        d = dt.date.fromisoformat(str(v)[:10])
        return d.strftime("%d.%m.%Y") if de else d.strftime("%b %d, %Y")
    except ValueError:
        return str(v or "")


def change_email(rec: str, old: str, new: str) -> tuple[bool, str]:
    from . import manager, portal_auth, cal_booking
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", new or ""):
        return False, "Bitte eine gültige E-Mail-Adresse eingeben."
    if new == (old or "").lower():
        return False, "Das ist schon deine E-Mail."
    if new in portal_auth.models_by_email() or new in manager.managers_by_email():
        return False, "Diese E-Mail wird schon verwendet."
    manager._update(portal.T_MODELS, rec, {"Email": new})
    portal.bust(portal.T_MODELS)
    name = str(_row(rec).get("Name") or "")
    cal_booking._tg(f"✉️ E-Mail geändert · {name}\n{old} → {new}\nDrive-Freigabe ggf. auf die neue Adresse umstellen.\n"
                    f"https://portal.northfeed-management.com/manager#model/{rec}")
    return True, ""


def _billing(rec: str, de: bool) -> str:
    """16.11 Rene: the invoices belong in the profile too - the open one at a glance, the rest one tap away."""
    from . import billing
    esc = html.escape
    try:
        rows = [r for r in portal.fetch_cached(billing.T_BILL, 20) if rec in (r["fields"].get("Model") or []) and r["fields"].get("Status") != "Offen"]
    except Exception as e:  # noqa: BLE001
        print("[konto] billing", e); rows = []
    rows.sort(key=lambda r: r["fields"].get("Week start", ""), reverse=True)
    open_ = [r for r in rows if r["fields"].get("Status") in ("Rechnung raus", "Überfällig")]
    lead = ("Jeden Montag kommt hier die Rechnung für die Vorwoche. Bitte überweise bis Mittwoch." if de
            else "Every Monday the invoice for the previous week shows up here. Please pay by Wednesday.")
    if not rows:
        return (f'<p class="hint" style="margin-top:0">{lead}</p><p class="kv"><span>{"Noch keine Rechnung." if de else "No invoice yet."}</span></p>')
    out = f'<p class="hint" style="margin-top:0">{lead}</p>'
    for r in (open_ or rows[:1]):
        f = r["fields"]
        ws = dt.date.fromisoformat(f["Week start"])
        st = f.get("Status", "")
        col = {"Bezahlt": "#5fd39a", "Nachweis da": "#7aa8ff", "Überfällig": "#ff9b9b"}.get(st, "#ffb35c")
        out += (f'<div class="kv"><span>KW {billing._kw(ws)} · {billing._eur(f.get("Amount"))}</span><b style="color:{col}">{esc(st)}</b></div>'
                f'<div class="kv" style="padding-top:0"><span>{"fällig" if de else "due"} {billing._de(f.get("Due", ""))}</span>'
                f'<a href="/abrechnung/pdf/{r["id"]}" target="_blank" style="color:#ff9ec4;font-weight:600">{"Rechnung (PDF)" if de else "Invoice (PDF)"}</a></div>')
    out += (f'<a class="btn" style="display:inline-block;width:auto;text-decoration:none;padding:13px 24px;margin-top:14px" href="/abrechnung">'
            f'{"Alle Rechnungen" if de else "All invoices"} →</a>')
    return out


def page(rec: str, saved: bool = False, err: str = "", pw_saved: bool = False, pw_err: str = "") -> bytes:
    from .contract_data import _shell
    from .onboarding import CONTRACT_DIR
    f = _row(rec)
    de = not str(f.get("Language") or "DE").upper().startswith("EN")
    esc = html.escape
    signed = f.get("Contract signed")
    has_pdf = (CONTRACT_DIR / f"{rec}.pdf").exists() or "DocuSeal submission" in str(f.get("Notes") or "")
    contract = (f'<div class="kv"><span>{"Unterschrieben am" if de else "Signed on"}</span><b>{esc(_date(signed, de)) if signed else ("noch nicht" if de else "not yet")}</b></div>'
                + (f'<a class="btn" style="display:inline-block;width:auto;text-decoration:none;padding:13px 24px;margin-top:14px" href="/konto/vertrag.pdf" target="_blank">'
                   f'{"Vertrag als PDF herunterladen" if de else "Download contract (PDF)"}</a>' if has_pdf else ""))
    login = (f'<form method="post" action="/konto/email"><label>{"Deine E-Mail zum Einloggen" if de else "Your sign-in e-mail"}'
             f'<input type="email" name="email" value="{esc(str(f.get("Email") or ""))}" required></label>'
             f'{"<p class=ok>Gespeichert. Ab jetzt loggst du dich mit der neuen E-Mail ein.</p>" if saved and de else "<p class=ok>Saved. From now on you sign in with the new e-mail.</p>" if saved else ""}'
             f'{f"<p class=err>{esc(err)}</p>" if err else ""}'
             f'<button class="btn" type="submit" style="width:auto;padding:12px 22px">{"E-Mail ändern" if de else "Change e-mail"}</button></form>'
             f'<p class="hint">{"Nimm am besten deine Gmail, mit der du auch in der Google-Drive-App angemeldet bist. An diese Adresse kommt auch der Link, falls du dein Passwort vergisst." if de else "Best use the Gmail you also use for the Google Drive app. A link to reset your password also goes to this address."}</p>'
             f'<form method="post" action="/konto/password" style="margin-top:22px"><input type="email" name="email" value="{esc(str(f.get("Email") or ""))}" autocomplete="username" style="display:none">'
             f'<label>{"Neues Passwort" if de else "New password"}<input type="password" name="password" minlength="8" required autocomplete="new-password" placeholder="{"mindestens 8 Zeichen" if de else "at least 8 characters"}"></label>'
             f'{("<p class=ok>Neues Passwort gespeichert.</p>" if de else "<p class=ok>New password saved.</p>") if pw_saved else ""}'
             f'{f"<p class=err>{esc(pw_err)}</p>" if pw_err else ""}'
             f'<button class="btn" type="submit" style="width:auto;padding:12px 22px">{"Passwort ändern" if de else "Change password"}</button></form>')
    inner = (f'<h1>{"Dein" if de else "Your"} <em>{"Konto" if de else "account"}</em></h1>'
             f'<div class="kg"><section class="kc"><h2>{"Deine Abrechnung" if de else "Your invoices"}</h2>{_billing(rec, de)}</section>'
             f'<section class="kc"><h2>{"Dein Vertrag" if de else "Your contract"}</h2>{contract}</section>'
             f'<section class="kc"><h2>{"Dein Login" if de else "Your login"}</h2>{login}</section></div>'
             f'<div class="kl"><a href="/zugaenge">{"Zugänge" if de else "Logins"} →</a><a href="/abrechnung">{"Abrechnung" if de else "Invoices"} →</a><a href="/logout">{"Abmelden" if de else "Sign out"}</a></div>')
    extra = ('<style>.kg{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-top:10px}'
             '.kc{background:#161a22;border:1px solid #2a303d;border-radius:20px;padding:22px}.kc h2{font-family:Fraunces,serif;font-weight:500;font-size:24px;margin:0 0 12px}'
             '.kv{display:flex;justify-content:space-between;gap:14px;padding:10px 0;font-size:15px}.kv span{color:#9aa3b2}.kv b{text-align:right;font-weight:600}'
             '.hint{color:#9aa3b2;font-size:13.5px;margin:12px 0 0}.ok{background:#10291c;color:#5fd39a;border-radius:10px;padding:10px 12px}'
             '.kl{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}.kl a{color:#e6e8ee;text-decoration:none;font-weight:600;font-size:14px;border:1px solid #343c4d;border-radius:999px;padding:10px 16px}'
             '.kl a:hover{border-color:#ff5c9a}</style>')
    return _shell(inner).replace(b"</head>", extra.encode() + b"</head>", 1)
