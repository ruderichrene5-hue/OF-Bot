"""Put a bought batch of proxies into Airtable's Containers table, 25 per phone (Rene 2026-10-12).

    .venv/bin/python3 -m adb_bot.automation.iphone_farm_proxy_import /root/proxies_new.txt          # plan only
    .venv/bin/python3 -m adb_bot.automation.iphone_farm_proxy_import /root/proxies_new.txt --apply  # write rows

Input: one proxy per line, "ip:port:user:password" (the seller's format). Lines already in Airtable are skipped.
Order: first the new phones (G, H, I) up to 25 containers each, then A-E up to 25 each, lowest first.
Each proxy becomes a Containers row "<letter><n>" (n continues after the phone's highest number) with
Proxy "http://user:password@ip:port", iPhone "iPhone <letter>" and the Device link.
The phones themselves are changed afterwards by `iphone_farm_containers apply iphoneN` (Crane containers +
Shadowrocket per container). Nothing here prints a proxy's login.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import requests

BASE, T_CONT = "appwk9LrgAhJv8iEH", "tblyZXN7caKZZBaBj"
PER_PHONE = 25
# letter -> (Device record, stream). Stickers on the phones = letters (10-12: F is free, I = the first iPhone 8).
PHONES = {"G": ("rec1LI87qC7VsOR24", "iphone7"), "H": ("recpejbxw0aZujyyM", "iphone8"), "I": ("recpwDsV7NtNOBjNv", "iphone6"),
          "A": ("recRsaDtxs4l4mYMh", "iphone1"), "B": ("recOPO3NqF4vvtJru", "iphone2"), "C": ("recsa0BcUdzuWXE3s", "iphone3"),
          "D": ("recHMlCtzugm6Bslb", "iphone4"), "E": ("recDW5MoC4xuHN0oX", "iphone5")}
ORDER = ["G", "H", "I", "A", "B", "C", "D", "E"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}


def _rows() -> list:
    out, off = [], None
    while True:
        p = {"pageSize": 100, "fields[]": ["Container", "Proxy"]}
        if off:
            p["offset"] = off
        j = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_CONT}", params=p, headers=_headers(), timeout=30).json()
        out += j.get("records", [])
        off = j.get("offset")
        if not off:
            return out


def parse(path: str) -> list[tuple[str, str]]:
    """[(host:port, http url)] in file order, duplicates dropped."""
    seen, out = set(), []
    for line in open(path, encoding="utf-8"):
        parts = line.strip().split(":")
        if len(parts) != 4 or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[0]):
            continue
        ip, port, user, pw = parts
        if f"{ip}:{port}" not in seen:
            seen.add(f"{ip}:{port}")
            out.append((f"{ip}:{port}", f"http://{user}:{pw}@{ip}:{port}"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    proxies = parse(a.file)
    rows = _rows()
    have = {re.sub(r"^.*@", "", (r["fields"].get("Proxy") or "").strip()) for r in rows}
    fresh = [p for p in proxies if p[0] not in have]
    count, top = {k: 0 for k in PHONES}, {k: 0 for k in PHONES}
    for r in rows:
        m = re.fullmatch(r"([A-Z])(\d+)", r["fields"].get("Container", ""))
        if m and m.group(1) in PHONES:
            top[m.group(1)] = max(top[m.group(1)], int(m.group(2)))
            if (r["fields"].get("Proxy") or "").strip():
                count[m.group(1)] += 1
    print(f"{len(proxies)} proxies in the file, {len(proxies) - len(fresh)} already in Airtable, {len(fresh)} new")
    plan, i = [], 0
    for k in ORDER:
        while count[k] < PER_PHONE and i < len(fresh):
            top[k] += 1
            count[k] += 1
            plan.append((k, f"{k}{top[k]}", fresh[i][1]))
            i += 1
    for k in ORDER:
        print(f"  iPhone {k}: {count[k]}/{PER_PHONE}" + (f"  (+{sum(1 for p in plan if p[0] == k)} new)" if any(p[0] == k for p in plan) else ""))
    missing = sum(PER_PHONE - count[k] for k in ORDER)
    print(f"left over: {len(fresh) - i} | still missing for 25 on every phone: {missing}")
    if not a.apply:
        print("plan only -- run again with --apply to write the rows")
        return 0
    recs = [{"fields": {"Container": name, "iPhone": f"iPhone {k}", "Proxy": url, "Device": [PHONES[k][0]]}} for k, name, url in plan]
    for n in range(0, len(recs), 10):
        r = requests.post(f"https://api.airtable.com/v0/{BASE}/{T_CONT}", headers=_headers(), json={"records": recs[n:n + 10], "typecast": True}, timeout=30)
        if r.status_code != 200:
            print("Airtable error:", r.status_code, r.text[:300])
            return 1
    print(f"{len(recs)} rows written. Next, per phone with new rows:")
    for k in ORDER:
        if any(p[0] == k for p in plan):
            print(f"  .venv/bin/python3 -m adb_bot.automation.iphone_farm_containers apply {PHONES[k][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
