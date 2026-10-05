"""Physical-iPhone-farm overview: which proxy sits in which Crane container on
which phone, and which account lives there. Reads the "Reels" Airtable base:

* Containers -- one row per Crane container (Container, iPhone, Proxy, Status,
  Daily Posting) linked to its Accounts rows (accounts still being created are
  Accounts rows with a Creation Status; the separate Account Creation table was
  merged into Accounts on 2026-10-05). This is the
  source of truth since 2026-10-02; it replaced the "A1  http://..." lines
  that used to be parsed out of each Device's Notes field (those lists are
  stale -- the Containers table carries different proxies for the same ids).
* Device -- one row per physical iPhone. Its Notes field is deliberately NOT
  read any more (Rene, 2026-10-02: only what the Reels tables say counts).

Rene, 2026-10-01: "ich brauche von dir auch die ganzen proxies ... so ein
Display, mit was verbunden ist und mit welchem Handy". 2026-10-02: "immer wenn
was in Airtable geändert wird, das auch hier geändert wird" -- the page is
rebuilt from Airtable on every view (short server cache), nothing is copied.
"""

from __future__ import annotations

import html
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

REELS_BASE = "appwk9LrgAhJv8iEH"
DEVICE_TABLE = "tbl6OZrzFuns7rux6"
ACCOUNTS_TABLE = "tbly0y1I4hNDlvh1K"      # also holds accounts being created (Creation Status) since 2026-10-05
CONTAINERS_TABLE = "tblyZXN7caKZZBaBj"

_PROXY_RE = re.compile(r"^\s*\w+://(?:([^:@\s]+):([^@\s]+)@)?([^:/\s]+):(\d+)")


def _all(table: str) -> list[dict]:
    tok = os.environ.get("AIRTABLE_TOKEN", "")
    out, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{REELS_BASE}/{table}",
                         headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out.extend(j.get("records", []))
        offset = j.get("offset")
        if not offset:
            return out


def _select(value):
    return value.get("name") if isinstance(value, dict) else value


def _natural(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text or "")]


def parse_proxy(proxy: str) -> dict:
    m = _PROXY_RE.match(proxy or "")
    if not m:
        return {"user": "", "password": "", "host": (proxy or "").strip(), "port": ""}
    return {"user": m.group(1) or "", "password": m.group(2) or "",
            "host": m.group(3), "port": m.group(4)}


def load() -> list[dict]:
    """One dict per physical iPhone, containers sorted A1, A2 ... A10."""
    with ThreadPoolExecutor(3) as pool:
        devices, containers, accounts = pool.map(_all, (DEVICE_TABLE, CONTAINERS_TABLE, ACCOUNTS_TABLE))
    accounts = {r["id"]: r.get("fields", {}) for r in accounts}

    phones = {}
    for rec in devices:
        f = rec.get("fields", {})
        if _select(f.get("Device Type")) != "Physical iPhone" and "iphone" not in str(f.get("iPhone UID", "")).lower():
            continue  # the rest of this table are MLX profiles, not devices
        phones[rec["id"]] = {
            "id": rec["id"],
            "name": str(f.get("iPhone UID") or "iPhone"),
            "max_containers": f.get("Max Containers"),
            "containers": [],
        }
    by_name = {p["name"].lower(): p for p in phones.values()}

    for rec in containers:
        f = rec.get("fields", {})
        dev_ids = f.get("Device") or []
        phone = phones.get(dev_ids[0]) if dev_ids else by_name.get(str(_select(f.get("iPhone")) or "").lower())
        if phone is None:
            name = str(_select(f.get("iPhone")) or "No iPhone")
            phone = by_name.setdefault(name.lower(), {
                "id": name, "name": name,
                "max_containers": None, "containers": []})
            phones.setdefault(phone["id"], phone)
        linked = []
        for acc_id in f.get("Accounts") or []:
            a = accounts.get(acc_id, {})
            # the Accounts primary field is called "Accounts" (the IG username); accounts still
            # being created carry a Creation Status other than Verified (merged from the former
            # Account Creation table, 2026-10-05)
            creating = _select(a.get("Creation Status")) or ""
            being_created = bool(creating) and creating != "Verified"
            linked.append({"username": a.get("Accounts") or a.get("Username") or (a.get("Email") if being_created else "") or "",
                           "status": (creating if being_created else _select(a.get("Status"))) or "",
                           "kind": "creation" if being_created else "account"})
        phone["containers"].append({
            "container": str(f.get("Container") or "?"),
            "status": _select(f.get("Status")) or "",
            "daily_posting": str(f.get("Daily Posting") or ""),
            "proxy": parse_proxy(str(f.get("Proxy") or "")),
            "accounts": linked,
        })

    out = sorted(phones.values(), key=lambda p: _natural(p["name"]))
    for p in out:
        p["containers"].sort(key=lambda c: _natural(c["container"]))
    return out


def _account_cell(c: dict) -> str:
    if not c["accounts"]:
        return '<span class="empty">free</span>'
    parts = []
    for a in c["accounts"]:
        name = f"<code>@{html.escape(a['username'])}</code>" if a["username"] else '<span class="empty">no name</span>'
        tag = ' <span class="tag">being created</span>' if a["kind"] == "creation" else ""
        status = f' <span class="muted">({html.escape(a["status"])})</span>' if a["status"] else ""
        parts.append(name + tag + status)
    return "<br>".join(parts)


def _row(c: dict) -> str:
    p = c["proxy"]
    hostport = f"{p['host']}:{p['port']}" if p["port"] else p["host"]
    return (f"<tr><td><strong>{html.escape(c['container'])}</strong></td>"
            f"<td><code>{html.escape(hostport) or '—'}</code></td>"
            f"<td><code>{html.escape(p['user'])}</code></td>"
            f"<td><code>{html.escape(p['password'])}</code></td>"
            f"<td>{_account_cell(c)}</td>"
            f"<td>{html.escape(c['status'])}</td>"
            f"<td>{html.escape(c['daily_posting'])}</td></tr>")


def build() -> str:
    phones = load()
    cards = []
    for dev in phones:
        used = sum(1 for c in dev["containers"] if c["accounts"])
        rows = "\n".join(_row(c) for c in dev["containers"]) or (
            "<tr><td colspan=7 class=empty>No containers linked in Airtable</td></tr>")
        cards.append(f"""
        <section class="card">
          <h2>{html.escape(dev['name'])}</h2>
          <div class="meta">
            {len(dev['containers'])} containers{' / ' + html.escape(str(dev['max_containers'])) if dev['max_containers'] else ''}
            &nbsp;·&nbsp; {used} with an account
          </div>
          <div class="scroll"><table>
            <thead><tr><th>Container</th><th>Proxy host:port</th><th>User</th><th>Password</th>
              <th>Account</th><th>Status</th><th>Daily posting</th></tr></thead>
            <tbody>{rows}</tbody>
          </table></div>
        </section>""")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>iPhone Farm — Proxies & Containers</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#0b0d12; color:#e6e8ee; font:15px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;
          margin:0; padding:24px 16px 60px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:#9aa3b2; font-size:13px; margin:0 0 20px; }}
  .card {{ background:#161a22; border:1px solid #262c38; border-radius:12px;
           padding:18px 20px; margin-bottom:20px; max-width:1100px; }}
  .card h2 {{ margin:0 0 6px; font-size:17px; }}
  .meta {{ color:#9aa3b2; font-size:13px; margin-bottom:10px; }}
  .scroll {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #222631; vertical-align:top; }}
  th {{ color:#9aa3b2; font-weight:600; white-space:nowrap; }}
  .empty, .muted {{ color:#9aa3b2; }} .empty {{ font-style:italic; }}
  .tag {{ font-size:11px; color:#d29922; border:1px solid #5a4614; border-radius:5px; padding:0 5px; }}
  code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
</style></head>
<body>
<h1>iPhone Farm — Proxies &amp; Containers</h1>
<p class="sub">Live from Airtable (Containers table) · refreshes itself every minute</p>
{''.join(cards) if cards else '<p>No devices found.</p>'}
</body></html>"""


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/root/.adb_bot/observer/phones.html"
    import os as _os
    _os.makedirs(_os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(build())
    print(out)
