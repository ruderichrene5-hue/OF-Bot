"""One navigation for every model page (16.11 Rene: "unstrukturiert", sub-pages squeezed on the MacBook).

The home page (portal.html) builds its tabs from window.NFNAV; the other pages (Steckbrief/Bilder, Tutorials, Skripte,
Zugänge, Abrechnung) get the same bar injected and use the full width. While the onboarding is open the bar starts
with "Start" (open steps as a badge) and only Skripte carry a lock until Steckbrief + Bilder are in.
"""

from __future__ import annotations

import html
import json

from . import portal

LOCK = ('<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" style="margin-left:5px;vertical-align:-1px">'
        '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>')

LABELS = {
    "de": {"start": "Start", "home": "Übersicht", "plan": "Plan", "accounts": "Accounts", "course": "Onboarding", "skripte": "Skripte",
           "zugaenge": "Zugänge", "abrechnung": "Abrechnung"},
    "en": {"start": "Start", "home": "Overview", "plan": "Plan", "accounts": "Accounts", "course": "Onboarding", "skripte": "Scripts",
           "zugaenge": "Logins", "abrechnung": "Invoices"},
}
LOCKABLE: tuple = ()                        # 16.11 Rene: nothing locked; during onboarding the other pages are simply not shown


def ctx(email: str) -> dict:
    from . import billing, onboarding_portal, portal_auth
    me = portal_auth.lookup(email) or {}
    rec = me.get("id", "")
    row = next((r["fields"] for r in portal.fetch_cached(portal.T_MODELS, 20) if r["id"] == rec), {})
    lang = "en" if str(row.get("Language") or me.get("language") or "DE").upper().startswith("EN") else "de"
    open_steps, videos = 0, (0, 0)                           # 16.11 Rene: the welcome videos come first, nothing else until they are watched
    try:
        from . import course
        cat = course.catalog(email, "Model")
        ob = [c for c in cat["courses"] if any(k in str(c.get("title") or "").lower() for k in ("onboarding", "plattform", "platform"))]
        open_steps = sum(c["total"] - c["done"] for c in ob)
        videos = (sum(c["done"] for c in ob), sum(c["total"] for c in ob))
    except Exception as e:  # noqa: BLE001
        print("[frame] onboarding videos:", e)
    if row.get("Alte Agentur"):                              # 21.11 Rene: existing models from the old agency go straight into the portal
        open_steps = 0
    invoices = any(rec in (r["fields"].get("Model") or []) and r["fields"].get("Status") != "Offen"
                   for r in portal.fetch_cached(billing.T_BILL, 20))
    return {"rec": rec, "name": str(row.get("Name") or me.get("name") or ""), "lang": lang, "open": open_steps, "videos": videos,
            "accounts": str(row.get("Accounts im Portal") or "Nein") != "Nein", "invoices": invoices}


def items(c: dict) -> list:
    L = LABELS[c["lang"]]
    if c.get("chatter"):                                     # 18.11 Rene: chatters get the same bar, English, course first
        if c["open"]:
            return [{"k": "course", "href": "/course", "label": "Course", "locked": False}]
        out = [{"k": "home", "href": "/", "label": "Overview", "locked": False}, {"k": "course", "href": "/course", "label": "Course", "locked": False}]
        if c.get("mass"):                                    # 18.11 Rene: mass messages every 2 h (mass_messages.py)
            out.insert(1, {"k": "mass", "href": "/mass", "label": "Mass messages", "locked": False})
        if c.get("discord"):
            out.append({"k": "discord", "href": c["discord"], "label": "Discord", "locked": False, "ext": True})
        return out
    if c["open"]:                                            # 16.11 Rene: videos first, nothing else in the menu (not locked: not shown)
        return [{"k": "course", "href": "/course", "label": "Onboarding", "locked": False}]
    out = [{"k": "home", "href": "/#home", "label": L["home"]}, {"k": "plan", "href": "/#plan", "label": L["plan"]}]
    if c["accounts"]:
        out.append({"k": "accounts", "href": "/#accounts", "label": L["accounts"]})
    out += [{"k": "course", "href": "/course", "label": L["course"]}, {"k": "skripte", "href": "/skripte", "label": L["skripte"]},
            {"k": "zugaenge", "href": "/zugaenge", "label": L["zugaenge"]}]
    if c["invoices"]:
        out.append({"k": "abrechnung", "href": "/abrechnung", "label": L["abrechnung"]})
    for i in out:
        i["locked"] = bool(c["open"]) and i["k"] in LOCKABLE
    return out


CSS = """<style>
.pfn{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:12px 18px;padding:14px clamp(16px,4vw,48px);background:rgba(11,13,18,.78);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid #262c38;font-family:"Bricolage Grotesque",system-ui,sans-serif}
.pfn .pl{flex:none;display:flex;align-items:center;gap:9px;text-decoration:none}.pfn .pl img{height:26px;display:block}
.pfn .obs{margin-left:auto;font-size:13px;font-weight:700;color:#ff9ec4;letter-spacing:.02em;white-space:nowrap}.pfn .obs .sh{display:none}
@media(max-width:560px){.pfn .obs .lg{display:none}.pfn .obs .sh{display:inline;background:rgba(255,92,154,.14);border:1px solid rgba(255,92,154,.45);border-radius:999px;padding:5px 10px}}
.pfn .pl span{font-family:Fraunces,Georgia,serif;font-style:italic;font-weight:500;font-size:22px;color:#ff9ec4;line-height:1;position:relative;top:-1px}
.pfn .pt{display:flex;gap:4px;margin-left:auto;flex-wrap:nowrap}
.pfn .pt a{display:inline-flex;align-items:center;padding:8px 14px;border-radius:999px;color:#9aa3b2;font-weight:600;font-size:14px;white-space:nowrap;text-decoration:none;border:1px solid transparent}
.pfn .pt a:hover{color:#e6e8ee}.pfn .pt a.on{background:#161a22;color:#e6e8ee;border-color:#343c4d}
.pfn .pt a.lk{opacity:.55}.pfn .pt a.st{color:#fff;background:rgba(255,92,154,.16);border-color:rgba(255,92,154,.5)}
.pfn .pt a .bd{margin-left:7px;min-width:18px;height:18px;border-radius:999px;background:#ff5c9a;color:#fff;font-size:11px;font-weight:700;display:inline-grid;place-items:center;padding:0 5px}
.pfn .pm{display:flex;align-items:center;gap:10px;margin-left:12px;color:#9aa3b2;font-size:13px;flex:none;text-decoration:none}.pfn .pm:hover span{color:#fff}
.pfn .pm b{width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,#ff5c9a,#ffc2da);display:grid;place-items:center;color:#0b0d12;font-weight:700;font-size:13px}
.pfn .pb{display:none;width:42px;height:42px;border-radius:12px;border:1px solid #343c4d;background:#161a22;color:#e6e8ee;cursor:pointer;align-items:center;justify-content:center;margin-left:8px;flex:none}
.pfn .pb i,.pfn .pb i::before,.pfn .pb i::after{display:block;width:18px;height:2px;border-radius:2px;background:currentColor;position:relative}
.pfn .pb i::before,.pfn .pb i::after{content:"";position:absolute;left:0}.pfn .pb i::before{top:-6px}.pfn .pb i::after{top:6px}
@media(max-width:900px){.pfn{flex-wrap:wrap}.pfn .pb{display:inline-flex}.pfn .pm{margin-left:auto}.pfn .pm span{display:none}.pfn .pt{display:none;order:3;width:100%;flex-direction:column;margin:8px 0 2px;padding-top:8px;border-top:1px solid #262c38}
  .pfn.open .pt{display:flex}.pfn:not(.single){gap:8px;padding:10px 14px}.pfn .pt a{padding:13px 14px;font-size:16px;border-radius:12px}
  .pfn.single{flex-wrap:nowrap}.pfn.single .pt{display:flex;order:0;width:auto;flex-direction:row;margin:0 0 0 auto;padding:0;border:0}
  .pfn.single .pt a{padding:8px 14px;font-size:14px;border-radius:999px}}
</style>"""


def nav(c: dict, active: str) -> str:
    links = "".join(
        f'<a href="{i["href"]}"{' target="_blank" rel="noopener"' if i.get("ext") else ""} class="{"on " if i["k"] == active else ""}{"lk " if i["locked"] else ""}{"st" if i["k"] == "start" else ""}">'
        f'{html.escape(i["label"])}{LOCK if i["locked"] else ""}{f'<span class="bd">{i["badge"]}</span>' if i.get("badge") else ""}</a>'
        for i in items(c))
    logo = ""
    try:
        import base64
        logo = "data:image/png;base64," + base64.b64encode(portal.LOGO.read_bytes()).decode()
    except Exception:  # noqa: BLE001
        pass
    single = len(items(c)) == 1                              # 16.11 Rene: during onboarding there is one item, no burger menu
    burger = "" if single else ('<button class="pb" type="button" aria-label="Menü" onclick="document.getElementById(\'pfn\').classList.toggle(\'open\')"><i></i></button>')
    if single:                                               # Rene 16.11: always "Northfeed Portal"; onboarding is a quiet status on the right, not a button
        d, t = c.get("videos", (0, 0))
        st = (f"Onboarding · {d} von {t} Videos" if c["lang"] == "de" else f"Onboarding · {d} of {t} videos") if t else "Onboarding"
        short = f"{d}/{t} Videos" if t else ""
        return (f'<nav class="pfn single" id="pfn"><a class="pl" href="/"><img src="{logo}" alt="Northfeed"><span>Portal</span></a>'
                f'<span class="obs"><span class="lg">{html.escape(st)}</span><span class="sh">{html.escape(short)}</span></span></nav>')
    first = (c.get("name") or "").strip()                    # 16.11 Rene: same bar as the home page, name + avatar on the right
    me = f'<a class="pm" href="/konto" title="{"Dein Konto" if c["lang"] == "de" else "Your account"}"><span>{html.escape(first)}</span><b>{html.escape(first[:1].upper() or "N")}</b></a>'
    return (f'<nav class="pfn" id="pfn"><a class="pl" href="/"><img src="{logo}" alt="Northfeed"><span>Portal</span></a>'
            f'<div class="pt">{links}</div>{me}{burger}</nav>')


FAST = '<style>@view-transition{navigation:auto}.pfn,body>nav.nav{view-transition-name:nfbar}</style><script type="speculationrules">{"prerender":[{"source":"list","urls":["/","/course","/skripte","/zugaenge","/abrechnung","/onboarding"],"eagerness":"conservative"}]}</script>'


def _hearts(page: bytes) -> bytes:
    """16.11 Rene: the floating hearts of the home page on every model page, not only on some."""
    from pathlib import Path
    js = Path(__file__).with_name("portal_hearts.js").read_text(encoding="utf-8")
    css = ("<style>body>main,body>.box{position:relative;z-index:1}body>.toast{z-index:60}"
           # 16.11 Rene: one type scale on every model page (the home page's), and no pink glow in the top right corner
           "body{background-image:none!important}"
           # 16.11 Rene: every model page as wide as "Heute"
           "body>main,body>.box{max-width:1240px!important;margin-left:auto!important;margin-right:auto!important;padding-left:clamp(16px,4vw,48px)!important;padding-right:clamp(16px,4vw,48px)!important}"
           "main h1,.box h1{font-family:Fraunces,Georgia,serif!important;font-weight:500!important;font-size:clamp(34px,5vw,56px)!important;line-height:1.02!important;letter-spacing:-.01em}"
           "main h1 em,.box h1 em{font-style:italic;color:#ff9ec4}"
           "main .lead,.box .lead{font-size:16px!important}main .eyebrow,.box .eyebrow{font-size:12px!important;letter-spacing:.14em}</style>")
    # 16.11 Rene "lädt komplett neu": the other portal pages are prerendered in the background and swapped with a
    # cross-document view transition, the bar stays put; feels like switching between Heute and Plan
    # 16.11 launch check: "immediate" froze each page at load time, so after the last welcome video "Weiter" opened the
    # prerendered "still locked" copy; "conservative" prerenders on touch/press, always with the current state
    css += FAST
    return page.replace(b"</head>", css.encode() + b"</head>", 1).replace(b"</body>", f"<script>{js}</script></body>".encode(), 1)


def wrap_shell(page: bytes, c: dict, active: str) -> bytes:
    """contract_data._shell pages (centred card) -> full-width app page with the shared bar."""
    css = CSS + ("<style>body{display:block!important;place-items:initial!important;padding:0!important}"
                 ".box{margin:0 auto!important;background:none!important;border:0!important;border-radius:0!important;"
                 "padding-top:34px!important;padding-bottom:80px!important}.box>.logo,.box>a.back,.box>a[href='/']:first-of-type{display:none!important}"
                 "</style>")
    page = page.replace("Northfeed · Vertragsdaten</title>".encode(), "Northfeed · Portal</title>".encode(), 1)   # _shell's default title
    return _hearts(page.replace(b"</head>", css.encode() + b"</head>", 1).replace(b"<body>", b"<body>" + nav(c, active).encode(), 1))


def wrap_app(page: bytes, c: dict, active: str) -> bytes:
    """Pages with their own top bar (onboarding.html, course.html): hide theirs, show the shared one."""
    css = CSS + "<style>body>nav.nav,body>div.nav{display:none!important}</style>"
    return _hearts(page.replace(b"</head>", css.encode() + b"</head>", 1).replace(b"<body>", b"<body>" + nav(c, active).encode(), 1))


def home_script(c: dict) -> bytes:
    """For portal.html: tabs + lock state, read by its route()."""
    return f'<script>window.NFNAV={json.dumps(items(c), ensure_ascii=False)};window.NFOPEN={int(c["open"])};</script>'.encode()


def locked_page(c: dict, active: str) -> bytes:
    from .contract_data import _shell
    de = c["lang"] == "de"
    inner = (f'<h1>{"Gleich" if de else "Almost"} <em>{"freigeschaltet" if de else "unlocked"}</em></h1>'
             f'<p class="lead">{"Das wird freigeschaltet, sobald dein Steckbrief und deine Referenzbilder da sind. Das dauert nur einmal kurz." if de else "This unlocks as soon as your profile and reference images are in. You only do it once."}</p>'
             f'<a class="btn" style="display:inline-block;width:auto;text-decoration:none;padding:14px 26px" href="/onboarding">{"Jetzt erledigen" if de else "Do it now"}</a>')
    return wrap_shell(_shell(inner), c, active)
