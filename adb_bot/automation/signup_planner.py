"""Tagesplan -> iPhone signups at the times Rene enters (10.10, Rene: "ich sage die Zeit selber ein",
"pro Handy am Tag bis zu 10 Accounts", "auf jedem Handy gleich viel, egal wie viel gepostet wird",
"lieber ein bisschen versetzt, ungefähr gleicher Abstand, aber etwas unterschiedlich").

Airtable table "Tagesplan" (tblSMaEQittuzIOvg): one row = one order -- Datum, Model, Aufgabe (Account
Creation), Signup-Methode (Icloud / SMS-Pool), Uhrzeiten (one HH:MM per line), Usernames (optional, one per
line), Handy (optional; empty = every phone), Aktiv tick.

EVERY entered time means ONE account on EVERY phone of the plan. The phones do not start together: per time
the order of the phones is shuffled and they start SIGNUP_STAGGER_MIN minutes apart (random, default 3-8),
seeded by plan + day + time so the schedule stays the same from pass to pass. Phones run in parallel, one
signup per phone at a time. A phone that is posting finishes its current post + scroll and then pauses for
the signup (iphone_phone_lock); posting never delays a signup by more than that.

Each signup runs as its own transient systemd unit (`adbbot-signup-<phone>-...`, this module with --worker):
it takes a free container on its phone, fills a prepared empty To-Do row in Account Creation (else a new
one: Model, Signup Method, Username, container, a free Mail-Alias for Icloud), waits for the phone, runs
`iphone_signup run <row>` and writes the result back. The timer pass (every 2 min, polling only) starts the
workers whose time has come and refreshes Plan / Status / Fortschritt.

Limits: IPHONE_SIGNUP_DEVICE_PER_DAY (10) per phone per day, counted here and in iphone_signup.check_budget
(which also blocks a phone for the day after 'Try Again Later'). No retries of failed slots. The farm
watchdog timer is paused while any signup runs (its video restart throws WDA out mid-signup, 09.10).

    .venv/bin/python -m adb_bot.automation.signup_planner             # one pass (adbbot-signup-planner.timer)
    .venv/bin/python -m adb_bot.automation.signup_planner --dry-run   # today's schedule, change nothing
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import requests

from adb_bot.automation import iphone_phone_lock as phone_lock

log = logging.getLogger("signup_planner")

BASE = "appwk9LrgAhJv8iEH"
BERLIN = ZoneInfo("Europe/Berlin")
T_PLAN, T_ACC, T_CONT, T_ALIAS, T_MODELS, T_DEV = ("tblSMaEQittuzIOvg", "tbly0y1I4hNDlvh1K", "tblyZXN7caKZZBaBj",
                                                   "tblodjIl9a7B7LvHa", "tblMkfEq3cEnXuZs5", "tbl6OZrzFuns7rux6")
P = {"name": "fldLb5x0NOhWQ1vXv", "date": "fldNm8tnuTEfjuPWm", "model": "fldRvYNjPXdbM382y", "task": "fldLlUvZudiavthGO",
     "method": "fldT6QHUhnP6HCHZb", "times": "fldnmCuRNzXJwfIZw", "usernames": "fldlq5JApQMBn77Np", "phones": "fldqtxM6hHJl5UJ5W",
     "active": "fld0Y0lsj4eNyR4Vz", "status": "fldlVQmBcI37groGF", "progress": "fld5Gdy2y9QV7SsIG", "accounts": "fld6Ryh91AY7HsoDL"}
A = {"username": "fld6qQqvc3xZVu93Q", "creation": "fld8vhG5DGnsiJafk", "model": "fldWqrNcLsbogxphV", "alias": "fldGn90iK5lBytleM",
     "container": "fldMiw9AuAg8wUEzd", "method": "fldPmxXuITL6TNk3f"}
C = {"name": "fldcBLkxaDrsZIht0", "device": "fldck8WL7t2Q17kUT", "status": "fld23I43IfgL6y2em", "accounts": "fld570bl55cPBlWWX"}
AL = {"addr": "fld0WY7btzpCTK3xR", "status": "fldH3hXRrOAjPQvXq"}
D = {"name": "flduRcXBIbLNd9WPP", "type": "fldFV8EPxtC3tQkdq"}
M_NAME = "fld9FFX1ohatkPIev"
TASK_CREATION = "Account Creation"
METHOD_ICLOUD, METHOD_SMS = "Icloud", "SMS-Pool / Virtual Number"

ADB_BOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.join(ADB_BOT, ".venv", "bin", "python")
HOME = os.path.join(os.path.expanduser("~"), ".adb_bot")
STATE_PATH = os.path.join(HOME, "signup_planner.json")
ATTEMPTS_PATH = os.path.join(HOME, "iphone_signup_attempts.json")       # written by iphone_signup
WATCHDOG_TIMER = "adbbot-iphone-farm-watchdog.timer"
WATCHDOG_MARK = os.path.join(HOME, "signup_planner.watchdog-paused")
ROWS_LOCK = os.path.join(phone_lock.DIR, "planner-rows.lock")
SIGNUP_TIMEOUT_S = int(os.environ.get("SIGNUP_PLANNER_TIMEOUT_S", "2700"))
CAP = int(os.environ.get("IPHONE_SIGNUP_DEVICE_PER_DAY", "10"))
ACTIVE = ("starting", "waiting", "running")          # a worker owns the slot
FINISHED = ("done", "failed")


def _stagger() -> tuple[float, float]:
    lo, _, hi = os.environ.get("SIGNUP_STAGGER_MIN", "3-8").partition("-")
    return float(lo), float(hi or lo)


# ---------------------------------------------------------------- Airtable

def _hdr() -> dict:
    tok = os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY") or ""
    if not tok:
        raise RuntimeError("AIRTABLE_TOKEN is not set (source /etc/adbbot/env)")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _list(table: str, formula: str = "") -> list[dict]:
    out, offset = [], None
    while True:
        params = {"returnFieldsByFieldId": "true"}
        if formula:
            params["filterByFormula"] = formula
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}", headers=_hdr(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out


def _get(table: str, rid: str) -> dict:
    r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}/{rid}", headers=_hdr(), params={"returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _patch(table: str, rid: str, fields: dict) -> None:
    r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{table}/{rid}", headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=30)
    r.raise_for_status()


def _create(table: str, fields: dict) -> str:
    r = requests.post(f"https://api.airtable.com/v0/{BASE}/{table}", headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def _text(v) -> str:
    if isinstance(v, dict):
        v = v.get("name", "")
    return str(v or "").strip()


# ---------------------------------------------------------------- state (shared by the pass and the workers)

def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def state_txn(write: bool = True):
    """Read-modify-write of the state file under an exclusive lock (write=False: read under the lock only)."""
    os.makedirs(HOME, exist_ok=True)
    with open(STATE_PATH + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        state = _load(STATE_PATH)
        yield state
        if not write:
            return
        cutoff = (dt.datetime.now(BERLIN).date() - dt.timedelta(days=7)).isoformat()
        state = {k: v for k, v in state.items() if v.get("date", "9999") >= cutoff}
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)


def set_slot(plan_id: str, t: str, dev: str, **fields) -> None:
    with state_txn() as st:
        plan = st.setdefault(plan_id, {"date": dt.datetime.now(BERLIN).date().isoformat(), "slots": {}})
        plan["slots"].setdefault(t, {}).setdefault(dev, {}).update(fields)


# ---------------------------------------------------------------- plans, phones, schedule

def parse_times(text: str) -> list[str]:
    """'10:00\\n13:30' (also '10.00', '9:5', commas) -> ['10:00', '13:30'], sorted, unique."""
    out = set()
    for tok in re.split(r"[\s,;]+", text or ""):
        m = re.fullmatch(r"(\d{1,2})[:.](\d{1,2})", tok.strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.add(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
    return sorted(out)


def todays_plans(today: dt.date) -> list[dict]:
    return [r for r in _list(T_PLAN, f"{{{P['active']}}} = 1")
            if _text(r["fields"].get(P["date"])) == today.isoformat() and _text(r["fields"].get(P["task"])) == TASK_CREATION]


def phones() -> dict[str, dict]:
    """{device record id: {'name': 'iPhone D', 'letter': 'D'}} of every physical iPhone."""
    out = {}
    for d in _list(T_DEV):
        f = d["fields"]
        name = _text(f.get(D["name"]))
        if "physical" in _text(f.get(D["type"])).lower() and name:
            out[d["id"]] = {"name": name, "letter": name.split()[-1][:1].upper()}
    return out


def plan_phones(plan: dict, all_phones: dict) -> list[str]:
    chosen = [p for p in (plan["fields"].get(P["phones"]) or []) if p in all_phones]
    return chosen or sorted(all_phones)


def schedule(plan_id: str, t: str, devs: list[str], day: dt.date) -> list[tuple[str, dt.datetime]]:
    """[(device, start)] for one entered time: phones shuffled, 3-8 min apart (random, but the same every pass)."""
    rng = random.Random(f"{plan_id}|{day.isoformat()}|{t}")
    order = sorted(devs)
    rng.shuffle(order)
    lo, hi = _stagger()
    h, m = map(int, t.split(":"))
    at = dt.datetime.combine(day, dt.time(h, m), tzinfo=BERLIN) + dt.timedelta(minutes=rng.uniform(0, min(lo, 2)))
    out = []
    for d in order:
        out.append((d, at))
        at += dt.timedelta(minutes=rng.uniform(lo, hi))
    return out


def model_name(model_id: str, cache: dict) -> str:
    if model_id not in cache:
        cache[model_id] = _text(_get(T_MODELS, model_id)["fields"].get(M_NAME))
    return cache[model_id]


# ---------------------------------------------------------------- processes

def _cmdlines(pattern: str) -> list[list[str]]:
    """argv of every python process whose command line contains `pattern` (never this process)."""
    me, out = os.getpid(), []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
        except OSError:
            continue
        if argv and "python" in os.path.basename(argv[0]) and pattern in " ".join(argv):
            out.append(argv)
    return out


def running_workers() -> set[tuple[str, str, str]]:
    """{(plan, time, device)} of the workers alive right now."""
    out = set()
    for argv in _cmdlines("adb_bot.automation.signup_planner"):
        if "--worker" in argv:
            i = argv.index("--worker")
            if len(argv) >= i + 4:
                out.add((argv[i + 1], argv[i + 2], argv[i + 3]))
    return out


def spawn_worker(plan_id: str, t: str, dev: str, letter: str) -> str:
    """Own transient unit per signup: it outlives this pass and logs to `journalctl -u <unit>`."""
    unit = f"adbbot-signup-{letter}-{t.replace(':', '')}-{plan_id[-5:]}-{int(time.time()) % 100000}"
    subprocess.run(["systemd-run", "--no-block", "--collect", f"--unit={unit}", f"--working-directory={ADB_BOT}",
                    "-p", "EnvironmentFile=-/etc/adbbot/env", "-p", "RuntimeMaxSec=14400",
                    PYTHON, "-m", "adb_bot.automation.signup_planner", "--worker", plan_id, t, dev],
                   check=True, capture_output=True, text=True)
    return unit


def _watchdog(action: str) -> bool:
    try:
        active = subprocess.run(["systemctl", "is-active", WATCHDOG_TIMER], capture_output=True, text=True).stdout.strip() == "active"
        if action == "stop" and active:
            subprocess.run(["systemctl", "stop", WATCHDOG_TIMER], check=False)
        elif action == "start":
            subprocess.run(["systemctl", "start", WATCHDOG_TIMER], check=False)
        return active
    except OSError:
        return False


def pause_watchdog() -> None:
    os.makedirs(HOME, exist_ok=True)
    if _watchdog("stop"):
        open(WATCHDOG_MARK, "w").close()


def restore_watchdog() -> None:
    """Start the watchdog timer again once no signup runs any more (workers only ever pause it)."""
    if os.path.exists(WATCHDOG_MARK) and not running_workers() and not _cmdlines("adb_bot.automation.iphone_signup run"):
        _watchdog("start")
        os.remove(WATCHDOG_MARK)
        log.info("watchdog timer started again (no signup running)")


# ---------------------------------------------------------------- the worker: one signup on one phone

def free_container_on(dev: str) -> dict | None:
    for c in sorted(_list(T_CONT, f"{{{C['status']}}} = 'Frei'"), key=lambda c: _text(c["fields"].get(C["name"]))):
        f = c["fields"]
        if (f.get(C["device"]) or [None])[0] == dev and not f.get(C["accounts"]):
            return {"id": c["id"], "name": _text(f.get(C["name"]))}
    return None


def account_row(method: str) -> str | None:
    """An empty To-Do row prepared in Account Creation (no username, container, model): for Icloud one that
    already holds a Mail-Alias, for SMS-Pool one without. None = create a new row."""
    for r in _list(T_ACC, f"AND({{{A['creation']}}} = 'To Do', {{{A['username']}}} = '', {{{A['container']}}} = '', {{{A['model']}}} = '')"):
        if bool(r["fields"].get(A["alias"])) == (method == METHOD_ICLOUD):
            return r["id"]
    return None


def claim_row(plan_id: str, dev: str) -> dict:
    """Under a farm-wide lock (workers run in parallel): container, account row, alias, username -> written."""
    os.makedirs(phone_lock.DIR, exist_ok=True)
    with open(ROWS_LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        f = _get(T_PLAN, plan_id)["fields"]              # fresh: the accounts other workers linked meanwhile
        model_id = (f.get(P["model"]) or [None])[0]
        if not model_id:
            raise ValueError("kein Model im Plan")
        method = _text(f.get(P["method"])) or METHOD_ICLOUD
        cont = free_container_on(dev)
        if not cont:
            raise ValueError("kein freier Container auf diesem Handy")
        linked = f.get(P["accounts"]) or []
        names = [u.strip().lstrip("@") for u in _text(f.get(P["usernames"])).splitlines() if u.strip()]
        username = names[len(linked)] if len(linked) < len(names) else ""
        row = account_row(method)
        fields = {A["model"]: [model_id], A["container"]: [cont["id"]], A["method"]: method, A["creation"]: "To Do"}
        if username:
            fields[A["username"]] = username
        if method == METHOD_ICLOUD and row is None:
            rows = _list(T_ALIAS, f"{{{AL['status']}}} = 'Free'")
            if not rows:
                raise ValueError("kein freier Mail-Alias mehr (Mail Aliases)")
            fields[A["alias"]] = [rows[0]["id"]]
            _patch(T_ALIAS, rows[0]["id"], {AL["status"]: "Used"})   # claimed at once: iphone_signup's add-email step takes the first Free one too
        if row is None:
            row = _create(T_ACC, fields)
        else:
            _patch(T_ACC, row, fields)
        _patch(T_PLAN, plan_id, {P["accounts"]: linked + [row]})
        return {"row": row, "container": cont["name"]}


def outcome(row: str, code: int, tail: str) -> tuple[str, str]:
    f = _get(T_ACC, row)["fields"]
    status, user = _text(f.get(A["creation"])), _text(f.get(A["username"]))
    if status == "Instagram Created":
        return "done", f"✅ @{user}"
    if status == "Stuck / Needs Human":
        return "done", f"🔐 @{user} Verifizierung lösen"
    return "failed", f"❌ {tail or f'Exit {code}'}"


def worker(plan_id: str, t: str, dev: str) -> int:
    ph = phones().get(dev, {"letter": "?", "name": dev})
    letter = ph["letter"]
    try:
        claim = claim_row(plan_id, dev)
    except Exception as exc:  # noqa: BLE001
        set_slot(plan_id, t, dev, state="failed", text=f"❌ {exc}")
        log.warning("%s %s %s: %s", plan_id, t, ph["name"], exc)
        return 1
    set_slot(plan_id, t, dev, state="waiting", wait="aktueller Post wird fertig gemacht", **claim)
    phone_lock.want(letter, f"signup {t}")
    held = phone_lock.hold(letter, timeout_s=7200)
    try:
        if held is None:
            set_slot(plan_id, t, dev, state="failed", text=f"❌ {ph['name']} war 2 Stunden belegt")
            return 1
        set_slot(plan_id, t, dev, state="running", at=dt.datetime.now(BERLIN).strftime("%H:%M"))
        pause_watchdog()
        log.info("%s %s %s: signup %s in %s", plan_id, t, ph["name"], claim["row"], claim["container"])
        try:
            proc = subprocess.run([PYTHON, "-m", "adb_bot.automation.iphone_signup", "run", claim["row"]], cwd=ADB_BOT,
                                  capture_output=True, text=True, timeout=SIGNUP_TIMEOUT_S)
            out = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
            code, tail = proc.returncode, (out[-1] if out else "")[:200]
        except subprocess.TimeoutExpired:
            code, tail = 124, f"Signup nach {SIGNUP_TIMEOUT_S // 60} min abgebrochen"
        st, text = outcome(claim["row"], code, tail)
        set_slot(plan_id, t, dev, state=st, text=text)
        log.info("%s %s %s: %s (exit %s)", plan_id, t, ph["name"], text, code)
        return 0
    finally:
        if held:
            held.release()
        phone_lock.unwant(letter)


# ---------------------------------------------------------------- display

def render(plan: dict, slots: dict, all_phones: dict, models: dict, today: dt.date) -> tuple[str, str, str]:
    f = plan["fields"]
    times = parse_times(_text(f.get(P["times"])))
    devs = plan_phones(plan, all_phones)
    model = model_name(f[P["model"]][0], models) if f.get(P["model"]) else "?"
    name = f"{model} · {today.strftime('%d.%m.')} · {_text(f.get(P['task']))} · {len(times)} × {len(devs)} Handys"
    lines, states = [], []
    for t in times:
        parts = []
        for dev, start in schedule(plan["id"], t, devs, today):
            s = slots.get(t, {}).get(dev, {})
            st = s.get("state", "scheduled")
            states.append(st)
            L = all_phones.get(dev, {}).get("letter", "?")
            if st in FINISHED:
                parts.append(f"{L} {s.get('text', '')}" + (f" ({s['container']})" if s.get("container") else ""))
            elif st == "running":
                parts.append(f"{L} ⏳ läuft seit {s.get('at', '?')}")
            elif st in ("waiting", "starting"):
                parts.append(f"{L} ⏳ {s.get('wait') or 'startet'}")
            elif s.get("wait"):
                parts.append(f"{L} wartet ({s['wait']})")
            else:
                parts.append(f"{L} {start:%H:%M}")
        lines.append(f"{t} · " + " · ".join(parts))
    if not times:
        lines.append("❌ keine Uhrzeit eingetragen (eine pro Zeile, z. B. 10:00)")
    if not states:
        status = "Fehler"
    elif any(s in ACTIVE for s in states):
        status = "Läuft"
    elif all(s in FINISHED for s in states):
        status = "Fertig"
    else:
        status = "Wartet"
    return name, status, "\n".join(lines)


# ---------------------------------------------------------------- the pass

def plan_pass(st: dict, plans: list[dict], all_phones: dict, now: dt.datetime, dry_run: bool) -> None:
    """Close the slots whose worker died, start the workers whose time has come (mutates `st`)."""
    today = now.date()
    alive = running_workers()
    busy = {dev for (_, _, dev) in alive}
    started: dict[str, int] = {}
    for pv in st.values():
        if pv.get("date") == today.isoformat():
            for tv in pv.get("slots", {}).values():
                for dev, s in tv.items():
                    if s.get("state") in ACTIVE + FINISHED and (s.get("row") or s.get("state") in ACTIVE):
                        started[dev] = started.get(dev, 0) + 1
    manual = _load(ATTEMPTS_PATH).get(today.isoformat(), {}).get("devices", {})
    due = []
    for plan in plans:
        ps = st.setdefault(plan["id"], {"date": today.isoformat(), "slots": {}})
        for t in parse_times(_text(plan["fields"].get(P["times"]))):
            for dev, start in schedule(plan["id"], t, plan_phones(plan, all_phones), today):
                s = ps["slots"].setdefault(t, {}).setdefault(dev, {})
                mine = (plan["id"], t, dev) in alive
                if s.get("state") in ("waiting", "running") and not mine:
                    # the worker is gone without writing a result -- read what the signup left on the row
                    try:
                        res = outcome(s["row"], -1, "Worker wurde unterbrochen") if s.get("row") else ("failed", "❌ Worker wurde unterbrochen")
                    except Exception as exc:  # noqa: BLE001
                        res = ("failed", f"❌ Worker unterbrochen ({exc})")
                    s.update(state=res[0], text=res[1])
                elif s.get("state") == "starting" and not mine and time.time() - s.get("spawned", 0) > 180:
                    s.update(state="failed", text="❌ Worker ist nicht gestartet")
                if s.get("state") in (None, "pending") and start <= now:
                    due.append((start, plan, t, dev, s))
    due.sort(key=lambda d: d[0])
    for start, plan, t, dev, s in due:
        ph = all_phones.get(dev, {"letter": "?", "name": dev})
        if manual.get(dev, {}).get("rate_limited"):
            s.update(state="failed", text=f"❌ {ph['name']} hat heute 'Try Again Later' gemeldet")
            continue
        if max(started.get(dev, 0), manual.get(dev, {}).get("attempts", 0)) >= CAP:
            s.update(state="failed", text=f"❌ Tageslimit ({CAP}) auf {ph['name']} erreicht")
            continue
        if dev in busy:
            s.update(state="pending", wait="vorheriger Signup auf diesem Handy läuft noch")
            continue
        if dry_run:
            print(f"would start {t} ({start:%H:%M}) on {ph['name']} for plan {plan['id']}")
            busy.add(dev)
            continue
        unit = spawn_worker(plan["id"], t, dev, ph["letter"])
        s.clear()
        s.update(state="starting", unit=unit, spawned=time.time())
        busy.add(dev)
        started[dev] = started.get(dev, 0) + 1
        log.info("started %s: %s %s on %s", unit, plan["id"], t, ph["name"])


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--worker", nargs=3, metavar=("PLAN", "TIME", "DEVICE"))
    a = ap.parse_args(argv)
    if a.worker:
        return worker(*a.worker)
    now = dt.datetime.now(BERLIN)
    if not a.dry_run:
        restore_watchdog()
    plans = todays_plans(now.date())
    if not plans:
        log.info("no active plan for %s", now.date().isoformat())
        return 0
    all_phones = phones()
    with state_txn(write=not a.dry_run) as st:
        plan_pass(st, plans, all_phones, now, a.dry_run)
        snapshot = json.loads(json.dumps(st))
    models: dict = {}
    for plan in plans:
        try:
            name, status, text = render(plan, snapshot.get(plan["id"], {}).get("slots", {}), all_phones, models, now.date())
            if a.dry_run:
                print(f"--- {name} [{status}]\n{text}")
                continue
            f = plan["fields"]
            if (_text(f.get(P["name"])), _text(f.get(P["status"])), _text(f.get(P["progress"]))) != (name, status, text):
                _patch(T_PLAN, plan["id"], {P["name"]: name, P["status"]: status, P["progress"]: text})
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh %s: %s", plan["id"], exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
