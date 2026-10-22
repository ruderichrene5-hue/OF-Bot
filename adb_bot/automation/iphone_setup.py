"""Account setup on the iPhone farm: profile picture, bio, story highlight with the OF link (Rene, 22.10).

Not bound to a warm-up day -- bound to the material. Every tick looks at each account: a picture in
`Profile Picture` and `Profile Picture done` unchecked -> upload it. A hand-written `Bio Text` and `Bio done`
unchecked -> type it (Rene 22.10: no pre-made pool bios, he writes them himself). `Story Highlight Picture` + `Funnel Link` and `Story Highlight Set`
unchecked -> story with link sticker, saved into a highlight. The bot ticks the checkbox when the step is
verified on the phone; on failure it keeps a screenshot, writes a Notes line and leaves the box empty, so
the next tick tries again. Two guards: warm-up day 1 must have run (or the account is Healthy), and one
profile edit per account per day (IPHONE_SETUP_MULTI=1 lifts that). Window 09-23 Berlin.

    .venv/bin/python -m adb_bot.automation.iphone_setup due
    .venv/bin/python -m adb_bot.automation.iphone_setup run <device> <container> --step picture|bio|highlight [--row rec..]
    .venv/bin/python -m adb_bot.automation.iphone_setup tick [--max-phones 4]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import sys
import tempfile
import threading
import time

import requests

from adb_bot.automation import iphone_diag, iphone_farm_config
from adb_bot.automation import iphone_phone_lock as phone_lock
from adb_bot.automation.iphone_farm_wda import Phone, resolve
from adb_bot.automation.iphone_ig import (create_story, media_to_photos, save_story_to_highlight, set_bio,
                                          set_profile_picture)
from adb_bot.automation.iphone_warmup import (BASE, BERLIN, BLOCKED_STATUS, F_DONE, F_NOTES, F_STATUS, F_USER, T_ACC,
                                              T_CONT, WARMUP_STATUS, WINDOW, _get, _hdr, _letter, _list, _note, _patch)

log = logging.getLogger("iphone_setup")
F_PIC, F_PIC_DONE, F_PIC_SET = "Profile Picture", "Profile Picture done", "Profile Picture Set"
F_BIO, F_BIO_DONE = "Bio Text", "Bio done"           # 22.10 Rene: hand-written text on the row, not the Bio POOL link
F_HL_PIC, F_HL_SET, F_LINK, F_LINK_TEXT = "Story Highlight Picture", "Story Highlight Set", "Funnel Link", "Story Link text"
F_LINK_DONE, F_FUNNEL_DONE = "Link gesetzt (Story/Highlight)", "Story funnel done"
HIGHLIGHT_NAME = os.environ.get("IPHONE_HIGHLIGHT_NAME", "Links")
MULTI = os.environ.get("IPHONE_SETUP_MULTI") == "1"
STATE_PATH = os.path.expanduser("~/.adb_bot/iphone_setup_state.json")
STEPS = ("picture", "bio", "highlight")


# ---------------------------------------------------------------- who needs what

def _state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _mark_state(row: str) -> None:
    s = _state()
    s[row] = dt.datetime.now(BERLIN).strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)


def missing_for_highlight(f: dict) -> list[str]:
    """What the highlight step still lacks on the row (for `due` and the flow check)."""
    if f.get(F_HL_SET):
        return []
    return [n for n, v in ((F_HL_PIC, f.get(F_HL_PIC)), (F_LINK, f.get(F_LINK)), (F_LINK_TEXT, (f.get(F_LINK_TEXT) or "").strip())) if not v]


def needed(f: dict) -> list[str]:
    """The setup steps this row still needs, in order, judged by material present + checkbox empty."""
    out = []
    if f.get(F_PIC) and not f.get(F_PIC_DONE):
        out.append("picture")
    if (f.get(F_BIO) or "").strip() and not f.get(F_BIO_DONE):
        out.append("bio")
    # 22.10 Rene: the link sticker ALWAYS carries a custom text -- without `Story Link text` the step is not due
    if f.get(F_HL_PIC) and f.get(F_LINK) and (f.get(F_LINK_TEXT) or "").strip() and not f.get(F_HL_SET):
        out.append("highlight")
    return out


def next_step(f: dict, row: str) -> tuple[str | None, str]:
    """(step to run now, reason)."""
    status = f.get(F_STATUS) or ""
    steps = needed(f)
    if not steps:
        return None, "nothing to do"
    if status in BLOCKED_STATUS:
        return None, f"status {status!r}"
    if not (f.get(F_USER) or "").strip():
        return None, "no username on the row"
    m = WARMUP_STATUS.search(status)
    warmed = bool(f.get(F_DONE[1])) or (m and int(m.group(1)) >= 2) or status == "Healthy"
    if not warmed:
        return None, f"warm-up day 1 not run yet (status {status!r}) -- needs {', '.join(steps)}"
    today = dt.datetime.now(BERLIN).strftime("%Y-%m-%d")
    if not MULTI and _state().get(row) == today:
        return None, f"already one profile edit today -- {', '.join(steps)} tomorrow"
    return steps[0], f"needs {', '.join(steps)}"


def candidates() -> list[dict]:
    rows = _list(T_ACC, f"AND({{Container}} != '', OR({{{F_PIC}}} != '', {{{F_BIO}}} != '', {{{F_HL_PIC}}} != ''))")
    out = []
    for row in rows:
        f = row["fields"]
        lacking = missing_for_highlight(f)
        if not needed(f):
            if f.get(F_HL_PIC) and lacking:                   # a highlight picture is there but the step can't run
                cont_id = (f.get("Container") or [None])[0]
                cont = _get(T_CONT, cont_id)["fields"] if cont_id else {}
                out.append({"row": row["id"], "fields": f, "username": (f.get(F_USER) or "").lstrip("@"), "container": cont.get("Container"),
                            "device": (cont.get("Device") or [None])[0], "step": None, "status": f.get(F_STATUS),
                            "why": "highlight waits for: " + ", ".join(lacking)})
            continue
        step, why = next_step(f, row["id"])
        cont_id = (f.get("Container") or [None])[0]
        try:
            cont = _get(T_CONT, cont_id)["fields"] if cont_id else {}
        except requests.RequestException:
            cont = {}
        out.append({"row": row["id"], "fields": f, "username": (f.get(F_USER) or "").lstrip("@"), "container": cont.get("Container"),
                    "device": (cont.get("Device") or [None])[0], "step": step, "why": why, "status": f.get(F_STATUS)})
    return out


# ---------------------------------------------------------------- the three steps

def _download(att: dict, workdir: str) -> str:
    name = att.get("filename") or "media.jpg"
    path = os.path.join(workdir, name.replace("/", "_"))
    r = requests.get(att["url"], timeout=120)
    r.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(r.content)
    return path


def step_picture(p: Phone, device: str, f: dict, say) -> dict:
    with tempfile.TemporaryDirectory() as wd:
        path = _download(f[F_PIC][0], wd)
        media_to_photos(p, device, path, log=say)
    res = set_profile_picture(p, 0, grant_newest=True, log=say)
    if not res.get("ok"):
        raise RuntimeError(f"profile picture upload did not finish ({res})")
    return {F_PIC_DONE: True, F_PIC_SET: True}


def step_bio(p: Phone, device: str, f: dict, say) -> dict:
    text = (f.get(F_BIO) or "").strip()
    if not text:
        raise RuntimeError("Bio Text is empty")
    if len(text) > 150:
        raise RuntimeError(f"Bio Text has {len(text)} chars, Instagram allows 150 -- shorten it in Airtable")
    res = set_bio(p, text, log=say)
    if not res.get("ok"):
        raise RuntimeError(f"bio read back differs: {res}")
    return {F_BIO_DONE: True}


def step_highlight(p: Phone, device: str, f: dict, say) -> dict:
    with tempfile.TemporaryDirectory() as wd:
        path = _download(f[F_HL_PIC][0], wd)
        media_to_photos(p, device, path, log=say)
    link, text = f[F_LINK].strip(), (f.get(F_LINK_TEXT) or "").strip()
    if not text:
        raise RuntimeError("Story Link text is empty -- the link sticker always needs a text (Rene 22.10)")
    story = create_story(p, grant_newest=1, log=say, link=link, link_text=text)
    if not story.get("shared"):
        raise RuntimeError(f"story not shared ({story})")
    hl = save_story_to_highlight(p, HIGHLIGHT_NAME, log=say)
    if not hl.get("ok"):
        raise RuntimeError(f"story shared with link, but highlight not confirmed ({hl}) -- check the profile")
    return {F_HL_SET: True, F_LINK_DONE: True, F_FUNNEL_DONE: True}


STEP_FN = {"picture": step_picture, "bio": step_bio, "highlight": step_highlight}


# ---------------------------------------------------------------- one run

def run_step(device: str, container: str, step: str, row: str | None = None, fields: dict | None = None,
             expect: str | None = None, log_fn=None) -> dict:
    say = log_fn or (lambda m: log.info("%s/%s: %s", _letter(device), container, m))
    if fields is None:
        if not row:
            raise ValueError("fields or row needed")
        fields = _get(T_ACC, row)["fields"]
    rec_id, _ = resolve(device)
    letter = _letter(device)
    if phone_lock.signup_wanted(letter):                        # signup first (Rene 22.10), see iphone_warmup.run_one
        return {"ok": False, "step": step, "error": f"phone {letter}: a signup is waiting for it -- next tick"}
    hold = phone_lock.hold(letter, timeout_s=600)
    if hold is None:
        return {"ok": False, "step": step, "error": f"phone {letter} busy"}
    p: Phone | None = None
    try:
        iphone_farm_config._set_off(rec_id, True)
        from adb_bot.automation.iphone_ig import open_account
        p = open_account(device, container, expect, log=say)
        say(f"setup step '{step}' starting")
        boxes = STEP_FN[step](p, device, fields, say)
        if row:
            _patch(row, boxes)
            _note(row, f"setup {step} ok")
            _mark_state(row)
        say(f"setup step '{step}' done -> {list(boxes)}")
        return {"ok": True, "step": step, "set": list(boxes)}
    except Exception as exc:                                  # noqa: BLE001
        shot = iphone_diag.keep_screen(device, f"setup-{step}", f"{type(exc).__name__}: {exc}", container=container, wda=p, log_fn=say)
        say(f"setup step '{step}' FAILED: {exc} (screen: {shot or 'none'})")
        if row:
            _note(row, f"setup {step} failed: {str(exc)[:300]} | screen {shot or '-'}")
            _mark_state(row)                                  # a half-done edit still counts as today's edit
            if "logged out" in str(exc):
                _patch(row, {F_STATUS: "logged out"})
        iphone_diag.notify(f"setup {step} on {letter}/{container} (@{expect or '?'}) failed: {str(exc)[:200]}\n{shot or ''}")
        return {"ok": False, "step": step, "error": str(exc), "screen": shot}
    finally:
        try:
            iphone_farm_config._set_off(rec_id, False)
        except Exception:                                     # noqa: BLE001
            pass
        hold.release()


def tick(max_phones: int = 4) -> dict:
    now = dt.datetime.now(BERLIN)
    if not WINDOW[0] <= now.hour < WINDOW[1]:
        log.info("outside %02d-%02d Berlin", *WINDOW)
        return {"skipped": "window"}
    due = [c for c in candidates() if c["step"] and c["device"] and c["container"]]
    random.shuffle(due)
    by_phone: dict[str, dict] = {}
    for c in due:
        by_phone.setdefault(c["device"], c)
    picked = list(by_phone.values())[:max_phones]
    if not picked:
        log.info("nothing due")
        return {"ran": []}
    results: dict[str, dict] = {}

    def work(c: dict) -> None:
        time.sleep(random.uniform(0, 60))
        results[c["container"]] = run_step(c["device"], c["container"], c["step"], row=c["row"], fields=c["fields"],
                                           expect=c["username"] or None)

    ths = [threading.Thread(target=work, args=(c,), name=c["container"]) for c in picked]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return {"ran": [{"container": k, **{x: v.get(x) for x in ("ok", "step", "error")}} for k, v in results.items()]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("due")
    s = sub.add_parser("run")
    s.add_argument("device"); s.add_argument("container")
    s.add_argument("--step", required=True, choices=STEPS)
    s.add_argument("--row", help="Accounts SOP record id (fields + checkboxes come from / go to it)")
    s.add_argument("--expect")
    s = sub.add_parser("tick")
    s.add_argument("--max-phones", type=int, default=4)
    a = ap.parse_args(argv)
    os.makedirs("/opt/adbbot/logs", exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("/opt/adbbot/logs/iphone_setup.log")])
    if a.cmd == "due":
        for c in candidates():
            mark = f"DUE {c['step']}" if c["step"] else "wait"
            print(f"{mark:14} {_letter(c['device'] or '')}/{c['container'] or '?':4} @{c['username'] or '?':22} "
                  f"status {c['status'] or '-':18} {c['why']}")
        return 0
    if a.cmd == "run":
        res = run_step(a.device, a.container, a.step, row=a.row, expect=a.expect)
        print(res)
        return 0 if res.get("ok") else 1
    res = tick(a.max_phones)
    log.info("tick: %s", res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
