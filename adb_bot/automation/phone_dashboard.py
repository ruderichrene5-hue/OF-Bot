"""Physical-iPhone-farm overview: which proxy sits in which Crane container on
which phone. Reads the "Reels" Airtable base's Device table -- the physical
iPhones -- and parses the container<->proxy pairing straight out of each
device's free-text Notes field (written by Claude alongside the proxy
list, see `A1  http://user:pass@ip:port` lines), rather than inventing a new
table for something that already fits in the one that's there.

Rene, 2026-10-01: "ich brauche von dir auch die ganzen proxies ... so ein
Display, mit was verbunden ist und mit welchem Handy".
"""

from __future__ import annotations

import html
import os
import re

import requests

REELS_BASE = "appwk9LrgAhJv8iEH"
DEVICE_TABLE = "tbl6OZrzFuns7rux6"
ACCOUNTS_TABLE = "tbly0y1I4hNDlvh1K"

# "A1  http://user:pass@host:port" (also tolerates no/odd spacing)
_LINE_RE = re.compile(
    r"^\s*([A-Za-z]?\d+)\s+http://([^:@\s]+):([^@\s]+)@([^:\s]+):(\d+)\s*$",
    re.MULTILINE,
)
_IP_RE = re.compile(r"Lokale IP[^:]*:\s*([0-9.]+)")
_LIVE_RE = re.compile(r"^\s*(✅[^\n]*)", re.MULTILINE)


def _devices() -> list[dict]:
    tok = os.environ.get("AIRTABLE_TOKEN", "")
    out, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{REELS_BASE}/{DEVICE_TABLE}",
                         headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            name = str(f.get("iPhone UID") or "")
            if "iphone" not in name.lower():
                continue  # the other 130-odd rows in this table are MLX profiles, not devices
            out.append({
                "name": name,
                "notes": str(f.get("Notes") or ""),
                "containers": f.get("Containers Used") or "",
                "max_containers": f.get("Max Containers") or "",
                "free": f.get("Containers Free"),
            })
        offset = j.get("offset")
        if not offset:
            return out


def _account_by_container() -> dict:
    """{(iphone label, container) -> {username, status}} from the Accounts table,
    so a container already in use shows *which* account lives there -- the
    "mit welchem Account verbunden" half of the request, not just the proxy."""
    tok = os.environ.get("AIRTABLE_TOKEN", "")
    out, offset = {}, None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{REELS_BASE}/{ACCOUNTS_TABLE}",
                         headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        for rec in j.get("records", []):
            f = rec.get("fields", {})
            iphone = str(f.get("iPhone") or "").strip()
            container = str(f.get("Container") or "").strip()
            user = str(f.get("Username") or "").strip()
            if iphone and container and user:
                status = f.get("Status")
                if isinstance(status, dict):
                    status = status.get("name")
                out[(iphone.lower(), container.lower())] = {"username": user, "status": status}
        offset = j.get("offset")
        if not offset:
            return out


def parse_device(notes: str) -> dict:
    """{local_ip, live_note, containers: [{container, user, password, host, port}]}"""
    ip = _IP_RE.search(notes)
    live = _LIVE_RE.search(notes)
    containers = [
        {"container": m.group(1), "user": m.group(2), "password": m.group(3),
         "host": m.group(4), "port": m.group(5)}
        for m in _LINE_RE.finditer(notes)
    ]
    return {
        "local_ip": ip.group(1) if ip else None,
        "live_note": live.group(1).strip() if live else None,
        "containers": containers,
    }


def _row(c: dict, device_name: str, accounts: dict) -> str:
    acc = accounts.get((device_name.lower(), c["container"].lower()))
    if acc:
        status = f" ({html.escape(str(acc['status']))})" if acc.get("status") else ""
        account_cell = f"<code>@{html.escape(acc['username'])}</code>{status}"
    else:
        account_cell = '<span class="empty">frei</span>'
    return (f"<tr><td>{html.escape(c['container'])}</td>"
            f"<td><code>{html.escape(c['host'])}:{html.escape(c['port'])}</code></td>"
            f"<td><code>{html.escape(c['user'])}</code></td>"
            f"<td><code>{html.escape(c['password'])}</code></td>"
            f"<td>{account_cell}</td></tr>")


def build() -> str:
    devices = _devices()
    accounts = _account_by_container()
    cards = []
    for dev in devices:
        parsed = parse_device(dev["notes"])
        rows = "\n".join(_row(c, dev["name"], accounts) for c in parsed["containers"]) or (
            "<tr><td colspan=5 class=empty>Keine Proxy-Zeilen im Notizfeld gefunden</td></tr>")
        live = (f'<div class="live">{html.escape(parsed["live_note"])}</div>'
                if parsed["live_note"] else "")
        cards.append(f"""
        <section class="card">
          <h2>{html.escape(dev['name'])}</h2>
          <div class="meta">
            Lokale IP: <code>{html.escape(parsed['local_ip'] or '—')}</code>
            &nbsp;·&nbsp; Container: {html.escape(str(dev['containers']) or '—')}
            {'/ ' + html.escape(str(dev['max_containers'])) if dev['max_containers'] else ''}
            &nbsp;·&nbsp; {len(parsed['containers'])} Proxy-Einträge erkannt
          </div>
          {live}
          <table>
            <thead><tr><th>Container</th><th>Host:Port</th><th>User</th><th>Passwort</th><th>Account</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </section>""")

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iPhone-Farm — Proxys & Container</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#0b0d12; color:#e6e8ee; font:15px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;
          margin:0; padding:24px 16px 60px; }}
  h1 {{ font-size:20px; margin:0 0 20px; }}
  .card {{ background:#161a22; border:1px solid #262c38; border-radius:12px;
           padding:18px 20px; margin-bottom:20px; max-width:720px; }}
  .card h2 {{ margin:0 0 6px; font-size:17px; }}
  .meta {{ color:#9aa3b2; font-size:13px; margin-bottom:10px; }}
  .live {{ color:#7ee787; font-size:13px; margin-bottom:12px; white-space:pre-wrap; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #222631; }}
  th {{ color:#9aa3b2; font-weight:600; }}
  td.empty {{ color:#9aa3b2; font-style:italic; }}
  code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
</style></head>
<body>
<h1>iPhone-Farm — Proxys &amp; Container</h1>
{''.join(cards) if cards else '<p>Keine Geräte gefunden.</p>'}
</body></html>"""


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/root/.adb_bot/observer/phones.html"
    import os as _os
    _os.makedirs(_os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(build())
    print(out)
