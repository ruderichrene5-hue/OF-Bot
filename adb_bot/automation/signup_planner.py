"""Tagesplan -> iPhone signups at the times Rene enters (10.10, Rene: "ich sage die Zeit selber ein",
"pro Handy am Tag bis zu 10 Accounts", "damit der Bot genau weiß, was er machen muss").

Airtable table "Tagesplan" (tblSMaEQittuzIOvg): one row = one order -- Datum, Model, Aufgabe (Account
Creation), Signup-Methode (Icloud / SMS-Pool), Uhrzeiten (one HH:MM per line = one account), Usernames
(optional, one per line in the same order), Handy (optional), Aktiv tick. Every run of this module:

  1. refreshes Plan / Status / Fortschritt of every active plan of today (Berlin time);
  2. takes the EARLIEST due time slot (time <= now, not started yet) of all plans and, unless another
     signup is running, picks a free container (idle phones first; a phone that is posting is fine too)
     and asks for the phone: a posting batch there finishes its current post + scroll and pauses before
     its next post / Shortcut (iphone_phone_lock). Then it prepares one Accounts SOP row (reuses a prepared
     empty To-Do row, else creates one; Model, Signup Method, Username, container, a free Mail-Alias for
     Icloud) and runs `iphone_signup run <row>`; posting goes on right after;
  3. writes the result back into the slot (✅ @username / 🔐 verification / ❌ reason).

It starts nothing on its own schedule -- only the entered times. One signup per run; the systemd timer only
polls. The per-account / per-phone limits (1 attempt per account per day, IPHONE_SIGNUP_DEVICE_PER_DAY,
'Try Again Later' blocks the phone for the day) stay where they are, in iphone_signup.check_budget; this
module only skips phones that already hit them. The farm watchdog timer is paused while a signup runs
(its video restart throws WDA out mid-signup, 09.10).

    .venv/bin/python -m adb_bot.automation.signup_planner             # one pass (adbbot-signup-planner.timer)
    .venv/bin/python -m adb_bot.automation.signup_planner --dry-run   # show what it would do, change nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

import requests

from adb_bot.automation import iphone_phone_lock as phone_lock

log = logging.getLogger("signup_planner")

BASE = "appwk9LrgAhJv8iEH"
BERLIN = ZoneInfo("Europe/Berlin")
T_PLAN, T_ACC, T_CONT, T_ALIAS, T_MODELS = "tblSMaEQittuzIOvg", "tbly0y1I4hNDlvh1K", "tblyZXN7caKZZBaBj", "tblodjIl9a7B7LvHa", "tblMkfEq3cEnXuZs5"
P = {"name": "fldLb5x0NOhWQ1vXv", "date": "fldNm8tnuTEfjuPWm", "model": "fldRvYNjPXdbM382y", "task": "fldLlUvZudiavthGO",
     "method": "fldT6QHUhnP6HCHZb", "times": "fldnmCuRNzXJwfIZw", "usernames": "fldlq5JApQMBn77Np", "phones": "fldqtxM6hHJl5UJ5W",
     "active": "fld0Y0lsj4eNyR4Vz", "status": "fldlVQmBcI37groGF", "progress": "fld5Gdy2y9QV7SsIG", "accounts": "fld6Ryh91AY7HsoDL"}
A = {"username": "fld6qQqvc3xZVu93Q", "creation": "fld8vhG5DGnsiJafk", "model": "fldWqrNcLsbogxphV", "alias": "fldGn90iK5lBytleM",
     "container": "fldMiw9AuAg8wUEzd", "method": "fldPmxXuITL6TNk3f", "email": "flde10adBpXhBl2Hn"}
C = {"name": "fldcBLkxaDrsZIht0", "device": "fldck8WL7t2Q17kUT", "status": "fld23I43IfgL6y2em", "accounts": "fld570bl55cPBlWWX", "phone": "fldBwhjXHRsH01c2H"}
AL = {"addr": "fld0WY7btzpCTK3xR", "status": "fldH3hXRrOAjPQvXq"}
M_NAME = "fld9FFX1ohatkPIev"
TASK_CREATION = "Account Creation"
METHOD_ICLOUD, METHOD_SMS = "Icloud", "SMS-Pool / Virtual Number"

ADB_BOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.join(ADB_BOT, ".venv", "bin", "python")
STATE_PATH = os.path.join(os.path.expanduser("~"), ".adb_bot", "signup_planner.json")
ATTEMPTS_PATH = os.path.join(os.path.expanduser("~"), ".adb_bot", "iphone_signup_attempts.json")   # written by iphone_signup
WATCHDOG_TIMER = "adbbot-iphone-farm-watchdog.timer"
WATCHDOG_MARK = os.path.join(os.path.expanduser("~"), ".adb_bot", "signup_planner.watchdog-paused")
SIGNUP_TIMEOUT_S = int(os.environ.get("SIGNUP_PLANNER_TIMEOUT_S", "2700"))


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


# ---------------------------------------------------------------- state

def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    cutoff = (dt.datetime.now(BERLIN).date() - dt.timedelta(days=7)).isoformat()
    state = {k: v for k, v in state.items() if v.get("date", "9999") >= cutoff}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- plans

def parse_times(text: str) -> list[str]:
    """'10:00\\n13:30' (also '10.00', '9:5', commas) -> ['10:00', '13:30'], sorted, unique."""
    out = set()
    for tok in re.split(r"[\s,;]+", text or ""):
        m = re.fullmatch(r"(\d{1,2})[:.](\d{1,2})", tok.strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.add(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
    return sorted(out)


def todays_plans(today: dt.date) -> list[dict]:
    rows = _list(T_PLAN, f"{{{P['active']}}} = 1")
    out = []
    for r in rows:
        f = r["fields"]
        if _text(f.get(P["date"])) != today.isoformat():
            continue
        if _text(f.get(P["task"])) != TASK_CREATION:
            continue
        out.append(r)
    return out


def model_name(model_id: str, cache: dict) -> str:
    if model_id not in cache:
        cache[model_id] = _text(_get(T_MODELS, model_id)["fields"].get(M_NAME))
    return cache[model_id]


# ---------------------------------------------------------------- farm guards

def _running(pattern: str) -> bool:
    """A process whose argv contains `pattern` (reads /proc, so this checker never matches itself)."""
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        if argv and b"python" in os.path.basename(argv[0]) and pattern.encode() in b" ".join(argv):
            return True
    return False


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


def posting_containers() -> set[str]:
    """Containers of every running iphone_xx_post_batch (its positional args: date, then comma lists per
    phone, e.g. `2026-10-10 A1,A2 B1,B2`). The batch runs all day and rests 2-4 h between batches, so it
    is the PHONES it posts on that are off-limits for signups -- not the whole farm (10.10)."""
    names = set()
    for argv in _cmdlines("iphone_xx_post_batch"):
        args = argv[argv.index("adb_bot.automation.iphone_xx_post_batch") + 1:] if "adb_bot.automation.iphone_xx_post_batch" in argv else argv[1:]
        for a in args[1:]:
            if a.startswith("-"):
                break
            names.update(x.strip() for x in a.split(",") if x.strip())
    return names


def busy_phones() -> set[str]:
    """Device record ids that a running posting batch uses."""
    conts = posting_containers()
    if not conts:
        return set()
    out = set()
    for c in _list(T_CONT):
        if _text(c["fields"].get(C["name"])) in conts:
            out.update(c["fields"].get(C["device"]) or [])
    return out


def farm_busy() -> str | None:
    if _running("adb_bot.automation.iphone_signup run"):
        return "ein anderer Signup läuft gerade"
    return None


def phone_load(today: dt.date) -> dict:
    """{device record id: {'attempts': n, 'rate_limited': 'HH:MM'|None}} from iphone_signup's own budget file."""
    day = _load(ATTEMPTS_PATH).get(today.isoformat(), {})
    return day.get("devices", {})


def free_container(allowed_phones: list[str], today: dt.date) -> dict | None:
    """A free container (Status Frei, no account linked) on an allowed phone that may still sign up today;
    the phone with the fewest signups today first."""
    cap = int(os.environ.get("IPHONE_SIGNUP_DEVICE_PER_DAY", "10"))
    load = phone_load(today)
    posting = busy_phones()          # phones a running posting batch uses: allowed (it yields between posts), idle ones first
    best = None
    for c in _list(T_CONT, f"{{{C['status']}}} = 'Frei'"):
        f = c["fields"]
        dev = (f.get(C["device"]) or [None])[0]
        if not dev or f.get(C["accounts"]):
            continue
        if allowed_phones and dev not in allowed_phones:
            continue
        info = load.get(dev, {})
        if info.get("rate_limited") or info.get("attempts", 0) >= cap:
            continue
        key = (dev in posting, info.get("attempts", 0), _text(f.get(C["name"])))
        if best is None or key < best[0]:
            best = (key, {"id": c["id"], "name": _text(f.get(C["name"])), "device": dev, "phone": _text(f.get(C["phone"]))})
    return best[1] if best else None


def account_row(model_id: str, method: str) -> str | None:
    """An empty To-Do row prepared in Account Creation (no username, container, model): for Icloud one that
    already holds a Mail-Alias, for SMS-Pool one without. None = create a new row."""
    rows = _list(T_ACC, f"AND({{{A['creation']}}} = 'To Do', {{{A['username']}}} = '', {{{A['container']}}} = '', {{{A['model']}}} = '')")
    for r in rows:
        has_alias = bool(r["fields"].get(A["alias"]))
        if has_alias == (method == METHOD_ICLOUD):
            return r["id"]
    return None


def free_alias() -> dict | None:
    rows = _list(T_ALIAS, f"{{{AL['status']}}} = 'Free'")
    return {"id": rows[0]["id"], "addr": _text(rows[0]["fields"].get(AL["addr"]))} if rows else None


# ---------------------------------------------------------------- one slot



def prepare(plan: dict, slot_index: int, today: dt.date, dry_run: bool) -> dict:
    """Fill (or create) the Accounts SOP row for one time slot. Returns {row, container, phone} or raises."""
    f = plan["fields"]
    model_id = (f.get(P["model"]) or [None])[0]
    if not model_id:
        raise ValueError("kein Model im Plan")
    method = _text(f.get(P["method"])) or METHOD_ICLOUD
    names = [u.strip().lstrip("@") for u in _text(f.get(P["usernames"])).splitlines() if u.strip()]
    username = names[slot_index] if slot_index < len(names) else ""
    cont = free_container(f.get(P["phones"]) or [], today)
    if not cont:
        raise ValueError("kein freier Container auf den erlaubten Handys (oder alle Handys heute am Limit / gesperrt)")
    row = account_row(model_id, method)
    fields = {A["model"]: [model_id], A["container"]: [cont["id"]], A["method"]: method, A["creation"]: "To Do"}
    if username:
        fields[A["username"]] = username
    alias = None
    if method == METHOD_ICLOUD and row is None:
        alias = free_alias()
        if not alias:
            raise ValueError("kein freier Mail-Alias mehr (Mail Aliases)")
        fields[A["alias"]] = [alias["id"]]
    choice = {"row": row, "container": cont["name"], "phone": cont["phone"], "fields": fields, "alias": alias}
    if dry_run:
        return {**choice, "row": row or "(neue Zeile)"}
    return commit(plan, choice)


def commit(plan: dict, choice: dict) -> dict:
    """Write the chosen slot: alias Used, account row filled/created, linked to the plan."""
    alias, row, fields = choice["alias"], choice["row"], choice["fields"]
    if alias:
        # claimed at once (like iphone_signup.claim_fresh_alias): the signup's own "add an email" step takes
        # the first Free alias too and must never get this one
        _patch(T_ALIAS, alias["id"], {AL["status"]: "Used"})
    if row is None:
        row = _create(T_ACC, fields)
    else:
        _patch(T_ACC, row, fields)
    linked = plan["fields"].get(P["accounts"]) or []
    _patch(T_PLAN, plan["id"], {P["accounts"]: linked + [row]})
    plan["fields"][P["accounts"]] = linked + [row]
    return {"row": row, "container": choice["container"], "phone": choice["phone"]}


def _watchdog(action: str) -> bool:
    try:
        active = subprocess.run(["systemctl", "is-active", WATCHDOG_TIMER], capture_output=True, text=True).stdout.strip() == "active"
        if action == "stop" and active:
            subprocess.run(["systemctl", "stop", WATCHDOG_TIMER], check=False)
            return True
        if action == "start":
            subprocess.run(["systemctl", "start", WATCHDOG_TIMER], check=False)
        return active
    except OSError:
        return False


def restore_watchdog() -> None:
    """A pass that died mid-signup left the watchdog timer stopped -- start it again once no signup runs."""
    if os.path.exists(WATCHDOG_MARK) and not _running("adb_bot.automation.iphone_signup run"):
        _watchdog("start")
        os.remove(WATCHDOG_MARK)
        log.info("watchdog timer restarted (left paused by an interrupted pass)")


def run_signup(row: str) -> tuple[int, str]:
    paused = _watchdog("stop")
    if paused:
        os.makedirs(os.path.dirname(WATCHDOG_MARK), exist_ok=True)
        open(WATCHDOG_MARK, "w").close()
    try:
        proc = subprocess.run([PYTHON, "-m", "adb_bot.automation.iphone_signup", "run", row], cwd=ADB_BOT,
                              capture_output=True, text=True, timeout=SIGNUP_TIMEOUT_S)
        tail = (proc.stdout.strip().splitlines() or [""])[-1] if proc.stdout.strip() else (proc.stderr.strip().splitlines() or [""])[-1]
        return proc.returncode, tail[:200]
    except subprocess.TimeoutExpired:
        return 124, f"Signup nach {SIGNUP_TIMEOUT_S // 60} min abgebrochen"
    finally:
        if paused:
            _watchdog("start")
            try:
                os.remove(WATCHDOG_MARK)
            except OSError:
                pass


def outcome(row: str, code: int, tail: str) -> tuple[str, str]:
    """(state, text) for the slot from what the signup wrote back on the account row."""
    f = _get(T_ACC, row)["fields"]
    status, user = _text(f.get(A["creation"])), _text(f.get(A["username"]))
    if status == "Instagram Created":
        return "done", f"✅ @{user}"
    if status == "Stuck / Needs Human":
        return "done", f"🔐 @{user} – Verifizierung lösen"
    return "failed", f"❌ {tail or f'Exit {code}'}"


# ---------------------------------------------------------------- display

def render(plan: dict, slots: dict, models: dict) -> tuple[str, str, str]:
    """(Plan name, Status, Fortschritt) for the row."""
    f = plan["fields"]
    times = parse_times(_text(f.get(P["times"])))
    model = model_name((f.get(P["model"]) or [""])[0], models) if f.get(P["model"]) else "?"
    date = _text(f.get(P["date"]))
    try:
        date = dt.date.fromisoformat(date).strftime("%d.%m.")
    except ValueError:
        pass
    name = f"{model} · {date} · {_text(f.get(P['task']))} · {len(times)}"
    lines, states = [], []
    for t in times:
        s = slots.get(t, {})
        states.append(s.get("state", "pending"))
        where = f" ({s['phone']} / {s['container']})" if s.get("container") else ""
        if s.get("state") == "running":
            lines.append(f"{t} · ⏳ läuft seit {s.get('at', '?')}{where}")
        elif s.get("state") in ("done", "failed"):
            lines.append(f"{t} · {s.get('text', '')}{where}")
        else:
            lines.append(f"{t} · wartet" + (f" – {s['wait']}" if s.get("wait") else ""))
    if not times:
        lines.append("❌ keine Uhrzeit eingetragen (eine pro Zeile, z. B. 10:00)")
    if not states:
        status = "Fehler"
    elif "running" in states:
        status = "Läuft"
    elif all(x in ("done", "failed") for x in states):
        status = "Fertig"
    else:
        status = "Wartet"
    return name, status, "\n".join(lines)


def refresh(plan: dict, slots: dict, models: dict, dry_run: bool) -> None:
    name, status, text = render(plan, slots, models)
    f = plan["fields"]
    if dry_run:
        print(f"--- {plan['id']} {name} [{status}]\n{text}")
        return
    if (_text(f.get(P["name"])), _text(f.get(P["status"])), _text(f.get(P["progress"]))) != (name, status, text):
        _patch(T_PLAN, plan["id"], {P["name"]: name, P["status"]: status, P["progress"]: text})


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    now = dt.datetime.now(BERLIN)
    today = now.date()
    if not a.dry_run:
        restore_watchdog()
    state = _load(STATE_PATH)
    models: dict = {}
    plans = todays_plans(today)
    due = []
    for plan in plans:
        ps = state.setdefault(plan["id"], {"date": today.isoformat(), "slots": {}})
        times = parse_times(_text(plan["fields"].get(P["times"])))
        for i, t in enumerate(times):
            s = ps["slots"].get(t, {})
            if s.get("state") == "running" and not a.dry_run and not _running("adb_bot.automation.iphone_signup run"):
                # the planner itself runs the signup synchronously (flock: one at a time) -- a slot still
                # "running" at the start of a pass means the last pass died; read what the signup wrote back
                try:
                    st, text = outcome(s["row"], -1, "Planer wurde unterbrochen")
                except Exception as exc:  # noqa: BLE001
                    st, text = "failed", f"❌ Planer unterbrochen ({exc})"
                ps["slots"][t] = {**s, "state": st, "text": text}
                s = ps["slots"][t]
            if s.get("state") in (None, "pending") and t <= now.strftime("%H:%M"):
                due.append((t, plan, i))
    due.sort(key=lambda d: d[0])
    if due:
        busy = farm_busy()
        t, plan, i = due[0]
        slots = state[plan["id"]]["slots"]
        if busy:
            slots[t] = {"state": "pending", "wait": busy}
            log.info("slot %s of %s due, but %s", t, plan["id"], busy)
        else:
            try:
                choice = prepare(plan, i, today, dry_run=True)
            except Exception as exc:  # noqa: BLE001 -- the slot fails with its reason, the plan goes on
                slots[t] = {"state": "failed", "text": f"❌ {exc}"}
                log.warning("slot %s of %s: %s", t, plan["id"], exc)
                choice = None
            if choice and a.dry_run:
                print(f"would start {t} for plan {plan['id']}: row {choice['row']} on {choice['phone']} / {choice['container']} fields={choice['fields']}")
            elif choice:
                if choice["row"] == "(neue Zeile)":
                    choice["row"] = None
                letter = choice["container"][:1]
                # 10.10 (Rene): the signup goes first -- a posting batch on this phone finishes its current
                # post + scroll, then pauses before its next post / Shortcut until the signup is done
                phone_lock.want(letter, f"signup {t}")
                slots[t] = {"state": "pending", "wait": f"{choice['phone']}: der aktuelle Post wird noch fertig gemacht"}
                refresh(plan, slots, models, False)
                held = phone_lock.hold(letter, timeout_s=1800)
                try:
                    if held is None:
                        slots[t] = {"state": "pending", "wait": f"{choice['phone']} war 30 min belegt, nächster Versuch gleich"}
                        log.warning("slot %s of %s: %s stayed busy for 30 min", t, plan["id"], choice["phone"])
                    else:
                        prep = commit(plan, choice)
                        slots[t] = {"state": "running", "at": dt.datetime.now(BERLIN).strftime("%H:%M"), **prep}
                        _save_state(state)
                        refresh(plan, slots, models, False)
                        log.info("slot %s of %s: signup %s on %s / %s", t, plan["id"], prep["row"], prep["phone"], prep["container"])
                        code, tail = run_signup(prep["row"])
                        st, text = outcome(prep["row"], code, tail)
                        slots[t] = {**slots[t], "state": st, "text": text}
                        log.info("slot %s of %s: %s (exit %s)", t, plan["id"], text, code)
                finally:
                    if held:
                        held.release()
                    phone_lock.unwant(letter)
    for plan in plans:
        try:
            refresh(plan, state[plan["id"]]["slots"], models, a.dry_run)
        except Exception as exc:  # noqa: BLE001
            log.warning("refresh %s: %s", plan["id"], exc)
    if not a.dry_run:
        _save_state(state)
    if not plans:
        log.info("no active plan for %s", today.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
