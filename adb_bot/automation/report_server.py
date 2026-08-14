"""A small local HTTP server that renders the operational report on request.

Stdlib only, and bound to the loopback interface by default. This box holds
Instagram sessions and an Airtable PAT; a dashboard that lists every profile by
name and its failure reasons has no business being reachable from the network
without someone deciding that on purpose. To view it from a laptop, forward the
port instead:

    ssh -N -L 8080:localhost:8080 <this-box>

Rendering happens per request, so the page is as fresh as the collector's short
cache allows. Requests are served on threads: one slow Airtable read must not
make the page look hung.
"""

from __future__ import annotations

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from adb_bot.automation import report, report_html

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def _build_airtable(logger=None):
    """Best-effort Airtable client; the page degrades rather than failing."""
    try:
        from adb_bot.automation.run_loop import _airtable
        return _airtable()
    except Exception as exc:      # no token, bad base, import trouble
        if logger:
            logger.warning("report: no Airtable client (%s); serving local sections only", exc)
        return None


class ReportHandler(BaseHTTPRequestHandler):
    server_version = "adbbot-report"
    airtable = None
    page_title = "ADB bot"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should ever be cached by the browser: a stale dashboard
        # is worse than no dashboard.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:                      # noqa: N802 (stdlib API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path != "/":
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        try:
            data = report.collect(airtable=self.airtable)
            page = report_html.render(data, live=True, title=self.page_title)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        except Exception as exc:
            # Show the error in the browser instead of an empty response; this
            # is a diagnostic tool, so a visible traceback beats a dead tab.
            body = f"report failed to render: {type(exc).__name__}: {exc}\n".encode()
            self._send(500, body, "text/plain; charset=utf-8")

    do_HEAD = do_GET                                # noqa: N815

    def log_message(self, fmt, *args) -> None:
        # One line per request into the unit's journal, not stderr's default
        # format, and never for the health probe.
        if "/healthz" in (args[0] if args else ""):
            return
        sys.stderr.write("report: %s\n" % (fmt % args))


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, airtable=None,
          logger=None, title: str = "ADB bot") -> None:
    handler = type("BoundReportHandler", (ReportHandler,),
                   {"airtable": airtable, "page_title": title})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    message = f"report: serving on http://{host}:{port} (Ctrl-C to stop)"
    (logger.info(message) if logger else print(message, flush=True))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve the ADB bot operational report.")
    parser.add_argument("--host", default=os.environ.get("ADBBOT_REPORT_HOST", DEFAULT_HOST),
                        help="interface to bind (default loopback only)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("ADBBOT_REPORT_PORT", DEFAULT_PORT)))
    parser.add_argument("--no-airtable", action="store_true",
                        help="skip Airtable entirely; serve only local sections")
    args = parser.parse_args(argv)

    airtable = None if args.no_airtable else _build_airtable()
    serve(host=args.host, port=args.port, airtable=airtable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
