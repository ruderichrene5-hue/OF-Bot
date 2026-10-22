"""Daily iPhone posting launcher (Rene 22.10: "auf allen" -- every Healthy account posts, every day, no manual Go).

Reads Accounts SOP: Model = <model>, Status = Healthy, Container set -> groups the containers per phone, drops
phones that do not answer (WDA /status), checks today's spoofed variants exist, then runs iphone_xx_post_batch
for today in-process (batches of 3, 09-23 Berlin, every clip of the day). Started by adbbot-iphone-posting.timer
at 09:05 Berlin; `--phones D` restarts a single phone later (own state tag), `--dry` only prints the plan.

    .venv/bin/python -m adb_bot.automation.iphone_posting_daily --model Nikki [--dry] [--phones D,E] [--reels 3]
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from adb_bot.automation import iphone_diag
from adb_bot.automation.iphone_farm_wda import phones as wda_phones

log = logging.getLogger("iphone_posting_daily")
BERLIN = ZoneInfo("Europe/Berlin")
BASE, T_ACC, T_CONT = "appwk9LrgAhJv8iEH", "tbly0y1I4hNDlvh1K", "tblyZXN7caKZZBaBj"
LETTER = {"iphone1": "A", "iphone2": "B", "iphone3": "C", "iphone4": "D", "iphone5": "E", "iphone6": "I", "iphone7": "G",
          "iphone8": "H", "iphone9": "F"}


def _hdr() -> dict:
    tok = os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY")
    if not tok:
        raise SystemExit("AIRTABLE_TOKEN not set")
    return {"Authorization": f"Bearer {tok}"}


def healthy_containers(model: str) -> dict[str, list[str]]:
    """{phone letter: [containers]} for every Healthy account of the model that has a container."""
    out: dict[str, list[str]] = {}
    params = {"filterByFormula": f"AND({{Model (auto)}}='{model}',{{Status}}='Healthy',{{Container}}!='')",
              "fields[]": ["Username", "Container"], "pageSize": 100}
    offset = None
    while True:
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_ACC}", headers=_hdr(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        for rec in j.get("records", []):
            cid = rec["fields"]["Container"][0]
            c = requests.get(f"https://api.airtable.com/v0/{BASE}/{T_CONT}/{cid}", headers=_hdr(), timeout=30).json()["fields"]
            name = c.get("Container") or ""
            if name[:1].isalpha() and name[1:].isdigit():
                out.setdefault(name[0], []).append(name)
        offset = j.get("offset")
        if not offset:
            break
    for k in out:
        out[k].sort(key=lambda n: int(n[1:]))
    return out


def phone_up(letter: str) -> bool:
    for _rec, p in wda_phones().items():
        if LETTER.get(str(p.get("stream"))) == letter:
            try:
                from adb_bot.automation.iphone_farm_wda import _relay_host
                return "sessionId" in requests.get(f"http://{_relay_host()}:{p['wda_port']}/status", timeout=6).text
            except Exception:                                 # noqa: BLE001
                return False
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="Nikki")
    ap.add_argument("--date", default=None)
    ap.add_argument("--phones", default="", help="only these letters, e.g. D or D,E (own state tag)")
    ap.add_argument("--reels", type=int, default=0, help="reels per account today (default: every clip)")
    ap.add_argument("--window", default="9-23")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    date = a.date or dt.datetime.now(BERLIN).strftime("%Y-%m-%d")
    clips = Path(f"/opt/adbbot/manual_spoof/{date}/extra/{a.model}/DE")
    groups = healthy_containers(a.model)
    only = [x.strip().upper() for x in a.phones.split(",") if x.strip()]
    plan, skipped = [], []
    for letter, conts in sorted(groups.items()):
        if only and letter not in only:
            continue
        if not phone_up(letter):
            skipped.append(f"{letter} ({len(conts)} accounts, phone not answering)")
            continue
        plan.append(",".join(conts))
    log.info("%s %s: %d phones -> %s%s", a.model, date, len(plan), " ".join(plan), f"; skipped {skipped}" if skipped else "")
    if not clips.is_dir() or not any(clips.iterdir()):
        iphone_diag.notify(f"posting {a.model} {date}: no spoofed variants in {clips} -- nothing started")
        return 2
    if not plan:
        iphone_diag.notify(f"posting {a.model} {date}: no phone with Healthy accounts answers -- nothing started ({skipped})")
        return 3
    if skipped:
        iphone_diag.notify(f"posting {a.model} {date} started on {len(plan)} phones; skipped: {', '.join(skipped)}")
    if a.dry:
        return 0
    from adb_bot.automation.iphone_xx_post_batch import main as post_main
    # 22.10 live: our basicConfig above made post_main's own basicConfig (stream + xx_post_<date>.log) a no-op,
    # so the whole run logged to the journal only. Hand the root logger back before the batch sets it up.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    args = [date, *plan, "--model", a.model, "--window", a.window]
    if a.reels:
        args += ["--reels", str(a.reels)]
    if only:
        args += ["--tag", "".join(only)]
    return post_main(args)


if __name__ == "__main__":
    sys.exit(main())
