"""iPhone farm -- THE flow file (Rene, 22.10: "Ich will sauber die ganzen Flows in einer Datei haben. Ich will
sauber, dass man die immer checkt, was man braucht.").

Seven flows. Six run today, one is planned and already has its place and its checks:

  signup     create an Instagram account in a container        iphone_signup.run_row            ready
  warmup     four warm-up days, one script per day             iphone_warmup.run_one / tick     ready
  posting    today's spoofed reels, batches per account        iphone_xx_post_batch.run_phone   ready
  picture    profile picture from the Airtable attachment     iphone_setup.run_step            ready
  bio        'Bio Text' from the row, default text if empty     iphone_setup.run_step            ready
  highlight  story with the OF link, saved into a highlight    iphone_setup.run_step            ready
  story      a plain story post                                iphone_ig.create_story           planned
Setup steps are driven by material + checkbox in Airtable, not by a warm-up day (iphone_setup tick).

Every flow lists what it NEEDS. `check` runs those needs against a real phone/container before anything
touches Instagram, and prints one line per need. Nothing here implements a flow -- the logic stays in the
module named on each line, this file only knows which one and what it needs.

    .venv/bin/python -m adb_bot.automation.iphone_flows list
    .venv/bin/python -m adb_bot.automation.iphone_flows check posting H H4 [--date 2026-10-22] [--model Nikki]
    .venv/bin/python -m adb_bot.automation.iphone_flows check warmup iphone8 H4
    .venv/bin/python -m adb_bot.automation.iphone_flows check-all H           # the phone-level needs only

Phones: A=iphone1 B=iphone2 C=iphone3 D=iphone4 E=iphone5 I=iphone6 G=iphone7 H=iphone8 F=iphone9 J/K/L=iphone11-13
(J/K/L are reserve phones: no containers, no shortcuts). Where a flow gets stuck it keeps a screenshot:
/opt/adbbot/logs/screens/<date>/ (iphone_diag).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from adb_bot.automation import iphone_farm_config as C
from adb_bot.automation import iphone_phone_lock as phone_lock
from adb_bot.automation.iphone_farm_wda import WDAError, connect, resolve

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
LETTER = {"iphone1": "A", "iphone2": "B", "iphone3": "C", "iphone4": "D", "iphone5": "E", "iphone6": "I", "iphone7": "G",
          "iphone8": "H", "iphone9": "F", "iphone11": "J", "iphone12": "K", "iphone13": "L"}
STREAM = {v: k for k, v in LETTER.items()}


@dataclasses.dataclass
class Result:
    need: str
    state: str                   # OK | WARN | FAIL | INFO
    detail: str = ""


@dataclasses.dataclass
class Flow:
    name: str
    title: str
    status: str                  # ready | planned
    module: str
    entry: str
    cli: str
    needs: list[str]             # names of checks below, run in order
    account_needs: list[str] = dataclasses.field(default_factory=list)   # Airtable fields the account row must have


# ---------------------------------------------------------------- phone resolution

def phone(ref: str) -> tuple[str, dict, str]:
    """('recXXX', phone dict, letter) from a letter (H), a stream (iphone8), a record id or an ssh alias."""
    ref = STREAM.get(str(ref).upper(), ref) if len(str(ref)) == 1 else ref
    rec_id, p = resolve(ref)
    return rec_id, p, LETTER.get(str(p.get("stream")), "?")


# ---------------------------------------------------------------- the checks (each: ctx -> Result)

def chk_config(ctx) -> Result:
    p = ctx["phone"]
    missing = [k for k in ("udid", "wda_port", "ssh") if not p.get(k)]
    return Result("config", FAIL if missing else OK,
                  f"{ctx['letter']} = {p.get('stream')} ({p.get('model', '?')}), ssh {p.get('ssh')}, wda :{p.get('wda_port')}"
                  + (f" -- missing {missing}" if missing else ""))


def chk_ssh(ctx) -> Result:
    ok, msg = C._ssh(ctx["rec"], "echo ok", timeout=25)          # _ssh() reports True only when stdout says ok
    return Result("ssh", OK if ok else FAIL, "reachable over WLAN (Bonjour name via the Mac)" if ok else (msg or "").strip()[:120])


def chk_wda(ctx) -> Result:
    try:
        p = connect(ctx["rec"], timeout=12)
        app = p.active_app()
        return Result("wda", OK, f"answers, foreground {app.get('bundleId', '?')}")
    except Exception as exc:                                   # noqa: BLE001
        return Result("wda", FAIL, f"{str(exc)[:140]} -- watchdog kicks it, else `launchctl kickstart -k gui/<uid>/com.veralta.wdaN` on the Mac")


def chk_xxtouch(ctx) -> Result:
    from adb_bot.automation import iphone_xxtouch
    p = ctx["phone"]
    if tuple(p.get("screen_px") or iphone_xxtouch.SCREEN_PX) != iphone_xxtouch.SCREEN_PX:
        return Result("xxtouch", INFO, f"{p.get('model')} has no XXTouch gesture library -- flows tap via WDA on this phone")
    try:
        code = iphone_xxtouch.Touch(ctx["rec"])._post("/spawn", b"sys.msleep(1)\n").get("code")
    except Exception as exc:                                   # noqa: BLE001
        return Result("xxtouch", FAIL, f"not reachable: {str(exc)[:120]}")
    if code == 0:
        return Result("xxtouch", OK, "licence valid, runs scripts")
    if code == 403:
        return Result("xxtouch", FAIL, "403 'requires a valid license' -- Havoc login on the phone, then XXTExplorer once")
    return Result("xxtouch", WARN, f"code {code}")


def chk_battery(ctx) -> Result:
    try:
        p = connect(ctx["rec"], timeout=12)
        b = p._req("GET", p._s("/wda/batteryInfo"))
        level, charging = int(round(b.get("level", 1) * 100)), b.get("state") in (2, 3)
    except Exception as exc:                                   # noqa: BLE001
        return Result("battery", WARN, f"could not read: {str(exc)[:80]}")
    if not charging and level <= 40:
        return Result("battery", FAIL if level <= 15 else WARN, f"{level} % and NOT charging -- cable/charger?")
    return Result("battery", OK, f"{level} %, {'charging' if charging else 'not charging (above 40 %, ChargeLimiter)'}")


def chk_free(ctx) -> Result:
    letter = ctx["letter"]
    if C.is_off(ctx["rec"]):
        return Result("free", WARN, "marked busy/off for the watchdog (a flow is driving it, or it was left set)")
    h = phone_lock.try_hold(letter)
    if h is None:
        return Result("free", FAIL, "phone lock held -- another flow (posting/signup/warm-up) is on this phone now")
    h.release()
    return Result("free", OK, "no other flow on the phone" + (" (signup is waiting for it)" if phone_lock.signup_wanted(letter) else ""))


def chk_vpn(ctx) -> Result:
    try:
        v = C.vpn_state(ctx["rec"])
    except Exception as exc:                                   # noqa: BLE001
        return Result("vpn", WARN, f"could not read: {str(exc)[:100]}")
    return Result("vpn", INFO, f"Shadowrocket {'ON' if v.get('vpn') else 'off'}, container {v.get('container') or '-'}")


def chk_shortcut(ctx) -> Result:
    """The container's Shortcut exists on the phone and is the current build (openapp Instagram with
    WFSelectedApp, or the SSH variant on iOS 15)."""
    cont = ctx.get("container")
    if not cont:
        return Result("shortcut", INFO, "no container given")
    from adb_bot.automation import iphone_farm_shortcuts as S
    import plistlib
    p = ctx["phone"]
    try:
        with tempfile.TemporaryDirectory() as d:
            db = S._pull(p.get("ssh") or p["ip"], d)
            con = sqlite3.connect(db)
            row = con.execute("select Z_PK from ZSHORTCUT where ZNAME=?", (cont,)).fetchone()
            if not row:
                names = [n for (n,) in con.execute("select ZNAME from ZSHORTCUT")]
                return Result("shortcut", FAIL, f"no Shortcut named {cont!r} on the phone ({len(names)} shortcuts: {', '.join(sorted(names)[:12])}...)")
            data = con.execute("select ZDATA from ZSHORTCUTACTIONS where ZSHORTCUT=?", (row[0],)).fetchone()
            actions = plistlib.loads(data[0]) if data and data[0] else []
            hosts = S.airtable_hosts()
            host = hosts.get(cont)
            if S._ios15(p):
                cur = S.is_current_ios15(actions, cont, host or "")
            else:
                cur = S.is_current(actions)
            exp = S.expected_host(actions)
            if host and exp and host != exp:
                return Result("shortcut", FAIL, f"Shortcut checks IP {exp}, Airtable proxy is {host} -- run iphone_farm_shortcuts sync")
            return Result("shortcut", OK if cur else WARN, f"present, {'current build' if cur else 'OLD build -- sync rewrites it'}, proxy {exp or host or '?'}")
    except Exception as exc:                                   # noqa: BLE001
        return Result("shortcut", WARN, f"could not read the Shortcuts DB: {str(exc)[:120]}")


def chk_airtable(ctx) -> Result:
    if not (os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY")):
        return Result("airtable", FAIL, "AIRTABLE_TOKEN not set -- `set -a; . /etc/adbbot/env`")
    cont = ctx.get("container")
    if not cont:
        return Result("airtable", OK, "token set")
    try:
        from adb_bot.automation.iphone_xx_post_batch import _account_row_id
        import requests
        row = _account_row_id(cont)
        if not row:
            return Result("airtable", FAIL, f"container {cont} has no account linked in Accounts SOP")
        acc = requests.get(f"https://api.airtable.com/v0/appwk9LrgAhJv8iEH/tbly0y1I4hNDlvh1K/{row}",
                           headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN') or os.environ.get('AIRTABLE_API_KEY')}"},
                           timeout=30).json().get("fields", {})
        ctx["account"] = acc
        ctx["row"] = row
        return Result("airtable", OK, f"@{acc.get('Username', '?')} status {acc.get('Status', '-')!r}, creation {acc.get('Creation Status', '-')!r}")
    except Exception as exc:                                   # noqa: BLE001
        return Result("airtable", FAIL, str(exc)[:140])


def chk_account_fields(ctx, flow: Flow) -> list[Result]:
    acc = ctx.get("account")
    if acc is None or not flow.account_needs:
        return []
    out = []
    for f in flow.account_needs:
        v = acc.get(f)
        out.append(Result(f"field:{f}", OK if v else FAIL, (str(v)[:80] if v else "empty on the Accounts row")))
    return out


def chk_account_live(ctx) -> Result:
    acc = ctx.get("account")
    if acc is None:
        return Result("account", INFO, "no account row loaded")
    st = acc.get("Status") or ""
    if st in ("Human Verification", "Completely Banned", "Banned", "Review", "logged out"):
        return Result("account", FAIL, f"status {st!r} -- nothing may run on it")
    return Result("account", OK, f"status {st or '-'!r}")


def chk_clips(ctx) -> Result:
    date, model = ctx.get("date") or time.strftime("%Y-%m-%d"), ctx.get("model") or "Nikki"
    root = Path(f"/opt/adbbot/manual_spoof/{date}/extra/{model}/DE")
    if not root.is_dir():
        return Result("clips", FAIL, f"{root} missing -- manual_spoof.py has not produced {model}'s clips for {date}")
    clips = sorted(p for p in root.iterdir() if p.is_dir())
    if not clips:
        return Result("clips", FAIL, f"{root} has no clip folders")
    limit = min(len(list(c.glob("*_x*.mp4"))) for c in clips)
    if limit == 0:
        return Result("clips", FAIL, f"{len(clips)} clips but a folder has 0 variants")
    cont = ctx.get("container")
    extra = ""
    if cont:
        try:
            from adb_bot.automation.iphone_xx_post_batch import variant_for
            extra = f", {cont} posts variant x{variant_for(model, cont, limit)}"
        except Exception as exc:                               # noqa: BLE001
            return Result("clips", FAIL, f"{len(clips)} clips x {limit} variants but no variant for {cont}: {exc}")
    return Result("clips", OK, f"{len(clips)} clips x {limit} variants for {model} {date}{extra}")


def chk_signup_budget(ctx) -> Result:
    from adb_bot.automation.iphone_signup import _attempts
    today = time.strftime("%Y-%m-%d")
    dev = _attempts().get(today, {}).get("devices", {}).get(ctx["rec"], {})
    if dev.get("rate_limited"):
        return Result("signup-budget", FAIL, f"Instagram said 'Try Again Later' on this phone today at {dev['rate_limited']}")
    n = dev.get("count") or dev.get("attempts") or 0
    if n:
        return Result("signup-budget", WARN, f"{n} signup attempt(s) on this phone today already -- Rene: one per phone per day")
    return Result("signup-budget", OK, "no signup on this phone today")


def chk_warmup_due(ctx) -> Result:
    acc = ctx.get("account")
    if acc is None:
        return Result("warmup-due", INFO, "no account row loaded")
    from adb_bot.automation.iphone_warmup import next_day
    day, why = next_day(acc)
    return Result("warmup-due", OK if day else WARN, f"day {day} due -- {why}" if day else why)


def chk_setup_due(ctx) -> Result:
    acc = ctx.get("account")
    if acc is None:
        return Result("setup-due", INFO, "no account row loaded")
    from adb_bot.automation.iphone_setup import needed, next_step
    step, why = next_step(acc, ctx.get("row") or "")
    need = needed(acc, ctx.get("row") or "")
    if not need:
        return Result("setup-due", OK, "nothing left to set up (or no material attached)")
    return Result("setup-due", OK if step else WARN, f"next: {step} -- {why}" if step else why)


def chk_planned(ctx) -> Result:
    return Result("planned", INFO, "flow not built yet -- the building blocks exist in iphone_ig (see FLOWS entry)")


CHECKS: dict[str, Callable] = {
    "config": chk_config, "ssh": chk_ssh, "wda": chk_wda, "battery": chk_battery, "xxtouch": chk_xxtouch, "free": chk_free, "vpn": chk_vpn,
    "shortcut": chk_shortcut, "airtable": chk_airtable, "account": chk_account_live, "clips": chk_clips,
    "signup-budget": chk_signup_budget, "warmup-due": chk_warmup_due, "setup-due": chk_setup_due, "planned": chk_planned,
}
PHONE_NEEDS = ["config", "ssh", "wda", "battery", "xxtouch", "free", "vpn"]


# ---------------------------------------------------------------- the registry

FLOWS: dict[str, Flow] = {
    "signup": Flow("signup", "Create an Instagram account", "ready", "adb_bot.automation.iphone_signup", "run_row(info)",
                   ".venv/bin/python -m adb_bot.automation.iphone_signup run <airtable_row_id>   (or the Tagesplan planner)",
                   PHONE_NEEDS + ["shortcut", "airtable", "signup-budget"],
                   ["Container", "Model auswählen (Pflicht)"]),
    "warmup": Flow("warmup", "Four warm-up days, one script per day", "ready", "adb_bot.automation.iphone_warmup",
                   "run_one(device, container, day) / tick()",
                   ".venv/bin/python -m adb_bot.automation.iphone_warmup due | run <device> <container> --day N | tick",
                   PHONE_NEEDS + ["shortcut", "airtable", "account", "warmup-due"],
                   ["Username", "Warm-up Start"]),
    "posting": Flow("posting", "Today's spoofed reels, batches per account", "ready", "adb_bot.automation.iphone_xx_post_batch",
                    "run_phone(letter, containers, clips, ...)",
                    ".venv/bin/python -m adb_bot.automation.iphone_xx_post_batch <YYYY-MM-DD> A1,A2 B1,B2 [--model Nikki] [--window 9-23]",
                    PHONE_NEEDS + ["shortcut", "airtable", "account", "clips"],
                    ["Username"]),
    "picture": Flow("picture", "Profile picture from the Airtable attachment", "ready", "adb_bot.automation.iphone_setup",
                    "run_step(device, container, 'picture') / tick()",
                    ".venv/bin/python -m adb_bot.automation.iphone_setup due | run <device> <container> --step picture --row <rec> | tick",
                    PHONE_NEEDS + ["shortcut", "airtable", "account", "setup-due"],
                    ["Username", "Profile Picture"]),
    "bio": Flow("bio", "Bio Text from the Accounts row (empty -> default 'schau mal da unten'-style text)", "ready", "adb_bot.automation.iphone_setup",
                "run_step(device, container, 'bio')",
                ".venv/bin/python -m adb_bot.automation.iphone_setup run <device> <container> --step bio --row <rec>",
                PHONE_NEEDS + ["shortcut", "airtable", "account", "setup-due"],
                ["Username"]),
    "highlight": Flow("highlight", "Story with the OF link sticker, saved into a highlight", "ready", "adb_bot.automation.iphone_setup",
                      "run_step(device, container, 'highlight')",
                      ".venv/bin/python -m adb_bot.automation.iphone_setup run <device> <container> --step highlight --row <rec>",
                      PHONE_NEEDS + ["shortcut", "airtable", "account", "setup-due"],
                      ["Username", "Story Highlight Picture", "Funnel Link"]),
    "story": Flow("story", "A plain story post (no highlight) -- not built yet", "planned", "adb_bot.automation.iphone_ig",
                  "create_story(p, link=...) + media_to_photos()",
                  "(planned)",
                  PHONE_NEEDS + ["shortcut", "airtable", "account", "planned"],
                  ["Username"]),
}


def check(flow_name: str, ref: str, container: str | None = None, **ctx_extra) -> list[Result]:
    """Run a flow's needs against a phone (+ container). Never raises on a failed need; a thrown check is a FAIL."""
    flow = FLOWS[flow_name]
    rec, p, letter = phone(ref)
    ctx = {"rec": rec, "phone": p, "letter": letter, "container": container, **ctx_extra}
    out: list[Result] = []
    for need in flow.needs:
        try:
            out.append(CHECKS[need](ctx))
        except Exception as exc:                               # noqa: BLE001
            out.append(Result(need, FAIL, f"check crashed: {type(exc).__name__}: {str(exc)[:120]}"))
        if need == "airtable":
            out += chk_account_fields(ctx, flow)
    return out


def ready(results: list[Result]) -> bool:
    return not any(r.state == FAIL for r in results)


def _print(results: list[Result]) -> None:
    mark = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", INFO: "ℹ️ "}
    for r in results:
        print(f"  {mark[r.state]} {r.need:28} {r.detail}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("check")
    s.add_argument("flow", choices=sorted(FLOWS))
    s.add_argument("phone", help="letter (H), stream (iphone8) or record id")
    s.add_argument("container", nargs="?")
    s.add_argument("--date", default=None, help="posting: clip date (default today)")
    s.add_argument("--model", default="Nikki", help="posting: whose clips")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("check-all")
    s.add_argument("phone")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for f in FLOWS.values():
            print(f"{f.name:10} {f.status:8} {f.title}\n{'':10} module {f.module}  entry {f.entry}\n{'':10} {f.cli}\n"
                  f"{'':10} needs: {', '.join(f.needs)}" + (f"; account fields: {', '.join(f.account_needs)}" if f.account_needs else ""))
        return 0
    if a.cmd == "check-all":
        rec, p, letter = phone(a.phone)
        ctx = {"rec": rec, "phone": p, "letter": letter, "container": None}
        res = [CHECKS[n](ctx) for n in PHONE_NEEDS]
        print(f"{letter} ({p.get('stream')}):")
        _print(res)
        return 0 if ready(res) else 1
    res = check(a.flow, a.phone, a.container, date=a.date, model=a.model)
    if a.json:
        print(json.dumps([dataclasses.asdict(r) for r in res], indent=1, ensure_ascii=False))
    else:
        f = FLOWS[a.flow]
        print(f"{a.flow} ({f.status}) on {a.phone}{'/' + a.container if a.container else ''}:")
        _print(res)
        print("  -> READY" if ready(res) else "  -> NOT READY (fix the ❌ lines first)")
    return 0 if ready(res) else 1


# ---------------------------------------------------------------- re-exports (grep lands here, one hop to the module)

from adb_bot.automation.iphone_signup import run_row as iphone_signup_run_row, ready_rows as iphone_signup_ready_rows   # noqa: E402,F401
from adb_bot.automation.iphone_xx_post_batch import run_phone as iphone_posting_run_phone, main as iphone_posting_main  # noqa: E402,F401
from adb_bot.automation.iphone_warmup import run_one as iphone_warmup_run_one, tick as iphone_warmup_tick           # noqa: E402,F401
from adb_bot.automation.iphone_ig import (create_story as iphone_story_create, set_bio as iphone_bio_set,              # noqa: E402,F401
                                          save_story_to_highlight as iphone_highlight_save, open_account as iphone_open_account)
from adb_bot.automation.iphone_diag import keep_screen as iphone_keep_screen                                          # noqa: E402,F401
from adb_bot.automation.iphone_farm_fastopen import open_container as iphone_open_container                           # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
