"""Instagram signup on the physical iPhones, driven from Airtable (08.10, Rene: "den Flow mit
Sign-Up aus der Airtable-Tabelle mit den Alias-Verbindungen bauen").

One Accounts-SOP row = one account to create. A row is ready when
    Creation Status = To Do, Container linked (-> phone + Shortcut), Model auswählen linked,
    Mail-Alias wählen linked (Hide My Email address), Username empty or chosen by Rene.

Steps per row:
  1. open the container through its Shortcut (switches Crane container, starts the proxy,
     opens Instagram) -- the same rule as every other iPhone flow;
  2. walk the signup with `flows.signup.run_signup` (the platform-neutral state machine the
     Android signup uses) through `IphoneSignupDriver`: screens are read from WDA's element
     tree, taps go through XXTouch (Rene: never WDA taps), text through WDA's keyboard;
  3. the confirmation code is read from the iCloud inbox (`icloud_codes`, alias in To:);
  4. write back: Username, IG Password, Email, Creation Status (Instagram Created /
     Stuck / Needs Human), Notes; the alias goes to Used; the container to Belegt.

Human verification: a "Confirm you're human" right after creation ends as
`created_unverified` -> Creation Status "Stuck / Needs Human" with the credentials saved, the
Notes say which screen. Selfie-video checks cannot be automated; SMS/text checks are handled
by the verification flow later, not here.

    .venv/bin/python -m adb_bot.automation.iphone_signup list                 # ready rows
    .venv/bin/python -m adb_bot.automation.iphone_signup run <row id|username> [--dry-run]
    .venv/bin/python -m adb_bot.automation.iphone_signup screen <container>   # what the driver sees
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

from adb_bot.automation import icloud_codes, iphone_farm_config, iphone_xxtouch
from adb_bot.automation.flows import signup as S
from adb_bot.automation.iphone_farm_wda import Phone, WDAError, connect
from adb_bot.automation.iphone_farm_wda import wait_for_foreground
from adb_bot.automation.iphone_farm_wda import dismiss_xxtouch_dialog as dismiss_xxtouch_dialog
from adb_bot.automation.iphone_ig import IG_BUNDLE, FlowError, Human, back_to_instagram
from adb_bot.automation.signup_identity import make_identity

log = logging.getLogger("iphone_signup")

BASE = "appwk9LrgAhJv8iEH"
T_ACC, T_CONT, T_ALIAS, T_MODELS = "tbly0y1I4hNDlvh1K", "tblyZXN7caKZZBaBj", "tblodjIl9a7B7LvHa", "tblMkfEq3cEnXuZs5"
A = {"username": "fld6qQqvc3xZVu93Q", "email": "flde10adBpXhBl2Hn", "ig_password": "fldHqlRoaElnfdvHR", "notes": "fldSLCdSS3p8iZX0V",
     "container": "fldMiw9AuAg8wUEzd", "creation": "fld8vhG5DGnsiJafk", "model": "fldWqrNcLsbogxphV", "alias": "fldGn90iK5lBytleM",
     "signup_method": "fldPmxXuITL6TNk3f", "status": "fldBaqXXLYIP8cbEC"}
C = {"name": "fldcBLkxaDrsZIht0", "device": "fldck8WL7t2Q17kUT", "status": "fld23I43IfgL6y2em", "accounts": "fld570bl55cPBlWWX"}
AL = {"addr": "fld0WY7btzpCTK3xR", "status": "fldH3hXRrOAjPQvXq"}
M_NAME = "fld9FFX1ohatkPIev"
CS_TODO, CS_CREATED, CS_STUCK = "To Do", "Instagram Created", "Stuck / Needs Human"


class RateLimited(RuntimeError):
    pass


# ---------------------------------------------------------------- Airtable

def _hdr() -> dict:
    tok = os.environ.get("AIRTABLE_TOKEN") or os.environ.get("AIRTABLE_API_KEY") or ""
    if not tok:
        raise RuntimeError("AIRTABLE_TOKEN is not set (source /etc/adbbot/env)")
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _get(table: str, rid: str) -> dict:
    r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}/{rid}", headers=_hdr(), params={"returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _list(table: str, formula: str) -> list[dict]:
    out, offset = [], None
    while True:
        params = {"filterByFormula": formula, "returnFieldsByFieldId": "true"}
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{BASE}/{table}", headers=_hdr(), params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out


def _patch(table: str, rid: str, fields: dict) -> None:
    r = requests.patch(f"https://api.airtable.com/v0/{BASE}/{table}/{rid}", headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=30)
    r.raise_for_status()


def claim_fresh_alias() -> dict | None:
    """One Free Mail-Alias row, claimed (Status -> Used) immediately so two concurrent
    signups can never grab the same one. Returns {id, email} or None if the pool is dry.

    10.10, explicit instruction (chat): at the post-signup "Add an email address"
    prompt, always add one here -- "da ist immer was drin zum Nehmen" (new aliases are
    minted daily, the pool is never actually empty in practice) -- rather than the
    'Skip' every other post-signup prompt gets.
    """
    rows = _list(T_ALIAS, "{Status}='Free'")
    if not rows:
        return None
    row = rows[0]
    _patch(T_ALIAS, row["id"], {AL["status"]: "Used"})
    return {"id": row["id"], "email": row["fields"].get(AL["addr"])}


def ready_rows() -> list[dict]:
    """Accounts whose signup can start: To Do + container + model.

    Mail-Alias is NOT required (10.10, Rene via chat: for now accounts are
    created with an SMS-Pool number only; the iCloud alias gets linked to the
    account afterwards, once it has survived its first warm-up day -- that
    hand-off is a separate, not-yet-built step). A row with no alias linked
    goes through `run_row` via the phone/SMS chain instead of the email one;
    a row that DOES have one linked keeps working the old way unchanged.
    """
    rows = _list(T_ACC, f"AND({{Creation Status}} = '{CS_TODO}', {{Container}} != '', {{Model auswählen (Pflicht)}} != '')")
    return rows


def describe(row: dict) -> dict:
    """Everything the run needs, resolved: phone record id, container name, alias (if any), model."""
    f = row["fields"]
    cont = _get(T_CONT, f[A["container"]][0])["fields"]
    dev = (cont.get(C["device"]) or [None])[0]
    alias_ids = f.get(A["alias"]) or []
    if alias_ids:
        alias = _get(T_ALIAS, alias_ids[0])["fields"]
        alias_addr, alias_id, alias_status = alias.get(AL["addr"]), alias_ids[0], alias.get(AL["status"])
    else:
        alias_addr = alias_id = alias_status = None
    model = _get(T_MODELS, f[A["model"]][0])["fields"].get(M_NAME, "")
    return {"row": row["id"], "username": (f.get(A["username"]) or "").strip().lstrip("@"), "container": cont.get(C["name"]),
            "device": dev, "alias": alias_addr, "alias_id": alias_id, "alias_status": alias_status,
            "model": model, "container_id": f[A["container"]][0], "ig_password": f.get(A["ig_password"]) or "",
            "linked_accounts": len(cont.get(C["accounts"]) or [])}


# ---------------------------------------------------------------- the device seam

class IphoneSignupDriver:
    """`flows.signup.SignupDriver` on an iPhone: screen = WDA element tree, taps = XXTouch."""

    FIELD_TYPES = ("XCUIElementTypeTextField", "XCUIElementTypeSecureTextField", "XCUIElementTypeTextView")

    def __init__(self, p: Phone, device_rec: str, container: str, touch: iphone_xxtouch.Touch | None, logger=None):
        self.p, self.device_rec, self.container, self.touch = p, device_rec, container, touch
        self.human = Human(p)
        if touch is None:
            # Human() opens its own independent XXTouch handle (iphone_ig.Human.__init__), unaware
            # of the 403/E430 fallback decision made before this driver was built -- without this,
            # the "fall back to WDA" path still hits the same refused-license error on the first tap.
            self.human.xx = None
        self.logger = logger or log
        self._root = None
        self._elems: list[dict] = []
        self.restarts = 0
        self.identity = None
        self._birthday_set = False

    # -- screen ---------------------------------------------------------------
    # the Android classifier knows the Play Store launcher; an iOS home screen / other app
    # gets translated into words it understands (-> SCREEN_LAUNCHER / SCREEN_NOT_INSTAGRAM -> restart_app)
    IOS_LAUNCHER_TEXT = "google play store telephone messaging camera gallery (iOS home screen)"
    IOS_OTHER_APP_TEXT = "google play store sign in to find the latest android apps, games, movies, music (iOS: {app} in front)"

    def read_screen(self) -> str:
        dismiss_xxtouch_dialog(self.p)
        try:
            app = self.p.active_app().get("bundleId") or ""
        except WDAError:
            app = ""
        if app and app != IG_BUNDLE:
            self._root, self._elems = None, []
            self.logger.info("%s is in front, not Instagram", app)
            return self.IOS_LAUNCHER_TEXT if app == "com.apple.springboard" else self.IOS_OTHER_APP_TEXT.format(app=app)
        try:
            xml = self.p.source("xml")
        except WDAError as exc:
            self.logger.warning("source failed: %s", exc)
            return ""
        try:
            self._root = ET.fromstring(xml)
        except ET.ParseError:
            self._root = None
            return ""
        self._elems = []
        words = []
        for node in self._root.iter():
            a = node.attrib
            if a.get("visible", "true") == "false":
                continue
            t = a.get("type", "")
            try:
                rect = {k: float(a.get(k, 0)) for k in ("x", "y", "width", "height")}
            except ValueError:
                continue
            if rect["width"] <= 0 or rect["height"] <= 0:
                continue
            label = a.get("label") or a.get("name") or ""
            value = a.get("value") or ""
            self._elems.append({"type": t, "label": label, "name": a.get("name", ""), "value": value, "rect": rect,
                                "enabled": a.get("enabled", "true") != "false"})
            for s in (label, value):
                if s and s not in words:
                    words.append(s)
        try:                                                     # iOS system alerts are their own tree
            alert = self.p.alert_text()
            if alert:
                words.append(alert)
                words += self.p.alert_buttons()
        except WDAError:
            pass
        text = "\n".join(words)
        self.logger.info("screen: %s", " | ".join(words)[:400])
        low = text.lower()
        # iOS post-creation prompts the Android classifier does not know: decline/skip them here
        # (Rene: "immer ablehnen") and read again -- the feed behind them is SCREEN_DONE.
        for marker, button in (("please turn on notifications", "Not now"), ("add a profile picture", "Skip"),
                               ("allow instagram to use your contacts", "Not now"), ("turn on contacts", "Not now"),
                               # Confirmed live 11.10, C5 (@nikkigoesplaces88): a second, differently
                               # worded push-notifications prompt ("Turn on push notifications / Stay
                               # up to date...") that the marker above does not match -- the account
                               # was already created at this point, but the run still finished as
                               # unknown_screen and wrote back "Stuck" instead of "Instagram Created".
                               ("turn on push notifications", "Not now")):
            if marker in low and self.tap_label((button,)):
                self.logger.info("post-signup prompt %r -> %s", marker, button)
                time.sleep(2)
                return self.read_screen()
        # iOS-only "Make sure your device is nearby" interstitial, shown right after
        # entering the mobile number and before the code screen. The shared Android
        # classifier does not know it (confirmed live 10.10: read as SCREEN_UNKNOWN,
        # which restarted the whole app and burned the number for nothing) -- tap
        # Continue and move on.
        if "make sure your device is nearby" in low and self.tap_label(("Continue",)):
            self.logger.info("device-nearby interstitial -> Continue")
            time.sleep(2)
            return self.read_screen()
        # iOS post-signup "Add an email address" prompt: fill a fresh alias instead of
        # skipping it like the prompts above (10.10, explicit instruction -- see
        # claim_fresh_alias's docstring). Falls back to Skip if the alias pool is dry
        # or the field/button cannot be found, so a bad day here never strands the run.
        if "add an email address" in low and "add email" in low:
            if self._handle_add_email():
                time.sleep(2)
                return self.read_screen()
            self.tap_label(("Skip",))
            time.sleep(2)
            return self.read_screen()
        if "we limit how often you can do certain things" in low:
            # Instagram's signup throttle for this IP/device ("Try Again Later"). Restarting the
            # chain only burns more codes -- stop the run, the caller switches container later.
            raise RateLimited("Instagram: 'Try Again Later -- we limit how often you can sign up' on this IP/container")
        # iOS birthday screen: the three picker wheels are already on screen (Android opens a
        # dialog first), so set them here before the flow taps Next -- otherwise Instagram reads
        # "0 years old" and detours to "How old are you?"
        if self.identity is not None and "what's your birthday" in text.lower() and "0 years old" in text:
            if self.set_date(self.identity.birth_day, self.identity.birth_month, self.identity.birth_year):
                time.sleep(1)
                return self.read_screen()
        return text

    def _handle_add_email(self) -> bool:
        """Claim a fresh alias and submit it on the post-signup 'Add an email address'
        prompt. Stamps it onto `self.identity` so `write_back` can record which one was
        used. Returns False (leaving the alias claim in place -- see claim_fresh_alias,
        it is marked Used before we know whether typing it will work, same tradeoff as
        every other number/alias claim in this codebase) if the field or button cannot
        be found, so the caller falls back to Skip."""
        alias = claim_fresh_alias()
        if alias is None or not alias.get("email"):
            self.logger.warning("add-email prompt: no free alias available, skipping instead")
            return False
        field = self.p.find("predicate string", "type == 'XCUIElementTypeTextField' AND visible == 1")
        if field is None:
            self.logger.warning("add-email prompt: no text field found")
            return False
        field.click()
        time.sleep(0.5)
        field.type(alias["email"])
        time.sleep(1)
        btn = self.p.find("predicate string", "type == 'XCUIElementTypeButton' AND label == 'Add email' AND visible == 1")
        if btn is None:
            self.logger.warning("add-email prompt: no 'Add email' button found")
            return False
        btn.click()
        if self.identity is not None:
            self.identity.email = alias["email"]
            self.identity.alias_id = alias["id"]
        self.logger.info("add-email prompt: added %s", alias["email"])
        return True

    def _norm(self, s: str) -> str:
        return " ".join((s or "").replace("’", "'").split()).strip().lower()

    def _tap_rect(self, rect: dict, what: str = ""):
        x = rect["x"] + rect["width"] * random.uniform(0.3, 0.7)
        y = rect["y"] + rect["height"] * random.uniform(0.3, 0.7)
        self.logger.info("tap %s at (%.0f, %.0f)", what or "", x, y)
        if self.touch is not None:
            self.touch.tap(x, y)
        else:
            self.human.tap_xy(x, y)
        time.sleep(random.uniform(0.6, 1.3))

    def clickable_labels(self) -> list[str]:
        """Labels of the enabled buttons/cells on screen (24.10: required by flows.signup on chooser screens)."""
        if not self._elems:
            self.read_screen()
        return [e["label"] for e in self._elems if e["type"] in ("XCUIElementTypeButton", "XCUIElementTypeCell", "XCUIElementTypeLink")
                and e["label"] and e.get("enabled", True)]

    def tap_label(self, labels) -> bool:
        if self._root is None:
            self.read_screen()
        wanted = [self._norm(x) for x in labels]
        # alerts first: their buttons are not in the main tree
        try:
            for b in self.p.alert_buttons():
                if self._norm(b) in wanted:
                    self.logger.info("alert button %r", b)
                    self.p.alert_accept(b)
                    time.sleep(1)
                    return True
        except WDAError:
            pass
        # exact label, prefer buttons/cells/links over static text
        order = ("XCUIElementTypeButton", "XCUIElementTypeCell", "XCUIElementTypeLink", "XCUIElementTypeStaticText", "XCUIElementTypeOther")
        for t in order:
            for e in self._elems:
                if e["type"] == t and e["enabled"] and self._norm(e["label"]) in wanted:
                    self._tap_rect(e["rect"], repr(e["label"]))
                    self._root = None
                    return True
        self.logger.warning("none of %s on screen; labels: %s", list(labels), [e["label"] for e in self._elems if e["type"] == "XCUIElementTypeButton"][:15])
        return False

    # -- typing ----------------------------------------------------------------
    def _field(self, hints) -> dict | None:
        if self._root is None:
            self.read_screen()
        fields = [e for e in self._elems if e["type"] in self.FIELD_TYPES]
        hints = [self._norm(h) for h in hints]
        for e in fields:
            hay = self._norm(f"{e['label']} {e['name']} {e['value']}")
            if any(h in hay for h in hints):
                return e
        return fields[0] if len(fields) == 1 else None

    def fill(self, hints, value: str, what: str, submits_itself: bool = False) -> bool:
        # 13.10, Rene (standing rule): a half-typed field used to end the whole run as RESULT_STUCK
        # on the first miss -- with Instagram seeing a string of started-then-abandoned signups.
        # Two extra tries (re-tap, clear, retype) before giving up for real.
        for attempt in range(3):
            e = self._field(hints)
            if e is None:
                if attempt == 0:
                    self.logger.warning("no field for %s (%s)", what, list(hints))
                return False
            self._tap_rect(e["rect"], f"field {what}")
            el = self.p.find("predicate string", f"type == '{e['type']}' AND visible == 1 AND name == {json.dumps(e['name'])}") if e["name"] else None
            if el is None:
                els = [x for x in self.p.find_all("predicate string", f"type == '{e['type']}' AND visible == 1")]
                el = els[0] if els else None
            try:
                if el is not None:
                    cur = el.text() or ""
                    if cur and self._norm(cur) != self._norm(e["label"]):        # placeholder shows as value on iOS
                        el.clear()
                        time.sleep(0.4)
            except WDAError:
                pass
            for ch in value:                                                     # keystroke by keystroke, human pace
                self.p.type(ch)
                time.sleep(random.uniform(0.05, 0.18))
            time.sleep(0.6)
            self._root = None
            self.read_screen()
            if self.field_holds(hints, value):
                return True
            if submits_itself and self._field(hints) is None:
                # e.g. the 6-digit code field: it can act on its last digit and the screen
                # moves on before this check runs -- the field being gone is success, not a miss.
                return True
            self.logger.warning("%s: field does not hold %r after typing (try %d/3)",
                                what, value if what != "password" else "***", attempt + 1)
            time.sleep(1.0)
        return False

    def field_holds(self, hints, value: str) -> bool:
        e = self._field(hints)
        if e is None:
            return False
        if e["type"] == "XCUIElementTypeSecureTextField":
            return len(e["value"].replace("•", "")) == 0 and len(e["value"]) == len(value) or len(e["value"]) >= len(value)
        return self._norm(value) in self._norm(e["value"])

    def dismiss_keyboard(self) -> None:
        if self._root is None:
            self.read_screen()
        # "return"/"go" would SUBMIT the field (the name screen jumped to username that way and the
        # flow's own Next then skipped the username screen) -- only a real Done key hides the keyboard
        for e in self._elems:
            if e["type"] == "XCUIElementTypeButton" and self._norm(e["label"]) in ("done", "fertig"):
                self._tap_rect(e["rect"], "keyboard done")
                self._root = None
                return
        w, h = self.p.size()
        self._tap_rect({"x": w * 0.4, "y": h * 0.12, "width": w * 0.2, "height": 30}, "outside keyboard")
        self._root = None

    def press_enter(self) -> None:
        self.p.type("\n")
        time.sleep(1.0)
        self._root = None

    def submit_with_keyboard(self) -> None:
        self.press_enter()

    def set_date(self, day: int, month: str, year: int) -> bool:
        """iOS birthday screen: three picker wheels (month, day, year).

        2026-10-19 Rene: WDA's POST /element/<id>/value returned success on the picker wheels
        but the wheels never moved on half the phones (iPhone 7/8 especially). Fallback below:
        after the API call, re-read the wheel's `value` attribute; if it still disagrees, do
        a short swipe towards the target (iOS picker items are ~44 pt tall, scrolling in the
        wheel's own middle). Repeat up to 10 swipes per wheel until the value matches or we
        clearly overshoot."""
        wheels = self.p.find_all("class name", "XCUIElementTypePickerWheel")
        if len(wheels) < 3:
            self.logger.warning("expected 3 picker wheels, found %d", len(wheels))
            return False
        for wheel, val in zip(wheels[:3], (month, str(day), str(year))):
            # 1st try: WDA set-value (works on some iPhones)
            try:
                self.p._req("POST", self.p._s(f"/element/{wheel.id}/value"), {"value": val})
            except WDAError as exc:
                self.logger.warning("picker %r: set-value %s", val, exc)
            time.sleep(random.uniform(0.4, 0.9))
            # 2nd try: swipe-based fallback -- read current value, swipe towards target
            current = None
            try:
                current = str(wheel.attr("value") or "").strip()
            except Exception:
                pass
            if current and current.strip(" .") != str(val).strip():
                self.logger.info("picker: set-value didn't take (val=%r still %r), swiping", val, current)
                try:
                    r = wheel.rect()
                    cx = r["x"] + r["width"] / 2
                    cy = r["y"] + r["height"] / 2
                    # Try up to 10 small swipes towards target; direction heuristic: swipe up if the
                    # current value sorts lexically BEFORE target (next items are below), else down.
                    going_down = current < str(val)
                    for _ in range(10):
                        dy = 80 if going_down else -80
                        self.p.swipe(cx, cy, cx, cy + dy, seconds=0.25)
                        time.sleep(random.uniform(0.3, 0.6))
                        try:
                            new = str(wheel.attr("value") or "").strip()
                        except Exception:
                            break
                        if new.strip(" .") == str(val).strip():
                            self.logger.info("picker: landed on %r via swipe", val)
                            break
                        if new == current:
                            break  # swipe not working, give up
                        current = new
                    else:
                        self.logger.warning("picker: %r never reached (last=%r)", val, current)
                except Exception as exc:
                    self.logger.warning("picker swipe fallback failed: %s", exc)
        self._root = None
        return True

    def keep_screen(self, reason: str, detail: str = "") -> str | None:
        """22.10 (Rene): picture + on-screen text of where the signup got stuck -> /opt/adbbot/logs/screens/."""
        from adb_bot.automation import iphone_diag
        return iphone_diag.keep_screen(getattr(self.p, "ref", self.device_rec), f"signup-{reason}", detail,
                                       container=self.container, wda=self.p, xx=self.touch,
                                       log_fn=self.logger.info)

    def restart_app(self) -> bool:
        """Instagram vanished: re-run the container Shortcut (proxy + container + app).

        2026-10-20 Rene: NIE restarten wenn der letzte Erfolg bei "I agree" / Terms-Screen war
        (= Account wurde grad erstellt). Nach "I agree" zeigt Instagram einen unbekannten Welcome-
        oder "Add profile picture"-Screen, den Script als SCREEN_UNKNOWN liest. Ein Restart würde
        Instagram schließen bevor der Account sicher eingeloggt ist -> Account hängt in HV.
        Besser: einfach warten (True returnen ohne restart), der Post-Signup Screen räumt sich
        selbst auf oder Rene macht manuell weiter."""
        if getattr(self, "terms_agreed", False):
            self.logger.info("restart via shortcut SKIPPED -- Terms were agreed, account should be created, do not kill IG now")
            return True
        if self.restarts >= 2:
            return False
        self.restarts += 1
        self.keep_screen("restart-app", f"restart {self.restarts}")
        try:
            self.p.terminate(IG_BUNDLE)
        except WDAError:
            pass
        ok, msg = iphone_farm_config.run_shortcut(self.device_rec, self.container)
        self.logger.info("restart via shortcut %s: %s %s", self.container, ok, msg[:60])
        up = ok and wait_for_instagram(self.p, self.container, 120)
        time.sleep(3)
        self._root = None
        return bool(up)


class AliasMailbox:
    """What run_signup needs from a mailbox: wait_for_code(timeout) -> str|None."""

    def __init__(self, alias: str):
        self.alias, self.since = alias, time.time() - 30

    def wait_for_code(self, timeout: float = 240) -> str | None:
        log.info("waiting up to %.0f s for Instagram's mail to %s", timeout, self.alias)
        return icloud_codes.wait_for_code(self.alias, since=self.since, timeout=timeout)


# ---------------------------------------------------------------- one account

def wait_for_instagram(p: Phone, container: str, seconds: float = 240) -> bool:
    """Wait until Instagram is in front, tapping through the container Shortcut's own prompts.
    11.10: moved into iphone_farm_wda.wait_for_foreground so iphone_ig's warmup/post/picture/pfp
    (open_account) use the exact same tested prompt-handling instead of a bare activeAppInfo poll
    that timed out doing nothing on every real farm phone."""
    return wait_for_foreground(p, IG_BUNDLE, container, seconds, log=log.info)


def open_fresh_container(device_rec: str, container: str) -> Phone:
    """Shortcut -> Instagram in that container with its proxy up (no logged-in account expected).

    2026-10-20 Rene: wenn Instagram beim Script-Start schon im Vordergrund ist (zB Shortcut wurde
    manuell oder von einem anderen Script-Call vorab gestartet), skippe den Shortcut-Launch — er
    würde Instagram ja schließen + wieder öffnen (= 5-min-Wait im `wait_for_instagram`). Erst
    probieren ob IG bereits da ist; wenn ja, direkt returnen."""
    # 2026-10-20 Rene: Shortcut MUSS immer getippt werden damit Container-Switch stattfindet
    # (sonst bleibt der falsche Container offen von vorherigem Signup). Nur das "wait" skippen
    # wenn Instagram schon im Vordergrund ist nach dem Shortcut (dann weniger warten).
    ok, msg = iphone_farm_config.run_shortcut(device_rec, container)
    if not ok:
        raise FlowError(f"shortcut {container}: {msg}")
    p = connect(device_rec, timeout=60)
    # 11.10, C9 twice in a row: the Shortcut itself (SSH kill, ipify/Shadowrocket checks, several
    # Allow prompts) can eat most of a slow 150 s budget before Instagram even opens, while the
    # phone is otherwise fine (confirmed live: Instagram was sitting on the entry screen right
    # after the timeout fired). Patience over abort -- 240 s total, not a detection change.
    # 12.10, Rene: a silent 240 s single wait is the wrong shape when the Shortcut itself simply
    # never got going (USB/WLAN hiccup, iOS swallowed the uiopen) -- retrigger it again after a
    # short look rather than sit out the whole budget on one launch.
    # 12.10 later, Rene live: 60 s per launch was still too short on a slow night -- C's screen,
    # checked right after a "failed" run gave up, showed Instagram sitting there open and fine,
    # just later than the cap allowed. Worse, relaunching every 60 s likely KILLED slow-but-
    # progressing attempts outright (each relaunch restarts the Shortcut's VPN-switch + IP/DNS
    # check from scratch). Patience over abort -- fewer, much longer looks: 2 launches, 5 min each.
    PER_LAUNCH_S = 300
    up = wait_for_instagram(p, container, PER_LAUNCH_S)
    tries = 1
    while not up and tries < 2:
        tries += 1
        log.info("%s: Instagram not up yet after launch %d -- relaunching the shortcut", container, tries - 1)
        ok, msg = iphone_farm_config.run_shortcut(device_rec, container)
        if not ok:
            raise FlowError(f"shortcut {container}: {msg}")
        up = wait_for_instagram(p, container, PER_LAUNCH_S)
    if not up:
        raise FlowError(f"shortcut {container} did not end in Instagram within {tries * PER_LAUNCH_S} s ({tries} launches)")
    vpn = iphone_farm_config.vpn_state(device_rec)
    if not vpn.get("vpn") or (vpn.get("container") or "").split(" ")[0] != container:
        raise FlowError(f"Instagram is open but the VPN is not {container}'s: {vpn}")
    time.sleep(random.uniform(2, 4))
    return p


ATTEMPTS_PATH = os.path.join(os.path.expanduser("~"), ".adb_bot", "iphone_signup_attempts.json")


def _attempts() -> dict:
    try:
        with open(ATTEMPTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _attempts_save(data: dict) -> None:
    os.makedirs(os.path.dirname(ATTEMPTS_PATH), exist_ok=True)
    tmp = ATTEMPTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, ATTEMPTS_PATH)


def check_budget(info: dict) -> None:
    """12.10, explicit instruction (chat): Rene dropped the "one attempt per account per day"
    cap himself ("Mach diese Sperre raus, ist mir egal ob das einmal fehlschlägt") -- he is
    running signups manually today and wants retries on the same row without waiting for
    tomorrow. The real IG-side signal (a device that already got 'Try Again Later' today) is
    kept: that one is not an arbitrary cap, it is Instagram itself telling this phone to stop.
    IPHONE_SIGNUP_FORCE=1 also bypasses the device-per-day count below (Rene's call only)."""
    today = time.strftime("%Y-%m-%d")
    day = _attempts().get(today, {})
    devices = day.get("devices", {})
    dev = devices.get(info["device"], {})
    if dev.get("rate_limited"):
        raise FlowError(f"device {info['device']} answered 'Try Again Later' today at {dev['rate_limited']} -- no more signups on it today")
    if os.environ.get("IPHONE_SIGNUP_FORCE") == "1":
        return
    if dev.get("attempts", 0) >= int(os.environ.get("IPHONE_SIGNUP_DEVICE_PER_DAY", "10")):
        raise FlowError(f"device {info['device']} already had {dev['attempts']} signup attempts today (cap IPHONE_SIGNUP_DEVICE_PER_DAY)")


def note_attempt(info: dict, rate_limited: bool = False) -> None:
    data = _attempts()
    today = time.strftime("%Y-%m-%d")
    day = data.setdefault(today, {"rows": {}, "devices": {}})
    day["rows"][info["row"]] = time.strftime("%H:%M")
    dev = day["devices"].setdefault(info["device"], {"attempts": 0})
    if rate_limited:
        dev["rate_limited"] = time.strftime("%H:%M")
    else:
        dev["attempts"] = dev.get("attempts", 0) + 1
    for old in [k for k in data if k != today]:
        data.pop(old, None)
    _attempts_save(data)


def run_row(info: dict, dry_run: bool = False) -> dict:
    """Create the account described by `info` (see `describe`). Returns the write-back dict.

    With no alias linked this goes through the phone/SMS-Pool chain instead
    of the email one (see `ready_rows`) -- so an alias is only checked when
    one is actually there; its absence is not itself an error.
    """
    if info["alias"] and info["alias_status"] not in (None, "Free", "Used"):
        raise FlowError(f"alias {info['alias']!r} is {info['alias_status']}")
    if not info["device"]:
        raise FlowError(f"container {info['container']} has no Device linked")
    check_budget(info)
    rng = random.Random()
    ident = make_identity(rng, model=info["model"]) if not info["username"] else None
    if ident is None:
        from adb_bot.automation.flows.signup import Identity
        from adb_bot.automation.signup_identity import MONTHS, _birthday, _password
        d, m, y = _birthday(rng)
        ident = Identity(full_name=info["model"], username=info["username"], password=info["ig_password"] or _password(rng),
                         birth_day=d, birth_month=m if isinstance(m, str) else MONTHS[m - 1], birth_year=y)
    if info["alias"]:
        ident.email = info["alias"]
    # else: leave ident.email unset -- flows.signup.run_signup reads that as
    # "no email", which is exactly what routes it down the phone/SMS chain.
    if not info["username"]:
        # 08.10 live: "Nikki_1480", "nikki_1480", "nikki.1480", "nikki.7620" -> "Input Username is invalid.";
        # "nikkilovesyou98", "nikkisunnyside" -> valid. Model name + a word + two digits, lowercase.
        words = ("sunnyside", "andcoffee", "goesplaces", "daydreams", "afterdark", "inbloom", "offduty", "ontheroad", "lovesyou", "atdawn")
        ident.username = f"{info['model'].lower()}{rng.choice(words)}{rng.randint(10, 99)}"
    ident.username = ident.username.lower()
    log.info("%s on %s: %s", info["container"], info["device"], ident.summary())
    # 2026-10-20 Rene: Password IMMER loggen (nicht nur bei STOP_AFTER=password). Falls Signup
    # irgendwo mittendrin failed oder Script crasht, muss das PW sichtbar sein damit Rene
    # den Account nicht per Reset neu setzen muss. ("Natürlich musst du alles speichern")
    log.info("%s: password for this account: %s  (handoff / crash backup)", info["container"], ident.password)
    if dry_run:
        return {"dry_run": True, "identity": ident.summary()}
    # 08.10: the farm watchdog "wakes" phones by opening Settings and pressing HOME, which killed
    # every signup mid-way. Mark the phone as off/busy for the watchdog while we drive it.
    info["_ident"] = ident
    try:
        iphone_farm_config._set_off(info["device"], True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not mark %s busy for the watchdog: %s", info["device"], exc)
    note_attempt(info)                       # counts even if the run dies -- Instagram saw it
    try:
        out = _run_row_driven(info)
        res = out.get("result")
        if res is not None and "limit how often" in str(getattr(res, "detail", "")):
            note_attempt(info, rate_limited=True)
        return out
    except Exception as exc:                          # noqa: BLE001
        # 22.10 (Rene): a crash outside the flow's own screen reading still leaves a picture behind
        from adb_bot.automation import iphone_diag
        shot = iphone_diag.keep_screen(info["device"], "signup-crash", f"{type(exc).__name__}: {exc}",
                                       container=info.get("container") or "", log_fn=log.info)
        log.error("%s/%s: signup crashed: %s (screen: %s)", info["device"], info.get("container"), exc, shot or "none")
        raise
    finally:
        try:
            iphone_farm_config._set_off(info["device"], False)
        except Exception:  # noqa: BLE001
            pass


# 13.10, Rene (standing rule): the SIGNUP runs on WDA -- everything outside Instagram and the signup itself.
# Everything inside Instagram afterwards (posting, stories, profile) runs on XXTouch. Only when the phone's WDA
# is not up does the signup fall back to XXTouch. (Until 13.10 a fixed letter list A/B/G/H/I was XX-only from a
# 12.10 snapshot of dead WDAs; checked live 13.10: B, C, E answer "ready", so the list was stale -- ask WDA.)
def _wda_ready(device_rec: str) -> bool:
    phone = iphone_farm_config.load().get(device_rec) or {}
    port = phone.get("wda_port")
    if not port:
        return False
    host = iphone_farm_config.relay()["ssh"].split("@")[-1]          # iproxy listens on the relay's tailnet IP
    try:
        r = requests.get(f"http://{host}:{port}/status", timeout=8)
        if not (r.json().get("value") or {}).get("ready"):
            return False
        # 13.10 E24/E25 live: /status said ready while every real call hung for minutes (slow farm WLAN) and the
        # signup sat on the home screen. Ask one real thing; no quick answer = treat WDA as down -> XXTouch.
        requests.get(f"http://{host}:{port}/wda/activeAppInfo", timeout=10).raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        return False


def _run_row_driven(info: dict) -> dict:
    pref = os.environ.get("IPHONE_SIGNUP_DRIVER", "auto")
    if pref == "xx":
        use_xx = True
    elif pref == "wda":
        use_xx = False
    else:
        use_xx = not _wda_ready(info["device"])
        log.info("%s: signup driver %s (WDA %s)", info["container"], "XXTouch" if use_xx else "WDA",
                 "down" if use_xx else "ready")
    out = _run_row_driven_xx(info) if use_xx else _run_row_driven_wda(info)
    res = out.get("result")
    detail = str(getattr(res, "detail", "")) if res is not None else ""
    # 14.10 live (planner test on C15): SMS-Pool sold nothing and the run ended "no_number -- 0 numbers, none
    # delivered" -- the "could rent a number" wording only comes from the router's own exception path, so the
    # fallback never fired. Both mean the same: no number to rent -> iCloud alias.
    no_rent = "could rent a number" in detail or (getattr(res, "status", "") == "no_number" and detail.startswith("0 numbers"))
    if res is not None and no_rent and not info.get("alias"):
        # 13.10, Rene (standing rule): SMS-Pool first; no number to rent -> ALWAYS fall back to an iCloud alias.
        claimed = claim_fresh_alias()
        if claimed and claimed.get("email"):
            log.info("%s: SMS-Pool had no number -- iCloud alias %s instead", info["container"], claimed["email"])
            info["alias"], info["alias_id"] = claimed["email"], claimed["id"]
            info["_ident"].email = claimed["email"]
            out = _run_row_driven_xx(info) if use_xx else _run_row_driven_wda(info)
        else:
            log.warning("%s: SMS-Pool had no number and no free iCloud alias either", info["container"])
    return out


def _run_row_driven_xx(info: dict) -> dict:
    """Same `flows.signup.run_signup` state machine as `_run_row_driven_wda`, but with
    `IphoneSignupDriverXX` (screen + taps + typing all through XXTouch, no WDA at all) --
    for phones whose WDA cannot be relied on to even open a session."""
    from adb_bot.automation.iphone_signup_xx import IphoneSignupDriverXX
    from adb_bot.automation.iphone_xx_flows import XX

    ident = info["_ident"]
    x = XX(info["device"], log=log.info)
    # 13.10 E22: a NEW container's first Shortcut run waits on three "Allow" taps by a person -- 240 s ran out
    x.open_container(info["container"], timeout=480)
    vpn = iphone_farm_config.vpn_state(info["device"])
    if not vpn.get("vpn") or (vpn.get("container") or "").split(" ")[0] != info["container"]:
        raise FlowError(f"Instagram is open but the VPN is not {info['container']}'s: {vpn}")
    time.sleep(random.uniform(2, 4))
    from adb_bot.automation.iphone_signup_xx8 import SMALL_PHONE_LETTERS, IphoneSignupDriverXX8
    # 13.10 Rene: iPhone 7/8 (G/H/I) run their own driver with their own screen numbers
    cls = IphoneSignupDriverXX8 if info["container"][:1] in SMALL_PHONE_LETTERS else IphoneSignupDriverXX
    driver = cls(x, info["device"], info["container"], logger=log.info)
    driver.identity = ident
    mailbox = AliasMailbox(info["alias"]) if info["alias"] else None
    try:
        from adb_bot.clients.sms.router import build_router
        router = build_router(logger=log)
    except Exception as exc:  # noqa: BLE001
        log.warning("no SMS router (%s) -- email chain only", exc)
        router = None
    try:
        res = S.run_signup(driver, router, ident, logger=log, mailbox=mailbox, country="US")
    except RateLimited as exc:
        res = S.SignupResult(status=S.RESULT_STUCK, detail=str(exc), username=ident.username, steps=["rate_limited"])
    log.info("result: %s %s steps=%s", res.status, res.detail, res.steps)
    return {"result": res, "identity": ident}


def _run_row_driven_wda(info: dict) -> dict:
    ident = info["_ident"]
    p = open_fresh_container(info["device"], info["container"])
    touch = None
    if os.environ.get("IPHONE_SIGNUP_TOUCH", "xxtouch") == "xxtouch":
        try:
            touch = iphone_xxtouch.for_phone(info["device"])
            touch.wait_idle(5)
            probe = touch._post("/spawn", b"touch.init(0)\nsys.msleep(1)\n")
            if probe.get("code") == 403:
                # 09.10: the service forgets the licence after a respring/reinstall although the app
                # has it cached -- opening the XXTouch app once hands it over again (B: 403 -> 0)
                log.info("XXTouch says 'requires a valid license' -- opening XXTExplorer once to re-sync")
                p.launch("ch.xxtou.XXTExplorer")
                time.sleep(8)
                back_to_instagram(p)
                time.sleep(2)
                probe = touch._post("/spawn", b"touch.init(0)\nsys.msleep(1)\n")
            if probe.get("code") not in (0, None):
                raise RuntimeError(f"XXTouch probe: {probe}")
            time.sleep(1.5)
            lic = p.find("predicate string", "label CONTAINS 'License Restricted' OR label CONTAINS 'not authorized to use' AND visible == 1")
            if lic is not None:
                # 08.10: "E430: This device is not authorized to use X.X.T.E." -- the licence check runs
                # through the container's proxy and fails; the dialog sends Instagram to the background
                log.warning("XXTouch licence restricted on %s (E430) -- WDA taps for this run", info["device"])
                for lab in ("OK", "Close", "Dismiss"):
                    b = p.find("predicate string", f"type == 'XCUIElementTypeButton' AND label == '{lab}' AND visible == 1")
                    if b:
                        b.click()
                        break
                back_to_instagram(p)
                time.sleep(2)
                touch = None
        except Exception as exc:  # noqa: BLE001
            log.warning("XXTouch not reachable (%s) -- falling back to WDA taps for this run", exc)
            touch = None
    else:
        log.info("IPHONE_SIGNUP_TOUCH=wda: WDA taps for this run")
    driver = IphoneSignupDriver(p, info["device"], info["container"], touch, logger=log)
    driver.identity = ident
    mailbox = AliasMailbox(info["alias"]) if info["alias"] else None
    try:
        from adb_bot.clients.sms.router import build_router
        router = build_router(logger=log)
    except Exception as exc:  # noqa: BLE001
        log.warning("no SMS router (%s) -- email chain only", exc)
        router = None
    try:
        # 10.10, explicit instruction (chat): buy US numbers for this fleet, not
        # DE -- flows.signup's PICKER_COUNTRY="DE" default assumes the picker
        # opens on Germany, but on this phone it opens on China; passing a
        # country that differs from PICKER_COUNTRY makes the flow type the
        # number in full e164 (with its + code) instead of relying on the
        # picker's default, which is what actually fixes it, not changing the
        # picker's selection.
        res = S.run_signup(driver, router, ident, logger=log, mailbox=mailbox, country="US")
    except RateLimited as exc:
        res = S.SignupResult(status=S.RESULT_STUCK, detail=str(exc), username=ident.username, steps=["rate_limited"])
    log.info("result: %s %s steps=%s", res.status, res.detail, res.steps)
    return {"result": res, "identity": ident}


def write_back(info: dict, out: dict) -> None:
    res, ident = out["result"], out["identity"]
    stamp = time.strftime("%d.%m. %H:%M")
    note = f"[{stamp}] iPhone-Signup {info['container']}: {res.status}"
    if res.detail:
        note += f" -- {res.detail[:200]}"
    note += f" (Schritte: {', '.join(res.steps[-8:])})"
    # Which chain actually ran: an alias was linked (email chain) or not (phone/SMS-Pool
    # chain, 10.10) -- `info["alias"]` is what run_row based ident.email on, so it is the
    # same signal here. A post-signup "Add an email address" alias (claim_fresh_alias,
    # stamped onto ident by _handle_add_email) does not change which chain created the
    # account -- it is recorded separately below.
    method = "Icloud" if info["alias"] else "SMS-Pool / Virtual Number"
    # The alias to link/claim on the Account row: the one it started with, or -- SMS-Pool
    # chain, 10.10 -- the one _handle_add_email claimed live during the run.
    alias_id = info["alias_id"] or getattr(ident, "alias_id", None)
    fields = {A["notes"]: note, A["signup_method"]: method, A["email"]: ident.email or ""}
    if alias_id and alias_id != info["alias_id"]:
        fields[A["alias"]] = [alias_id]
    if res.status in (S.RESULT_CREATED, S.RESULT_CREATED_UNVERIFIED):
        fields.update({A["username"]: ident.username, A["ig_password"]: ident.password,
                       A["creation"]: CS_CREATED if res.status == S.RESULT_CREATED else CS_STUCK})
        if res.status == S.RESULT_CREATED_UNVERIFIED:
            fields[A["status"]] = "Human Verification"
        if alias_id:
            _patch(T_ALIAS, alias_id, {AL["status"]: "Used"})
        _patch(T_CONT, info["container_id"], {C["status"]: "Belegt"})
    elif res.status in (S.RESULT_BANNED,):
        fields.update({A["username"]: ident.username, A["ig_password"]: ident.password, A["creation"]: CS_STUCK, A["status"]: "Banned"})
        if alias_id:
            _patch(T_ALIAS, alias_id, {AL["status"]: "Burned"})
    elif "terms" in res.steps:
        # 11.10, C9: "terms" in steps means the "I agree" tap fired -- the account exists
        # even when a later, unrecognised post-creation screen reads as unknown_screen/stuck
        # (confirmed live: @nikkiatdawn16 landed on the real feed and still got classified
        # this way). Same "keep the credentials, flag it Stuck" shape as RESULT_BANNED above,
        # so the account is not stranded with no way to log back in.
        fields.update({A["username"]: ident.username, A["ig_password"]: ident.password,
                       A["creation"]: CS_STUCK})
    else:
        fields[A["creation"]] = CS_STUCK
    old = _get(T_ACC, info["row"])["fields"].get(A["notes"]) or ""
    fields[A["notes"]] = (old + "\n" + note).strip()
    _patch(T_ACC, info["row"], fields)


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("row", help="Accounts record id or username")
    r.add_argument("--dry-run", action="store_true")
    sc = sub.add_parser("screen")
    sc.add_argument("device")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        rows = ready_rows()
        for row in rows:
            try:
                i = describe(row)
                print(f"{i['row']}  {i['container']:>4} on {i['device']}  alias {i['alias']} ({i['alias_status']})  model {i['model']}  username {i['username'] or '(auto)'}")
            except Exception as exc:  # noqa: BLE001
                print(f"{row['id']}  NOT READY: {exc}")
        if not rows:
            print("no row is ready (To Do + Container + Model; Mail-Alias optional)")
        return 0
    if a.cmd == "screen":
        p = connect(a.device)
        d = IphoneSignupDriver(p, a.device, "", None)
        text = d.read_screen()
        print(text)
        print("--- screen:", S.classify_signup_screen(text))
        return 0
    rows = [x for x in ready_rows() if x["id"] == a.row or (x["fields"].get(A["username"]) or "").lstrip("@").lower() == a.row.lstrip("@").lower()]
    if not rows:
        print(f"{a.row}: not among the ready rows (run `list`)")
        return 1
    info = describe(rows[0])
    out = run_row(info, dry_run=a.dry_run)
    if a.dry_run:
        print(out)
        return 0
    write_back(info, out)
    print(out["result"].status, out["result"].detail)
    return 0 if out["result"].ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
