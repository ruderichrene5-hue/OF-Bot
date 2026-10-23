"""Team platform (first page: Accounts dashboard). Stdlib only.

Binds 127.0.0.1 by default: it is meant to sit behind a Cloudflare Tunnel +
Cloudflare Access (e-mail login), never directly on the internet. A shared
password (PLATFORM_PASSWORD, HTTP basic auth) is an extra lock for local use.
The page is rebuilt at most every REBUILD_SECONDS and served from memory.
"""

from __future__ import annotations

import base64
import json
import re
import hashlib
import hmac
import os
import select
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PHONE_SSH_SETUP_CMD = (
    'sudo mkdir -p /var/root/.ssh && echo "ssh-ed25519 '
    'AAAAC3NzaC1lZDI1NTE5AAAAIML7/IeVcx6DN9/lBBu+XtQ6NXhiujaQTm8szGQWGB1I '
    'veralta-mng-iphone-farm" | sudo tee -a /var/root/.ssh/authorized_keys '
    "&& sudo chmod 700 /var/root/.ssh && sudo chmod 600 /var/root/.ssh/authorized_keys\n"
)

REBUILD_SECONDS = 300
_cache = {"at": 0.0, "html": b""}
_lock = threading.Lock()
_phones_cache = {"at": 0.0, "html": b""}
_phones_lock = threading.Lock()
_live_cache = {"at": 0.0, "html": b""}
# The two iPhone-farm pages mirror Airtable, so an edit there should show up
# within a minute, not after the views dashboard's 5-minute rebuild.
FARM_REBUILD_SECONDS = 60
_live_lock = threading.Lock()

# One tab bar over every page, so Accounts / Proxys / Live read as one platform.
NAV = [("/live", "Phones"), ("/", "CRM")]   # Proxies & Containers page removed (Rene 2026-10-08)
_NAV_CSS = (
    "<style>:root{--nav-h:58px}html{padding-top:58px}"
    ".pf-nav{position:fixed;top:0;left:0;right:0;z-index:1000;box-sizing:border-box;"
    "display:flex;gap:4px;align-items:center;height:58px;padding:0 20px;"
    "background:#07090d;border-bottom:1px solid #262c38;"
    "font:600 14.5px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
    ".pf-nav b{color:#e6e8ee;margin-right:14px}.pf-nav .sp{flex:1}"
    ".pf-nav a.pf-logo{padding:0 22px 0 0;display:flex;align-items:center}.pf-nav a.pf-logo:hover{background:none}"
    ".pf-nav a{color:#9aa3b2;text-decoration:none;padding:9px 14px;border-radius:9px}"
    ".pf-nav a:hover{color:#e6e8ee}.pf-nav a.on{color:#e6e8ee;background:#161a22}</style>"
)


# RNX Viewer logo (Rene 2026-10-08), inline so it needs no extra request
_LOGO = (Path(__file__).parent / "static" / "rnx_logo.svg").read_text(encoding="utf-8") if (Path(__file__).parent / "static" / "rnx_logo.svg").exists() else "<b>RNX Viewer</b>"


_ICON = (Path(__file__).parent / "static" / "rnx_icon.svg")
_FAVICON = ('<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,' +
            base64.b64encode(_ICON.read_bytes()).decode() + '">') if _ICON.exists() else ""
_FAVICON += ('<link rel="icon" type="image/x-icon" sizes="any" href="/favicon.ico">'
             '<link rel="apple-touch-icon" href="/apple-touch-icon.png">')


def _with_nav(body: bytes, active: str) -> bytes:
    """Insert the tab bar right after the page's <body ...> tag."""
    links = "".join(f'<a href="{href}"{" class=on" if href == active else ""}>{label}</a>'
                    for href, label in NAV)
    logo = _LOGO.replace('width="300" height="72"', 'height="38" width="158"')
    bar = f'{_NAV_CSS}<nav class="pf-nav"><a class="pf-logo" href="/live" title="RNX Viewer">{logo}</a>{links}</nav>'.encode("utf-8")
    h = body.find(b"</head>")                      # tab icon belongs in <head>: Chrome showed the globe when it sat in <body>
    if h >= 0:
        body = body[:h] + _FAVICON.encode() + body[h:]
    else:
        bar = _FAVICON.encode() + bar
    i = body.find(b"<body")
    j = body.find(b">", i) if i >= 0 else -1
    return body[:j + 1] + bar + body[j + 1:] if j >= 0 else bar + body


def _build_into(cache: dict, lock, module: str) -> None:
    """Build a page and swap it into its cache; the slow part (Airtable) runs outside the lock."""
    try:
        from importlib import import_module
        html = import_module(f"adb_bot.automation.{module}").build().encode("utf-8")
    except Exception as exc:                               # show the error, keep serving
        html = f"<pre>Dashboard error: {exc}</pre>".encode() if not cache["html"] else cache["html"]
    with lock:
        cache["html"], cache["at"] = html, time.time()


def _cached(cache: dict, lock, module: str) -> bytes:
    """Serve the last build at once; only the very first request waits for a build. Freshness comes
    from _warm() in the background -- before, a stale page was rebuilt while the visitor waited
    (CRM 3-6 s), which is what made switching Phones <-> CRM slow (Rene 2026-10-08)."""
    if not cache["html"]:
        _build_into(cache, lock, module)
    return cache["html"]


def _warm() -> None:
    while True:
        for cache, lock, module, every in ((_live_cache, _live_lock, "phone_live", FARM_REBUILD_SECONDS),
                                           (_cache, _lock, "crm", REBUILD_SECONDS)):
            if time.time() - cache["at"] > every:
                _build_into(cache, lock, module)
        time.sleep(10)


def _page() -> bytes:
    return _cached(_cache, _lock, "crm")


def _phones_page() -> bytes:
    with _phones_lock:
        if time.time() - _phones_cache["at"] > FARM_REBUILD_SECONDS or not _phones_cache["html"]:
            from adb_bot.automation import phone_dashboard
            try:
                _phones_cache["html"] = phone_dashboard.build().encode("utf-8")
            except Exception as exc:
                _phones_cache["html"] = f"<pre>Dashboard error: {exc}</pre>".encode()
            _phones_cache["at"] = time.time()
        return _phones_cache["html"]


def _live_page() -> bytes:
    return _cached(_live_cache, _live_lock, "phone_live")


# Live phone screens: the browser opens a WebSocket to /vnc, which is spliced to
# adbbot-vnc-bridge (websockify, loopback only) -> the phone's CatVNC. Browsers
# don't reliably send basic auth on WebSockets, so the (authenticated) Phones
# page embeds a ticket derived from PLATFORM_PASSWORD; it is valid for the
# current and previous UTC day.
VNC_BRIDGE = ("127.0.0.1", 6080)


def vnc_ticket(day: int | None = None) -> str:
    day = int(time.time() // 86400) if day is None else day
    key = os.environ.get("PLATFORM_PASSWORD", "").encode()
    return hmac.new(key, f"vnc:{day}".encode(), hashlib.sha256).hexdigest()[:32]


def _ticket_ok(ticket: str) -> bool:
    today = int(time.time() // 86400)
    return any(hmac.compare_digest(ticket, vnc_ticket(d)) for d in (today, today - 1))


_status_cache = {"at": 0.0, "data": {}}
_status_lock = threading.Lock()


def _farm_status() -> dict:
    """{Device record id: {vpn, container, reachable}} for the Phones page badges; cached 10 s."""
    with _status_lock:
        if time.time() - _status_cache["at"] > 10:
            from concurrent.futures import ThreadPoolExecutor
            from adb_bot.automation import iphone_farm_config
            ids = list(iphone_farm_config.load())
            with ThreadPoolExecutor(8) as pool:
                _status_cache["data"] = dict(zip(ids, pool.map(iphone_farm_config.vpn_state, ids)))
            _status_cache["at"] = time.time()
        return _status_cache["data"]


# Cloudflare Access in front (app.rnx-viewer.com, tunnel "reynex", 2026-10-08): whoever passed its
# e-mail login arrives through cloudflared on localhost with a signed JWT -- that IS the login, so no
# second password prompt (Rene: "will mich nicht bei jedem Ding anmelden"). The token is verified
# against the team's public keys; anything else (tailnet direct, forged header) still needs the password.
CF_TEAM = os.environ.get("CF_ACCESS_TEAM", "billowing-night-fe80")
_jwks = None


def _cf_access_ok(token: str) -> str | None:
    """E-mail of a valid Cloudflare Access JWT for our team, else None."""
    global _jwks
    if not token:
        return None
    try:
        import jwt
        if _jwks is None:
            _jwks = jwt.PyJWKClient(f"https://{CF_TEAM}.cloudflareaccess.com/cdn-cgi/access/certs", cache_keys=True)
        key = _jwks.get_signing_key_from_jwt(token).key
        aud = os.environ.get("CF_ACCESS_AUD")
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=f"https://{CF_TEAM}.cloudflareaccess.com",
                            audience=aud, options={"verify_aud": bool(aud)})
        return claims.get("email") or claims.get("sub") or "cloudflare"
    except Exception:
        return None


# Model logins (Rene 14.10): a model signs in through the same Cloudflare Access e-mail login and
# sees only her own accounts. /etc/adbbot/creators.json = {"emails": {"lea@example.com": "Lea"}} --
# the value is the model name exactly as in Airtable "Model (auto)". Anyone not listed is staff.
CREATORS_PATH = os.environ.get("CREATORS_PATH", "/etc/adbbot/creators.json")
_crm_data = {"at": 0.0, "data": None}
_crm_data_lock = threading.Lock()


def _creators() -> dict:
    try:
        return {k.strip().lower(): v for k, v in (json.loads(Path(CREATORS_PATH).read_text()).get("emails") or {}).items()}
    except Exception:
        return {}


def _crm_dataset() -> dict:
    """crm.data() (one Airtable round trip), refreshed at most every REBUILD_SECONDS."""
    with _crm_data_lock:
        if _crm_data["data"] is None or time.time() - _crm_data["at"] > REBUILD_SECONDS:
            from adb_bot.automation import crm
            _crm_data["data"], _crm_data["at"] = crm.data(), time.time()
        return _crm_data["data"]


def _portal_page(model: str) -> bytes:
    """Northfeed model portal (16.10): today/week/reels/tasks/accounts in the website's look."""
    from adb_bot.automation import portal
    return portal.build(model, _crm_dataset()).encode("utf-8")


def _creator_page(model: str) -> bytes:
    from adb_bot.automation import crm
    html = crm.render(crm.creator_view(_crm_dataset(), model)).encode("utf-8")
    logo = _LOGO.replace('width="300" height="72"', 'height="38" width="158"')
    badge = (f'<span style="margin-left:auto;display:flex;align-items:center;gap:10px;color:#9aa3b2;font-weight:500">'
             f'<span style="font-size:11px;padding:4px 9px;border-radius:999px;background:#161a22;border:1px solid #262c38">Creator view · only your accounts</span>'
             f'<b style="color:#e6e8ee">{model}</b><span style="width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,#4f8cff,#9b5cff);'
             f'display:grid;place-items:center;color:#fff;font-weight:700;font-size:13px">{model[:1].upper()}</span></span>')
    bar = f'{_NAV_CSS}<nav class="pf-nav"><a class="pf-logo" href="/" title="RNX Viewer">{logo}</a>{badge}</nav>'.encode("utf-8")
    h = html.find(b"</head>")
    if h >= 0:
        html = html[:h] + _FAVICON.encode() + html[h:]
    i = html.find(b"<body"); j = html.find(b">", i) if i >= 0 else -1
    return html[:j + 1] + bar + html[j + 1:] if j >= 0 else bar + html


class Handler(BaseHTTPRequestHandler):
    identity: str | None = None                  # e-mail from Cloudflare Access, None for the password login

    def _portal_session(self) -> str | None:
        """portal.* only: e-mail from the nf_session cookie, or None."""
        from adb_bot.automation import portal_auth
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nf_session":
                return portal_auth.read(v)
        return None

    def _portal_site(self, route: str):
        from adb_bot.automation import portal_auth
        lang = (self.headers.get("Accept-Language") or "en")[:2].lower()
        lang = lang if lang in ("de", "en", "th") else "en"
        if route.startswith("/portal/ref/"):
            if self._portal_session() or self._basic_ok():
                return self._portal_ref(route)
            self.send_response(401); self.end_headers(); return
        if route == "/logout":
            self.send_response(302); self.send_header("Set-Cookie", "nf_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/"); self.end_headers(); return
        email = self._portal_session()
        if email:
            model = portal_auth.models_by_email().get(email, {}).get("name")
            if model:
                self.identity = email
                return self._send_html(_portal_page(model))
        q = parse_qs(urlsplit(self.path).query)
        if q.get("model") and self._basic_ok():            # staff preview: ?model=Lea with the platform password
            return self._send_html(_portal_page(q["model"][0].strip()))
        if route != "/":
            self.send_response(302); self.send_header("Location", "/"); self.end_headers(); return
        return self._send_html(portal_auth.login_page(lang).encode("utf-8"))

    def _send_html(self, body: bytes, extra: list | None = None):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _basic_ok(self) -> bool:
        pw = os.environ.get("PLATFORM_PASSWORD", "")
        hdr = self.headers.get("Authorization", "")
        if not pw or not hdr.startswith("Basic "):
            if not pw:
                return True
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Northfeed staff"'); self.end_headers()
            return False
        try:
            _, given = base64.b64decode(hdr[6:]).decode().split(":", 1)
        except Exception:
            return False
        return hmac.compare_digest(given, pw)

    def _authorized(self) -> bool:
        pw = os.environ.get("PLATFORM_PASSWORD", "")
        self.identity = None
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host.startswith("portal."):
            email = self._portal_session()
            if email:
                self.identity = email
                return True
            return self._basic_ok() if self.headers.get("Authorization", "").startswith("Basic ") else True
        if not pw:
            return True
        if self.client_address[0] in ("127.0.0.1", "::1"):
            tok = self.headers.get("Cf-Access-Jwt-Assertion", "")
            if not tok:
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "CF_Authorization":
                        tok = v
            who = _cf_access_ok(tok)
            if who:
                self.identity = who
                return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            _, given = base64.b64decode(hdr[6:]).decode().split(":", 1)
        except Exception:
            return False
        return hmac.compare_digest(given, pw)

    def _splice_vnc(self) -> None:
        """Hand the upgrade request to websockify unchanged and pipe both ways."""
        try:
            upstream = socket.create_connection(VNC_BRIDGE, timeout=5)
        except OSError:
            self.send_response(502)
            self.end_headers()
            return
        upstream.settimeout(None)
        head = f"{self.requestline}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()) + "\r\n"
        upstream.sendall(head.encode("latin-1"))
        client = self.connection
        self.close_connection = True
        socks = [client, upstream]
        try:
            while True:
                readable, _, _ = select.select(socks, [], [], 300)
                if not readable:
                    break
                for src in readable:
                    data = src.recv(65536)
                    if not data:
                        return
                    (upstream if src is client else client).sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()

    def _portal_ref(self, route: str):
        from adb_bot.automation import portal
        f = portal.ref_file(route.rsplit("/", 1)[-1])
        if not f:
            self.send_response(404); self.end_headers(); return
        data = f.read_bytes()
        self.send_response(200); self.send_header("Content-Type", "video/mp4"); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400"); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        route = self.path.split("?")[0]
        # 20.10 (Rene): forward tap/swipe/type from the /mjpeg page to WDA so the dashboard is
        # interactive. Simple session-caching proxy -- one WDA session per phone, reused across taps.
        # POST /phones/wda/<letter>/<action> with JSON body {x,y} or {x1,y1,x2,y2} or {text}.
        if route.startswith("/phones/wda/"):
            if not self._authorized():
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Platform"'); self.end_headers(); return
            parts = route[len("/phones/wda/"):].split("/")
            if len(parts) != 2:
                self.send_response(404); self.end_headers(); return
            letter, action = parts[0].upper(), parts[1]
            port_map = {"A": 8101, "B": 8102, "C": 8103, "D": 8104, "E": 8105,
                        "I": 8106, "G": 8107, "H": 8108, "F": 8109, "J": 8111, "K": 8112, "L": 8113}
            port = port_map.get(letter)
            if not port:
                self.send_response(404); self.end_headers(); return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 4096))
            import urllib.request, urllib.error
            # cache WDA sessions in a module global
            global _wda_sessions
            if "_wda_sessions" not in globals():
                _wda_sessions = {}
            def wda_post(path, data):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                    data=data if isinstance(data, bytes) else json.dumps(data).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                return urllib.request.urlopen(req, timeout=5).read()
            def ensure_sid():
                sid = _wda_sessions.get(letter)
                if sid:
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/session/{sid}", timeout=3).read()
                        return sid
                    except Exception:
                        pass
                r = wda_post("/session", {"capabilities": {"alwaysMatch": {"platformName": "iOS"}}})
                sid = json.loads(r)["value"]["sessionId"]
                _wda_sessions[letter] = sid
                return sid
            try:
                sid = ensure_sid()
                if action == "tap":
                    wda_post(f"/session/{sid}/wda/tap", body)
                elif action == "swipe":
                    wda_post(f"/session/{sid}/wda/dragfromtoforduration", body)
                elif action == "type":
                    wda_post(f"/session/{sid}/wda/keys", body)
                elif action == "home":
                    wda_post(f"/session/{sid}/wda/pressButton", {"name": "home"})
                else:
                    self.send_response(400); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as exc:
                import logging
                logging.getLogger("wda-proxy").warning("wda %s %s: %s", letter, action, exc)
                self.send_response(502); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())
            return
        if route in ("/portal/login", "/portal/verify"):
            from adb_bot.automation import portal_auth
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(length, 4096)).decode("utf-8", "replace")).items()}
            lang = (self.headers.get("Accept-Language") or "en")[:2].lower(); lang = lang if lang in ("de", "en", "th") else "en"
            email = form.get("email", "").strip().lower()
            if route == "/portal/login":
                m = portal_auth.models_by_email().get(email)
                if not m:
                    return self._send_html(portal_auth.login_page(lang, "email", email, "unknown").encode("utf-8"))
                mode = "code" if portal_auth.smtp_ready() else "password"
                if mode == "code":
                    try:
                        portal_auth.send_code(email)
                    except Exception as e:  # noqa: BLE001
                        print("portal send_code:", e); mode = "password"
                return self._send_html(portal_auth.login_page(lang, "secret", email, "", mode).encode("utf-8"))
            m = portal_auth.check(email, form.get("secret", ""))
            if not m:
                mode = "code" if portal_auth.smtp_ready() else "password"
                return self._send_html(portal_auth.login_page(lang, "secret", email, "wrong", mode).encode("utf-8"))
            tok = portal_auth.issue(email)
            self.send_response(302)
            self.send_header("Set-Cookie", f"nf_session={tok}; Path=/; Max-Age={30*86400}; Secure; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/"); self.end_headers(); return
        if route == "/portal/done":
            # model ticks an item in the portal -> status written to the Northfeed Portal base
            if not self._authorized():
                self.send_response(401); self.end_headers(); return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(min(length, 4096)).decode("utf-8"))
                from adb_bot.automation import portal, portal_auth
                creator = _creators().get((self.identity or "").lower()) or portal_auth.models_by_email().get((self.identity or "").lower(), {}).get("name")
                ok = portal.mark_done(str(data.get("id", "")), bool(data.get("done")), creator)
            except Exception as e:  # noqa: BLE001
                ok = False
                print("portal/done:", e)
            body = json.dumps({"ok": ok}).encode()
            self.send_response(200 if ok else 400); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path.split("?")[0] == "/northfeed/apply":
            # 2026-10-12: application funnel of the new agency site (public, no auth). Stores one JSON per
            # application under /opt/northfeed-applications; photos arrive base64 inside the JSON.
            import datetime as _dt, re as _re, uuid as _uuid
            length = int(self.headers.get("Content-Length") or 0)
            if length > 25 * 1024 * 1024:
                self.send_response(413); self.end_headers(); return
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                assert isinstance(data, dict)
            except Exception:
                self.send_response(400); self.end_headers(); return
            out = Path("/opt/northfeed-applications"); out.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            name = _re.sub(r"[^a-z0-9]+", "-", str(data.get("first_name", "")).lower())[:30] or "anon"
            fid = f"{ts}-{name}-{_uuid.uuid4().hex[:6]}"
            photos = data.pop("photos", None) or {}
            pdir = out / fid
            saved = []
            for key, b64 in photos.items():
                if not isinstance(b64, str) or not b64.startswith("data:image/"):
                    continue
                try:
                    mime, payload = b64.split(";base64,", 1)
                    ext = {"data:image/jpeg": "jpg", "data:image/png": "png", "data:image/webp": "webp", "data:image/heic": "heic"}.get(mime, "jpg")
                    pdir.mkdir(exist_ok=True)
                    (pdir / f"{key}.{ext}").write_bytes(__import__("base64").b64decode(payload))
                    saved.append(f"{key}.{ext}")
                except Exception:  # noqa: BLE001
                    pass
            data["_received"] = _dt.datetime.now().isoformat(timespec="seconds")
            data["_ip"] = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or self.client_address[0]
            data["_photos"] = saved
            (out / f"{fid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            try:
                import threading
                threading.Thread(target=_northfeed_notify, args=(data, pdir, saved), daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
            body = json.dumps({"ok": True, "id": fid}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Platform"')
            self.end_headers()
            return
        action = self.path.split("?")[0].removeprefix("/phones/")
        from adb_bot.automation import iphone_farm_config
        if self.path.split("?")[0] == "/phones/whep":
            q = parse_qs(urlsplit(self.path).query)
            if not _ticket_ok((q.get("t") or [""])[0]):
                self.send_response(403)
                self.end_headers()
                return
            length = min(int(self.headers.get("Content-Length") or 0), 200_000)
            status, text = iphone_farm_config.whep((q.get("token") or [""])[0],
                                                   self.rfile.read(length).decode("utf-8", "replace"))
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/sdp" if status == 201 else "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if action == "clipboard":
            # Rene 09.10: Cmd+V in a phone window pasted the PHONE's old clipboard. The page posts the Mac's
            # clipboard text here, it goes onto the phone's pasteboard (XXTouch), then the page sends Cmd+V.
            q = parse_qs(urlsplit(self.path).query)
            if not _ticket_ok((q.get("t") or [""])[0]):
                self.send_response(403)
                self.end_headers()
                return
            length = min(int(self.headers.get("Content-Length") or 0), 100_000)
            text = self.rfile.read(length).decode("utf-8", "replace")
            ok, msg = iphone_farm_config.set_clipboard((q.get("token") or [""])[0], text,
                                                       type_it=(q.get("mode") or [""])[0] == "type")
            body = json.dumps({"ok": ok, "message": msg}).encode()
            self.send_response(200 if ok else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if action == "clipboard_read":
            # Rene 10.10: copy on the phone, Cmd+C in its window -> the phone's pasteboard onto the Mac.
            q = parse_qs(urlsplit(self.path).query)
            if not _ticket_ok((q.get("t") or [""])[0]):
                self.send_response(403)
                self.end_headers()
                return
            ok, text = iphone_farm_config.get_clipboard((q.get("token") or [""])[0])
            body = json.dumps({"ok": ok, "text": text if ok else "", "message": "" if ok else text}).encode()
            self.send_response(200 if ok else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if action == "shortcut":
            q = parse_qs(urlsplit(self.path).query)
            if not _ticket_ok((q.get("t") or [""])[0]):
                self.send_response(403)
                self.end_headers()
                return
            ok, msg = iphone_farm_config.run_shortcut((q.get("token") or [""])[0], (q.get("name") or [""])[0])
            body = json.dumps({"ok": ok, "message": msg}).encode()
            self.send_response(200 if ok else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/phones/") and action in iphone_farm_config.ACTIONS:
            q = parse_qs(urlsplit(self.path).query)
            if not _ticket_ok((q.get("t") or [""])[0]):      # page-issued ticket, blocks cross-site POSTs
                self.send_response(403)
                self.end_headers()
                return
            ok, msg = iphone_farm_config.ACTIONS[action]((q.get("token") or [""])[0])
            body = json.dumps({"ok": ok, "message": msg}).encode()
            self.send_response(200 if ok else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if (self.path.split("?")[0] == "/vnc"
                and self.headers.get("Upgrade", "").lower() == "websocket"):
            ticket = (parse_qs(urlsplit(self.path).query).get("t") or [""])[0]
            if not _ticket_ok(ticket):
                self.send_response(403)
                self.end_headers()
                return
            self._splice_vnc()
            return
        # 20.10 (Rene): MJPEG stream per phone from WDA's own :9100 (SSH-forwarded 127.0.0.1:910N).
        # /phones/mjpeg/<letter> opens a persistent multipart/x-mixed-replace response that the
        # browser renders in a plain <img src>. 15-25 fps, 720p, no decoding on the server side.
        # 21.10 (Rene: "fullscreen zum richtigen Steuern mit flüssigen FPS"): one-phone noVNC page.
        # /phones/<letter>/screen -> HTML with embedded noVNC client that opens a WebSocket back to
        # /vnc?token=<rec_id>&t=<ticket>, so this page uses the same websockify bridge the /live grid
        # already uses, just targeted at the picked phone. 25-30 fps real-time via CatVNC + input.
        if self.path.startswith("/phones/") and self.path.split("?")[0].endswith("/screen"):
            if not self._authorized():
                self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Platform"'); self.end_headers(); return
            letter = self.path.split("?")[0].removeprefix("/phones/").removesuffix("/screen").upper()
            # letter -> record id. Most phones don't carry "name" or "ssh_usb_backup" -- but every phone
            # has stream=iphoneN. The N<->letter mapping on this farm (not sequential, grew over time):
            LETTER_TO_STREAM = {"A":"iphone1","B":"iphone2","C":"iphone3","D":"iphone4","E":"iphone5",
                                "I":"iphone6","G":"iphone7","H":"iphone8",
                                "F":"iphone10","J":"iphone11","K":"iphone12","L":"iphone13"}
            from adb_bot.automation import iphone_farm_config
            rec_id = None; vnc_pw = "123"
            want_stream = LETTER_TO_STREAM.get(letter)
            for rid, ph in iphone_farm_config.load().items():
                if (ph.get("name", "") or "").endswith(f" {letter}") \
                   or (ph.get("ssh_usb_backup", "") or "").endswith(f"-{letter.lower()}") \
                   or (want_stream and ph.get("stream") == want_stream):
                    rec_id = rid
                    vnc_pw = ph.get("vnc_password") or "123"
                    break
            if not rec_id:
                self.send_response(404); self.end_headers(); self.wfile.write(f"no iPhone {letter}".encode()); return
            # 21.10 Rene: CatVNC on the phones is too fragile (daemon dies, kickstart doesn't help)
            # -> fullscreen uses the already-running MJPEG+WDA stack instead: ~15-20 fps, click/swipe/
            # type via /phones/wda/<L>/... (same handlers the /mjpeg grid uses). Works on every phone
            # that has WDA alive. No CatVNC dependency.
            page = (
                "<!doctype html><html><head><meta charset=utf-8>"
                f"<title>iPhone {letter} — Live</title>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<style>html,body{margin:0;padding:0;background:#000;color:#eee;font-family:system-ui;height:100%;overflow:hidden}"
                "#bar{position:fixed;top:0;left:0;right:0;background:rgba(0,0,0,.75);padding:6px 12px;z-index:10;display:flex;gap:10px;align-items:center}"
                "#bar a,#bar button{color:#4af;background:transparent;border:1px solid #4af;padding:3px 10px;border-radius:4px;text-decoration:none;cursor:pointer;font:inherit}"
                "#bar button:hover,#bar a:hover{background:#4af;color:#000}"
                "#scr{position:fixed;top:36px;left:0;right:0;bottom:0;background:#111;display:flex;align-items:center;justify-content:center;cursor:crosshair}"
                "#scr img{max-height:100%;max-width:100%;height:auto;width:auto;aspect-ratio:9/16;background:#222;pointer-events:none;display:block;object-fit:contain}"
                "</style></head><body>"
                f"<div id=bar><strong>iPhone {letter}</strong>"
                "<a href=/mjpeg>⟵ Übersicht</a>"
                f"<button onclick=\"fetch('/phones/wda/{letter.lower()}/home',{{method:'POST'}})\">Home</button>"
                f"<button onclick=\"document.querySelector('#scr img').src='/phones/mjpeg/{letter.lower()}?r='+Date.now()\">Reload</button>"
                "<span style='margin-left:auto;font-size:12px;color:#888'>Klick = Tap · Drag = Swipe · Keyboard tippt</span></div>"
                f'<div id=scr data-p="{letter.lower()}"><img src="/phones/mjpeg/{letter.lower()}" alt="iPhone {letter} offline"></div>'
                "<script>"
                "const scr=document.getElementById('scr'), p=scr.dataset.p;"
                "let start=null;"
                "scr.addEventListener('mousedown',e=>{if(e.target.tagName==='BUTTON'||e.target.tagName==='A')return;start={x:e.offsetX,y:e.offsetY,t:Date.now()};});"
                "scr.addEventListener('mouseup',e=>{"
                "  if(!start)return;"
                "  const img=scr.querySelector('img');const r=img.getBoundingClientRect();"
                "  const sx=375/r.width, sy=812/r.height;"
                "  const dur=Date.now()-start.t;"
                "  const dx=Math.abs(e.offsetX-start.x), dy=Math.abs(e.offsetY-start.y);"
                "  if(dx>12||dy>12){"
                "    fetch('/phones/wda/'+p+'/swipe',{method:'POST',headers:{'Content-Type':'application/json'},"
                "      body:JSON.stringify({fromX:start.x*sx,fromY:start.y*sy,toX:e.offsetX*sx,toY:e.offsetY*sy,duration:dur/1000})});"
                "  } else {"
                "    fetch('/phones/wda/'+p+'/tap',{method:'POST',headers:{'Content-Type':'application/json'},"
                "      body:JSON.stringify({x:e.offsetX*sx,y:e.offsetY*sy})});"
                "  }"
                "  start=null;"
                "});"
                "document.addEventListener('keydown',e=>{"
                "  if(e.target.tagName==='INPUT'||e.target.tagName==='BUTTON')return;"
                "  if(e.key.length==1||['Backspace','Enter'].includes(e.key)){"
                "    e.preventDefault();"
                "    const k=e.key==='Backspace'?'\\uE003':(e.key==='Enter'?'\\n':e.key);"
                "    fetch('/phones/wda/'+p+'/type',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:[k]})});"
                "  }"
                "});"
                "</script></body></html>"
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/phones/mjpeg/"):
            letter = self.path.split("?")[0].removeprefix("/phones/mjpeg/").upper()
            port_map = {"A": 9101, "B": 9102, "C": 9103, "D": 9104, "E": 9105,
                        "I": 9106, "G": 9107, "H": 9108,
                        "F": 9109, "J": 9111, "K": 9112, "L": 9113}     # 22.10: F = stream 9 (Renes-iPhone.local)
            port = port_map.get(letter)
            if not port:
                self.send_response(404); self.end_headers(); return
            try:
                upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
            except OSError:
                self.send_response(502); self.end_headers(); return
            upstream.settimeout(None)
            try:
                upstream.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        self.send_response(502); self.end_headers(); return
                    buf += chunk
                head_bytes, _, rest = buf.partition(b"\r\n\r\n")
                # extract Content-Type from WDA's response headers so we can set the right one
                # on our own response -- the browser needs "multipart/x-mixed-replace; boundary=..."
                ctype = "multipart/x-mixed-replace; boundary=--BoundaryString"
                for line in head_bytes.decode("latin-1", "ignore").splitlines()[1:]:
                    if line.lower().startswith("content-type:"):
                        ctype = line.split(":", 1)[1].strip()
                        break
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                if rest:
                    self.wfile.write(rest)
                    self.wfile.flush()
                self.close_connection = True
                while True:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
            except Exception as exc:
                import logging, traceback
                logging.getLogger("mjpeg").warning("mjpeg %s: %s\n%s", letter, exc, traceback.format_exc())
            finally:
                try: upstream.close()
                except Exception: pass
            return
        _public = {"/favicon.ico": "favicon.ico", "/apple-touch-icon.png": "rnx-viewer-icon-180.png",
                   "/brand/icon.png": "rnx-viewer-icon-512.png", "/brand/logo.png": "rnx-viewer-logo.png"}  # also for the Cloudflare login page
        if self.path.split("?")[0].startswith("/northfeed"):
            # 2026-10-11: public design preview of the new agency site (Rene), static files from
            # /opt/northfeed-site; no basic-auth, the hostname itself sits behind Cloudflare Access.
            rel = self.path.split("?")[0].removeprefix("/northfeed").lstrip("/") or "index.html"
            root = Path("/opt/northfeed-site").resolve()
            f = (root / rel).resolve()
            if f.is_dir():
                f = f / "index.html"
            if not str(f).startswith(str(root)) or not f.is_file():
                self.send_response(404); self.end_headers(); return
            body = f.read_bytes()
            ctype = {".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".png": "image/png", ".css": "text/css", ".js": "text/javascript", ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".xml": "application/xml", ".txt": "text/plain; charset=utf-8"}.get(f.suffix, "application/octet-stream")
            if f.suffix == ".html" and not body.lstrip().lower().startswith(b"<!doctype"):
                body = (b'<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                        b'</head><body style="margin:0">' + body + b'</body></html>')
            # iOS Safari refuses to play audio/video unless the server honours Range requests
            rng = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d*)-(\d*)", rng) if rng else None
            if m and f.suffix in (".mp4", ".mp3", ".webm"):
                start = int(m.group(1) or 0)
                end = int(m.group(2)) if m.group(2) else len(body) - 1
                end = min(end, len(body) - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.end_headers()
                    return
                chunk = body[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache" if f.suffix == ".html" else "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in _public:     # public: browsers fetch these without auth
            f = Path(__file__).parent / "static" / _public[self.path]
            body = f.read_bytes() if f.exists() else b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "image/x-icon" if f.suffix == ".ico" else "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Platform"')
            self.end_headers()
            return
        route = self.path.split("?")[0]
        creator = _creators().get((self.identity or "").lower())
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host.startswith("portal."):                # portal.northfeed-management.com: own login, the model portal IS the site
            return self._portal_site(route)
        if creator:                                   # model login: her page and nothing else
            if route.startswith("/portal/ref/"):
                return self._portal_ref(route)
            if route not in ("/", "/accounts", "/health", "/portal"):
                self.send_response(302); self.send_header("Location", "/"); self.end_headers(); return
            body = _portal_page(creator) if route == "/portal" else _creator_page(creator)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/whoami":                        # debug: who does the server think you are?
            body = json.dumps({"identity": self.identity, "creator": creator, "creators_file": sorted(_creators().keys())}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if route.startswith("/portal/ref/"):          # reference video cached on this server (see portal.fetch_references)
            return self._portal_ref(route)
        if route == "/portal":                        # staff preview of the model portal: /portal?model=Lea
            model = (parse_qs(urlsplit(self.path).query).get("model") or [""])[0].strip()
            body = _portal_page(model) if model else b"<pre>/portal?model=&lt;Model name&gt;</pre>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/creator":                       # staff preview: /creator?model=Nikki
            model = (parse_qs(urlsplit(self.path).query).get("model") or [""])[0].strip()
            body = _creator_page(model) if model else b"<pre>/creator?model=&lt;Model name&gt;</pre>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/phones/lines":
            from adb_bot.automation import iphone_farm_config
            body = json.dumps(iphone_farm_config.lines_status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/phones/status":
            body = json.dumps(_farm_status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if route in ("/", "/accounts", "/phones", "/live", "/live-vnc", "/mjpeg"):
            if route == "/phones":                       # old Proxies & Containers page: gone, send people to Phones
                self.send_response(302)
                self.send_header("Location", "/live")
                self.end_headers()
                return
            elif route == "/live-vnc":
                # the old VNC/WebRTC grid (USB era); kept reachable for comparison, not linked in the nav
                body = _with_nav(_live_page().replace(b"__VNC_TICKET__", vnc_ticket().encode()), "/live")
            elif route in ("/live", "/mjpeg"):
                # 22.10 (Rene: alles ueber WLAN, auch das Video): /live IS the WDA grid now -- picture from WDA's
                # MJPEG (:9100 via tunnel 910N), taps/swipes/keys to WDA (:8100 via 810N); works through Cloudflare.
                # 20.10 (Rene): Live-view at 15-25 fps via WDA MJPEG. 7/8 iPhones (A/B/C/E/G/H/I),
                # D is out while XXTouch license is expired. <img> auto-reconnects on each navigation.
                body = _with_nav((
                    # 23.10 (Rene: "komische Ansicht, man kann nicht richtig scrollen"): reserve phones without a picture
                    # are hidden, the mouse wheel scrolls INSIDE the phone (swipe), a click on the name makes the tile big.
                    "<style>body{background:#000;color:#eee;font-family:system-ui;margin:0;padding:12px;overflow-y:auto}"
                    ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;align-items:start}"
                    ".t{position:relative;background:#111;border-radius:8px;overflow:hidden;cursor:crosshair;touch-action:none}"
                    ".t.big{grid-column:1/-1;max-width:460px;margin:0 auto}"
                    ".t.off{display:none}"
                    ".bar{display:flex;gap:8px;align-items:center;margin:0 0 10px 0;font-size:13px;color:#9aa3b2}"
                    ".bar button{background:#161a22;color:#e6e8ee;border:1px solid #262c38;border-radius:8px;padding:6px 10px;cursor:pointer}"
                    ".t img{width:100%;height:auto;display:block;aspect-ratio:9/16;background:#222;pointer-events:none}"
                    ".t canvas.hold{position:absolute;inset:0;width:100%;height:100%;display:none;pointer-events:none;z-index:1}"
                    ".t .rc{position:absolute;bottom:6px;right:8px;width:8px;height:8px;border-radius:50%;background:#fa0;display:none;z-index:2}"
                    ".l{position:absolute;top:6px;left:8px;background:rgba(0,0,0,.6);padding:2px 8px;border-radius:4px;font-size:13px;z-index:2}"
                    ".h{position:absolute;top:6px;right:8px;background:rgba(0,0,0,.6);padding:2px 8px;border-radius:4px;font-size:11px;z-index:2;cursor:pointer}"
                    "</style>"
                    "<div class=bar><span>Click a name = big view · mouse wheel = scroll in the phone · drag = swipe · click = tap · type after a click</span>"
                    "<span style='flex:1'></span><button onclick=\"document.querySelectorAll('.t.off').forEach(t=>t.classList.remove('off'));this.remove()\">show all phones</button></div>"
                    "<div class=g>"
                    + "".join(f'<div class="t{" off" if L in ("F","J","K","L") else ""}" data-p="{L.lower()}">'
                             f'<div class=l onclick="event.stopPropagation();this.parentNode.classList.toggle(\'big\');this.parentNode.scrollIntoView({{behavior:\'smooth\',block:\'start\'}})" title="big view on/off" style="cursor:pointer">iPhone {L}</div>'
                             f'<div class=h style="right:48px" onclick="event.stopPropagation();window.open(\'/phones/{L.lower()}/screen\',\'_blank\')" title="Fullscreen (noVNC)">⛶</div>'
                             f'<div class=h onclick="event.stopPropagation();fetch(\'/phones/wda/{L.lower()}/home\',{{method:\"POST\"}})" title="Home">⏺</div>'
                             f'<img src="/phones/mjpeg/{L.lower()}" alt="iPhone {L} offline">'
                             f'<div class=rc title="reconnecting"></div>'
                             f'</div>'
                             for L in ("A","B","C","D","E","F","G","H","I","J","K","L"))
                    + "</div>"
                    # click handler: translate browser click coords -> iPhone pt coords (375x812 for 7/8, 375x812 for X -- same pt)
                    "<script>"
                    "document.querySelectorAll('.t').forEach(t=>{"
                    "  let start=null;"
                    "  t.addEventListener('mousedown',e=>{start={x:e.offsetX,y:e.offsetY,t:Date.now()};});"
                    "  t.addEventListener('mouseup',e=>{"
                    "    if(!start)return;"
                    "    const r=t.querySelector('img').getBoundingClientRect();"
                    "    const sx=375/r.width, sy=812/r.height;"
                    "    const p=t.dataset.p;"
                    "    const dur=Date.now()-start.t;"
                    "    const dx=Math.abs(e.offsetX-start.x), dy=Math.abs(e.offsetY-start.y);"
                    "    if(dx>12||dy>12){"
                    "      fetch('/phones/wda/'+p+'/swipe',{method:'POST',headers:{'Content-Type':'application/json'},"
                    "        body:JSON.stringify({fromX:start.x*sx,fromY:start.y*sy,toX:e.offsetX*sx,toY:e.offsetY*sy,duration:Math.min(0.6,Math.max(0.08,dur/1000))})});"
                    "    } else {"
                    "      fetch('/phones/wda/'+p+'/tap',{method:'POST',headers:{'Content-Type':'application/json'},"
                    "        body:JSON.stringify({x:e.offsetX*sx,y:e.offsetY*sy})});"
                    "    }"
                    "    start=null;"
                    "  });"
                    "});"
                    "// 22.10 (Rene: 'fuer die VAs ist es nervig, wenn das Bild kurz weg ist'): WDA restarts every few"
                    "// minutes per phone. When its stream breaks, copy the last frame onto a canvas over the <img>, reconnect"
                    "// in the background and drop the canvas as soon as the new stream delivers a frame -> a short freeze"
                    "// instead of a blank 'offline' tile."
                    "function hold(t){const img=t.querySelector('img');let c=t.querySelector('canvas.hold');"
                    "  if(!c){c=document.createElement('canvas');c.className='hold';t.appendChild(c);}"
                    "  if(img.naturalWidth>0){c.width=img.naturalWidth;c.height=img.naturalHeight;try{c.getContext('2d').drawImage(img,0,0);c.style.display='block';}catch(e){}}"
                    "  t.querySelector('.rc').style.display='block';}"
                    "function release(t){const c=t.querySelector('canvas.hold');if(c)c.style.display='none';t.querySelector('.rc').style.display='none';}"
                    "function reconnect(t){hold(t);const img=t.querySelector('img');setTimeout(()=>{img.src='/phones/mjpeg/'+t.dataset.p+'?r='+Date.now();},1500);}"
                    "document.querySelectorAll('.t').forEach(t=>{const img=t.querySelector('img');let busy=false;"
                    "  img.addEventListener('error',()=>{if(!busy){busy=true;reconnect(t);setTimeout(()=>busy=false,3000);}});"
                    "  img.addEventListener('load',()=>{release(t);});"
                    "  setInterval(()=>{if(busy)return;if(!img.complete||img.naturalWidth==0){busy=true;reconnect(t);setTimeout(()=>busy=false,3000);}},3000);"
                    "});"
                    "// 23.10: mouse wheel over a tile scrolls the phone's content (one short swipe per wheel step, throttled)"
                    "document.querySelectorAll('.t').forEach(t=>{let last=0;t.addEventListener('wheel',e=>{"
                    "  e.preventDefault();const now=Date.now();if(now-last<350)return;last=now;"
                    "  const up=e.deltaY>0;const p=t.dataset.p;"
                    "  fetch('/phones/wda/'+p+'/swipe',{method:'POST',headers:{'Content-Type':'application/json'},"
                    "    body:JSON.stringify({fromX:187,fromY:up?560:300,toX:187,toY:up?300:560,duration:0.18})});"
                    "},{passive:false});});"
                    "// keyboard: focus a tile first, then type"
                    "let focused=null;"
                    "document.querySelectorAll('.t').forEach(t=>t.addEventListener('click',e=>{focused=t.dataset.p;t.style.outline='2px solid #4af';setTimeout(()=>t.style.outline='',2000);}));"
                    "document.addEventListener('keydown',e=>{"
                    "  if(!focused||e.target.tagName=='INPUT')return;"
                    "  if(e.key.length==1||['Backspace','Enter'].includes(e.key)){"
                    "    e.preventDefault();"
                    "    const k=e.key=='Backspace'?'\\uE003':(e.key=='Enter'?'\\n':e.key);"
                    "    fetch('/phones/wda/'+focused+'/type',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:[k]})});"
                    "  }"
                    "});"
                    "</script>"
                ).encode("utf-8"), "/live")
            else:
                body = _with_nav(_page(), "/")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/iphone-ssh-setup":
            # Unauthenticated on purpose: just our own SSH public key + the
            # one-time setup command, meant to be opened in Safari *on the
            # phone itself* (via VNC) to copy-paste into NewTerm, because
            # CatVNC doesn't sync the Mac's clipboard into the VNC session.
            # Not sensitive -- a public key is, by design, fine to publish.
            body = PHONE_SSH_SETUP_CMD.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):             # keep the journal quiet
        pass


def main() -> None:
    host = os.environ.get("PLATFORM_HOST", "127.0.0.1")
    port = int(os.environ.get("PLATFORM_PORT", "8095"))
    threading.Thread(target=_warm, daemon=True, name="warm-pages").start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()




def _northfeed_notify(data: dict, pdir, photos: list) -> None:
    """Telegram alert for a new website application (NORTHFEED_TG_TOKEN / NORTHFEED_TG_CHAT / NORTHFEED_TG_TOPIC)."""
    import os as _os, urllib.request as _ur, urllib.parse as _up, uuid as _uuid
    tok = (_os.environ.get("NORTHFEED_TG_TOKEN") or "").strip(); chat = (_os.environ.get("NORTHFEED_TG_CHAT") or "").strip(); topic = (_os.environ.get("NORTHFEED_TG_TOPIC") or "").strip()
    if not tok or not chat:
        return
    g = lambda k: data.get(k) or "–"
    why = ", ".join(data.get("why") or []) if isinstance(data.get("why"), list) else g("why")
    lines = ["🩷 Neue Bewerbung · Northfeed",
             f"Name: {g('first_name')} {g('last_name')}",
             f"WhatsApp: {g('phone_cc')} {g('phone')}",
             f"OnlyFans aktiv: {g('active')} · Umsatz: {g('rev')} · Zeit: {g('time') if data.get('time') else g('time2')}",
             f"Rolle/Ziel: {g('role')} / {g('goal')}",
             f"Gesicht zeigen: {g('face')} · Verstecken: {g('hide')} · Sperren: {g('block')}",
             f"Agentur: {g('agency')} · Gründe: {why} {g('why_other') if data.get('why_other') else ''}",
             f"Instagram: {g('instagram')} · TikTok: {g('tiktok')} · Snapchat: {g('snapchat')}",
             f"Links: {g('links')}",
             f"Sprache: {g('lang')} · Einstieg: {g('entry')} · UTM: {g('utm')}",
             f"Fotos: {len(photos)} · Server: /opt/northfeed-applications/"]
    text = "\n".join(lines)
    base = f"https://api.telegram.org/bot{tok}"
    def post(method, fields, files=None):
        boundary = "----nf" + _uuid.uuid4().hex; body = b""
        for k, v in fields.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (fn, blob) in (files or {}).items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + blob + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = _ur.Request(f"{base}/{method}", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return _ur.urlopen(req, timeout=60).read()
    fields = {"chat_id": chat, "text": text}
    if topic: fields["message_thread_id"] = topic
    try:
        post("sendMessage", fields)
    except Exception:  # noqa: BLE001
        return
    if photos:
        try:
            media = []; files = {}
            for i, fn in enumerate(photos[:3]):
                blob = (pdir / fn).read_bytes(); files[f"p{i}"] = (fn, blob); media.append({"type": "photo", "media": f"attach://p{i}"})
            f2 = {"chat_id": chat, "media": json.dumps(media)}
            if topic: f2["message_thread_id"] = topic
            post("sendMediaGroup", f2, files)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()