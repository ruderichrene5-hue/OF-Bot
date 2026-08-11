"""The operational report as a website: one password in front, refreshed on a beat.

`report_server.py` is the loopback version and stays that way -- it is a
diagnostic tool that answers every request by re-reading Airtable, which is fine
when the only way in is an SSH tunnel. This module is the version you open from
a phone. Three things follow from being reachable:

* **A password.** The page names every profile, its failure reasons and the
  shape of the operation. Scrypt-hashed, checked in constant time, and wrong
  guesses are rate-limited per address -- a public port gets knocked on.
* **A fixed five-minute beat.** The page is rendered once per interval and
  served from memory, so a stranger holding down F5 cannot burn the Airtable
  quota or pile up collectors. Everyone sees the same snapshot; the header says
  when it was taken.
* **No controls.** Read-only, no JavaScript, no forms except the login. Nothing
  reachable from here can start a post, park a profile or touch a phone.

Stdlib only, like the rest of the loop tooling: no framework to keep patched on
a box whose job is to stay up.

Configuration (all from the environment, and `/etc/adbbot/env` is the place):

    ADBBOT_SITE_PASSWORD_HASH   scrypt$<salt-hex>$<key-hex>, see `hash_password`
    ADBBOT_SITE_SECRET          random string; signs the session cookie
    ADBBOT_SITE_PORT            default 8088
    ADBBOT_SITE_HOST            default 0.0.0.0

Generate the first two with:

    python -m adb_bot.automation.site --hash-password
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from adb_bot.automation import report, report_html

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8088

#: How often the data behind the page is rebuilt, and what the page promises.
REFRESH_SECONDS = 300
#: A login lasts a fortnight: this is a dashboard someone checks from bed, and
#: an expiry short enough to be annoying is an expiry people work around.
SESSION_SECONDS = 14 * 24 * 3600
COOKIE_NAME = "adbbot_session"

#: Wrong guesses allowed per address before that address is made to wait.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300

# scrypt at these parameters costs ~100ms and 16MB per attempt. That is nothing
# for one real login and a wall for anyone working through a word list.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


# --- passwords ----------------------------------------------------------------

def hash_password(password: str, salt: bytes = b"") -> str:
    """`scrypt$<salt>$<key>`, the form stored in the environment."""
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check. A malformed or missing hash denies rather than raises."""
    try:
        kind, salt_hex, key_hex = (encoded or "").split("$")
        if kind != "scrypt":
            return False
        key = hashlib.scrypt(password.encode("utf-8"),
                             salt=bytes.fromhex(salt_hex), **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), key_hex)


# --- sessions -----------------------------------------------------------------

def _signature(secret: str, expiry: int) -> str:
    return hmac.new(secret.encode("utf-8"), str(expiry).encode("ascii"),
                    hashlib.sha256).hexdigest()


def make_session(secret: str, now: float = 0.0, lifetime: int = SESSION_SECONDS) -> str:
    """`<expiry>.<hmac>`. The cookie carries its own expiry so nothing is stored
    server-side -- a restart in the middle of the night must not log anybody out."""
    expiry = int((now or time.time()) + lifetime)
    return f"{expiry}.{_signature(secret, expiry)}"


def valid_session(token: str, secret: str, now: float = 0.0) -> bool:
    if not token or "." not in token or not secret:
        return False
    expiry_text, signature = token.rsplit(".", 1)
    if not expiry_text.isdigit():
        return False
    if not hmac.compare_digest(signature, _signature(secret, int(expiry_text))):
        return False
    return int(expiry_text) > (now or time.time())


class Throttle:
    """Failed logins per address. Not a security boundary on its own -- the
    scrypt cost is -- but it keeps the journal readable and the CPU idle."""

    def __init__(self, limit: int = MAX_FAILURES, window: int = LOCKOUT_SECONDS):
        self.limit, self.window = limit, window
        self._seen: dict = {}
        self._lock = threading.Lock()

    def locked(self, who: str, now: float = 0.0) -> int:
        """Seconds this address must wait, 0 if it may try."""
        now = now or time.time()
        with self._lock:
            count, first = self._seen.get(who, (0, 0.0))
            if count < self.limit:
                return 0
            waited = now - first
            return 0 if waited >= self.window else int(self.window - waited) + 1

    def failed(self, who: str, now: float = 0.0) -> None:
        now = now or time.time()
        with self._lock:
            count, first = self._seen.get(who, (0, now))
            if now - first >= self.window:        # the old strikes have expired
                count, first = 0, now
            self._seen[who] = (count + 1, first)

    def passed(self, who: str) -> None:
        with self._lock:
            self._seen.pop(who, None)


# --- the page -----------------------------------------------------------------

class PageCache:
    """One render per interval, shared by every viewer.

    The build happens under the lock on purpose. Two collectors running at once
    means two Airtable sweeps for one page, and the second is pure waste: the
    caller that waits gets the first one's result a second later.
    """

    def __init__(self, airtable=None, ttl: int = REFRESH_SECONDS,
                 title: str = "ADB bot", collect=None, render=None):
        self.airtable, self.ttl, self.title = airtable, ttl, title
        self._collect = collect or report.collect
        self._render = render or report_html.render
        self._lock = threading.Lock()
        self._page, self._built = "", 0.0

    def page(self, now: float = 0.0) -> str:
        now = now or time.time()
        with self._lock:
            if self._page and (now - self._built) < self.ttl:
                return self._page
            data = self._collect(airtable=self.airtable, use_cache=False)
            self._page = self._render(data, live=True, title=self.title,
                                      refresh_seconds=self.ttl)
            self._built = now
            return self._page

    def age(self, now: float = 0.0) -> int:
        return int((now or time.time()) - self._built) if self._built else -1


_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --ink:#14161a;
           --muted:#5b6472; --line:#e2e6ec; --accent:#2f6fed; --bad:#c0392b; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0f1115; --card:#171a20; --ink:#e8eaee; --muted:#98a1b0;
             --line:#272c35; --accent:#5b8cff; --bad:#ff6b5e; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
          justify-content:center; background:var(--bg); color:var(--ink);
          font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  form {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:28px; width:min(92vw, 340px); }}
  h1 {{ margin:0 0 4px; font-size:19px; }}
  p {{ margin:0 0 18px; color:var(--muted); font-size:13px; }}
  label {{ display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }}
  input {{ width:100%; padding:10px 12px; font-size:15px; border-radius:8px;
           border:1px solid var(--line); background:var(--bg); color:var(--ink); }}
  input:focus {{ outline:2px solid var(--accent); outline-offset:1px; }}
  button {{ width:100%; margin-top:14px; padding:10px 12px; font-size:15px;
            border:0; border-radius:8px; background:var(--accent); color:#fff;
            cursor:pointer; }}
  .err {{ margin:0 0 14px; padding:8px 10px; border-radius:8px; font-size:13px;
          color:var(--bad); border:1px solid var(--bad); background:transparent; }}
</style></head>
<body>
  <form method="post" action="/login">
    <h1>{title}</h1>
    <p>Posting report — sign in to view.</p>
    {error}
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password"
           autofocus required>
    <button type="submit">Sign in</button>
  </form>
</body></html>
"""


def login_page(title: str = "ADB bot", error: str = "") -> str:
    block = f'<p class="err">{error}</p>' if error else ""
    return _LOGIN_PAGE.format(title=title, error=block)


# --- the server ---------------------------------------------------------------

class SiteHandler(BaseHTTPRequestHandler):
    server_version = "adbbot-site"
    protocol_version = "HTTP/1.1"

    cache: PageCache = None          # type: ignore[assignment]
    throttle: Throttle = None        # type: ignore[assignment]
    password_hash = ""
    secret = ""
    page_title = "ADB bot"

    # -- plumbing --

    def _send(self, code: int, body: bytes, content_type: str, headers=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # A dashboard is never worth caching, and the login page least of all.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, code: int, page: str, headers=()) -> None:
        self._send(code, page.encode("utf-8"), "text/html; charset=utf-8", headers)

    def _redirect(self, where: str, headers=()) -> None:
        self._send(303, b"", "text/plain; charset=utf-8",
                   tuple(headers) + (("Location", where),))

    @property
    def _who(self) -> str:
        # Behind a reverse proxy the peer is the proxy; trust the forwarded
        # address only when one is present, since this port may also be direct.
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def _signed_in(self) -> bool:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return False
        try:
            jar = SimpleCookie(raw)
        except Exception:
            return False
        morsel = jar.get(COOKIE_NAME)
        return bool(morsel) and valid_session(morsel.value, self.secret)

    def _cookie(self, token: str, seconds: int) -> tuple:
        # No Secure flag: this is served over plain HTTP unless someone puts a
        # certificate in front, and a cookie the browser refuses to send is a
        # login screen that never goes away.
        return ("Set-Cookie",
                f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={seconds}")

    # -- routes --

    def do_GET(self) -> None:                      # noqa: N802 (stdlib API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path == "/logout":
            self._redirect("/", [self._cookie("", 0)])
            return
        if path != "/":
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if not self._signed_in():
            self._html(200, login_page(self.page_title))
            return
        try:
            self._html(200, self.cache.page())
        except Exception as exc:
            # Never leak a traceback to a public port; the journal gets the detail.
            sys.stderr.write(f"site: render failed: {type(exc).__name__}: {exc}\n")
            self._html(500, login_page(self.page_title,
                                       "The report failed to render — check the logs."))

    do_HEAD = do_GET                                # noqa: N815

    def do_POST(self) -> None:                     # noqa: N802 (stdlib API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in ("/", "/login"):
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return

        waiting = self.throttle.locked(self._who)
        if waiting:
            self._html(429, login_page(
                self.page_title,
                f"Too many attempts. Try again in {waiting // 60 + 1} minute(s)."))
            return

        length = int(self.headers.get("Content-Length") or 0)
        # A password is short. Anything longer is not a login attempt.
        if length > 4096:
            self._send(413, b"too large\n", "text/plain; charset=utf-8")
            return
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        password = (parse_qs(body).get("password") or [""])[0]

        if not verify_password(password, self.password_hash):
            self.throttle.failed(self._who)
            sys.stderr.write(f"site: failed login from {self._who}\n")
            self._html(401, login_page(self.page_title, "Wrong password."))
            return

        self.throttle.passed(self._who)
        sys.stderr.write(f"site: signed in from {self._who}\n")
        self._redirect("/", [self._cookie(make_session(self.secret), SESSION_SECONDS)])

    def log_message(self, fmt, *args) -> None:
        line = fmt % args
        if "/healthz" in line:
            return
        sys.stderr.write("site: %s\n" % line)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, password_hash: str,
          secret: str, airtable=None, ttl: int = REFRESH_SECONDS,
          title: str = "ADB bot") -> None:
    handler = type("BoundSiteHandler", (SiteHandler,), {
        "cache": PageCache(airtable=airtable, ttl=ttl, title=title),
        "throttle": Throttle(),
        "password_hash": password_hash,
        "secret": secret,
        "page_title": title,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    sys.stderr.write(f"site: serving on http://{host}:{port} "
                     f"(data rebuilt every {ttl}s)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_airtable():
    """Best-effort: the page degrades to local sections rather than failing."""
    try:
        from adb_bot.automation.run_loop import _airtable
        return _airtable()
    except Exception as exc:
        sys.stderr.write(f"site: no Airtable client ({exc}); local sections only\n")
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("ADBBOT_SITE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("ADBBOT_SITE_PORT", DEFAULT_PORT)))
    parser.add_argument("--refresh", type=int, default=REFRESH_SECONDS,
                        help="seconds between rebuilds of the page (default 300)")
    parser.add_argument("--title", default="ADB bot")
    parser.add_argument("--hash-password", action="store_true",
                        help="print a hash + secret for /etc/adbbot/env and exit")
    args = parser.parse_args(argv)

    if args.hash_password:
        password = getpass.getpass("password: ")
        if password != getpass.getpass("again: "):
            print("they do not match", file=sys.stderr)
            return 2
        print(f"ADBBOT_SITE_PASSWORD_HASH={hash_password(password)}")
        print(f"ADBBOT_SITE_SECRET={secrets.token_urlsafe(32)}")
        return 0

    password_hash = os.environ.get("ADBBOT_SITE_PASSWORD_HASH", "").strip()
    secret = os.environ.get("ADBBOT_SITE_SECRET", "").strip()
    if not password_hash or not secret:
        # Refusing is the whole point: a dashboard that opens without a password
        # because a variable was misspelled is worse than one that will not start.
        print("site: ADBBOT_SITE_PASSWORD_HASH and ADBBOT_SITE_SECRET must be set "
              "(run with --hash-password)", file=sys.stderr)
        return 2

    serve(args.host, args.port, password_hash=password_hash, secret=secret,
          airtable=_build_airtable(), ttl=args.refresh, title=args.title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
