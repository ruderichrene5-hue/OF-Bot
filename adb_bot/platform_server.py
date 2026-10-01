"""Team platform (first page: Accounts dashboard). Stdlib only.

Binds 127.0.0.1 by default: it is meant to sit behind a Cloudflare Tunnel +
Cloudflare Access (e-mail login), never directly on the internet. A shared
password (PLATFORM_PASSWORD, HTTP basic auth) is an extra lock for local use.
The page is rebuilt at most every REBUILD_SECONDS and served from memory.
"""

from __future__ import annotations

import base64
import hmac
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REBUILD_SECONDS = 300
_cache = {"at": 0.0, "html": b""}
_lock = threading.Lock()
_phones_cache = {"at": 0.0, "html": b""}
_phones_lock = threading.Lock()


def _page() -> bytes:
    with _lock:
        if time.time() - _cache["at"] > REBUILD_SECONDS or not _cache["html"]:
            from adb_bot.automation import observer_dashboard
            try:
                _cache["html"] = observer_dashboard.build().encode("utf-8")
            except Exception as exc:                       # show the error, keep serving
                _cache["html"] = f"<pre>Dashboard error: {exc}</pre>".encode()
            _cache["at"] = time.time()
        return _cache["html"]


def _phones_page() -> bytes:
    with _phones_lock:
        if time.time() - _phones_cache["at"] > REBUILD_SECONDS or not _phones_cache["html"]:
            from adb_bot.automation import phone_dashboard
            try:
                _phones_cache["html"] = phone_dashboard.build().encode("utf-8")
            except Exception as exc:
                _phones_cache["html"] = f"<pre>Dashboard error: {exc}</pre>".encode()
            _phones_cache["at"] = time.time()
        return _phones_cache["html"]


class Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        pw = os.environ.get("PLATFORM_PASSWORD", "")
        if not pw:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            _, given = base64.b64decode(hdr[6:]).decode().split(":", 1)
        except Exception:
            return False
        return hmac.compare_digest(given, pw)

    def do_GET(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Platform"')
            self.end_headers()
            return
        if self.path.split("?")[0] in ("/", "/accounts"):
            body = _page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.split("?")[0] == "/phones":
            body = _phones_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
