"""`flows.signup.SignupDriver` on an iPhone, built entirely on XXTouch (no WDA).

12.10, Rene: the WDA-based driver (`iphone_signup.IphoneSignupDriver`) depends on
WebDriverAgent for *reading* the screen (element tree, `active_app()`) even though it
already taps through XXTouch when available -- and WDA needs a much deeper, flakier USB
connection (go-ios launching a real XCUITest runner on the device) than XXTouch's own
SSH-tunneled HTTP API, which keeps working fine over WiFi when WDA is down (confirmed
live 12.10: A/B's WDA dead or crash-looping all afternoon, their XXTouch never was).

This driver reads the screen with `XX.ocr()` (Apple Vision OCR, same as `iphone_xx_flows`)
instead of the WDA element tree. Trade-off: OCR gives text + position, not a typed element
tree, so there is no FIELD_TYPES check and no secure-field masking -- `fill()` finds a
field by tapping near its hint label instead of an exact element lookup. First version,
meant to be driven live and corrected (Rene: "lernst es an") rather than trusted blind on
a real account yet -- try it on one phone, watch what it actually does, fix what's wrong.
"""

from __future__ import annotations

import random
import re
import time

from adb_bot.automation import iphone_farm_config
from adb_bot.automation.iphone_xx_flows import XX, FlowError, _dismiss_popups

IG_BUNDLE = "com.burbn.instagram"

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def _norm(s: str) -> str:
    return " ".join((s or "").replace("'", "'").split()).strip().lower()


class IphoneSignupDriverXX:
    # Screen geometry in XX units (OCR/tap coordinates) for the iPhone X family (A-F). The iPhone 7/8 phones
    # (G/H/I, smaller panel) have their own driver with their own numbers: iphone_signup_xx8.py (Rene 13.10:
    # "lieber einen eigenen Code machen", not switches inside this one).
    SCREEN = (375.0, 812.0)
    FIELD_GAP = 28           # field sits this far below its label/placeholder
    CODE_GAP = 85            # the 6 code boxes sit this far below the body text
    """`flows.signup.SignupDriver`: screen + taps + typing all through XXTouch."""

    def __init__(self, x: XX, device_rec: str, container: str, logger=None):
        self.x, self.device_rec, self.container = x, device_rec, container
        self.logger = logger or print
        self._words: list[dict] = []
        self.restarts = 0
        self.identity = None

    # -- screen -----------------------------------------------------------------
    def read_screen(self) -> str:
        _dismiss_popups(self.x)
        front = self.x.front()
        if front and front != IG_BUNDLE:
            self._words = []
            self.logger(f"{front} is in front, not Instagram")
            return ("google play store telephone messaging camera gallery (iOS home screen)"
                     if front == "com.apple.springboard" else
                     f"google play store sign in to find the latest android apps, games, movies, music (iOS: {front} in front)")
        self._words = sorted(self.x.ocr(), key=lambda w: (round(w["y"] / 14), w["x"]))
        texts = [w["text"] for w in self._words if w["text"].strip()]
        text = "\n".join(texts)
        self.logger("screen: " + " | ".join(texts)[:400])
        # 13.10 E19 live: OCR breaks one sentence over two items ("Make sure your device is" / "nearby"), so a
        # marker spanning the break never matched and the run restarted Instagram, throwing the number away.
        low = " ".join(text.lower().split())
        # 12.10, A8/A10 live: the container's own "Allow ... to connect to ..." one-time prompt
        # (iphone_xx_flows.XX._allow_ssh_script_prompt handles it before Instagram opens) can
        # ALSO fire again mid-signup -- its own IP-check script keeps running after handing
        # control to Instagram. read_screen had no idea what to do with the merged text (the
        # dialog's own words plus whatever screen sits behind it) and gave up as unknown_screen,
        # with "Allow"/"Erlauben" sitting right there unclicked both times. Same tap, just reused
        # here instead of only in open_container's own wait loop.
        if ("to connect to" in low or "run an ssh script" in low or "run a shell script" in low) and self.x._allow_ssh_script_prompt():
            self.x.pause(1, 1.5)
            return self.read_screen()
        if "would like to paste from" in low and self.x.tap_text(r"^(Allow Paste|Einsetzen erlauben)$", 3):
            # 13.10 E22 live: the paste dialog came up after fill()'s own look and then sat over the password
            # screen for three rounds -- every retype only opened another one. Answer it wherever it shows.
            self.logger("paste dialog -> Allow Paste")
            self.x.pause(1, 1.5)
            return self.read_screen()
        for marker, button in (("please turn on notifications", "Not now"), ("add a profile picture", "Skip"),
                               ("allow instagram to use your contacts", "Not now"), ("turn on contacts", "Not now"),
                               ("turn on push notifications", "Not now")):
            if marker in low and self.tap_label((button,)):
                self.logger(f"post-signup prompt {marker!r} -> {button}")
                self.x.pause(1.5, 2.5)
                return self.read_screen()
        if "make sure your device is nearby" in low and self.tap_label(("Continue",)):
            self.logger("device-nearby interstitial -> Continue")
            self.x.pause(1.5, 2.5)
            return self.read_screen()
        if self.identity is not None and "how old are you" in low and self.tap_label(("Age",)):
            # 13.10 H2 live: after a birthday Instagram rejects ("It looks like you entered the wrong info") it
            # asks for the age instead -- unknown screen, so the run restarted Instagram and lost the signup.
            import datetime as _dt
            age = _dt.date.today().year - int(self.identity.birth_year) - 1
            self.x.pause(0.8, 1.2)
            self.x.type(str(age))
            self.x.pause(1.5, 2)
            self._allow_paste()
            self.logger(f"age screen -> {age}")
            if self.tap_label(("Next",)):
                self.x.pause(2, 3)
            return self.read_screen()
        if "we limit how often you can do certain things" in low:
            from adb_bot.automation.iphone_signup import RateLimited
            raise RateLimited("Instagram: 'Try Again Later -- we limit how often you can sign up' on this IP/container")
        _age = re.search(r"\(\s*([0-9oO]+)\s+years? old", low)
        _age = int(_age.group(1).replace("o", "0")) if _age and _age.group(1).replace("o", "0").isdigit() else None
        # 13.10: only turn the wheels while the date is NOT already adult -- "years old" alone matched every read
        # of the screen and E23 re-checked an adult date forever without ever tapping Next. H: OCR "(O years old)".
        if self.identity is not None and "what's your birthday" in low and _age is not None and _age < 19:
            if self.set_date(self.identity.birth_day, self.identity.birth_month, self.identity.birth_year):
                self.x.pause(1, 1.5)
                return self.read_screen()
        return text

    # -- tapping ------------------------------------------------------------------
    def _tap_word(self, w: dict, what: str = ""):
        self.logger(f"tap {what} at ({w['x']:.0f}, {w['y']:.0f})")
        self.x.tap(w["x"], w["y"])
        time.sleep(random.uniform(0.6, 1.3))

    def tap_label(self, labels) -> bool:
        if not self._words:
            self.read_screen()
        wanted = [_norm(l) for l in labels]
        for w in self._words:
            if _norm(w["text"]) in wanted:
                self._tap_word(w, repr(w["text"]))
                self._words = []
                return True
        if any(_norm(l) == "back" for l in labels):
            # 13.10 E17/E22 live: iOS shows the back button as a bare "<" glyph, never the word "Back", so the
            # number swap after a code that never came ended as "code did not advance in 4 tries".
            arrow = next((w for w in self._words if w["text"].strip() in ("<", "‹", "〈") and w["y"] < 160), None)
            if arrow:
                self._tap_word(arrow, "'<' (Back)")
                self._words = []
                return True
        self.logger(f"none of {list(labels)} on screen")
        return False

    # -- typing -----------------------------------------------------------------
    def _field_guess(self, hints) -> dict | None:
        """No element tree to ask 'is this a text field' -- find the hint label by OCR
        and guess the field sits in the next ~60 pt below it (every iOS signup screen
        this flow drives puts the label directly above its field).

        12.10, A8/B13 live: taking the FIRST word containing the hint picked the screen's
        own heading ("What's your email?") or body text ("Enter the email where you can be
        contacted...") over the real field placeholder ("Email") -- both contain "email" too
        and sort above it. The tap landed in the paragraph, sys.input_text typed into nothing,
        and the field stayed empty ("does not hold ... after typing") both times. The actual
        placeholder is always the SHORTEST matching text on screen, heading/body never are --
        pick that instead of the first hit.

        12.10, B9 live: the confirmation-code screen has no separate short placeholder at all
        (just "Enter the confirmation code" / a 2-line body sentence / the field), so even after
        excluding the "I didn't get the code" link below it, SHORTEST still picked the heading
        over the longer body text and tapped partway into the body paragraph -- confirmed by
        three straight code-field misses (A10, B9, B13) that were not an OCR misread (the code is
        all digits, nothing to confuse). Every screen actually driven here puts the real
        label/placeholder as the LAST matching line before the field, not the shortest one --
        "Email"/"Mobile number"/"Username" all sort after their own heading+body too. Picking the
        BOTTOMMOST matching candidate instead fixes the code screen without disturbing any of
        those (still excluding the "didn't"/resend link, which sits even lower, below the field
        itself)."""
        if not self._words:
            self.read_screen()
        hints = [_norm(h) for h in hints]
        best = None
        for w in self._words:
            hay = _norm(w["text"])
            if "didn't" in hay or "resend" in hay:
                continue
            if any(h in hay for h in hints) and (best is None or w["y"] > best["y"]):
                best = w
        if best is None:
            return None
        return {"x": best["x"], "y": best["y"] + self.FIELD_GAP, "label_y": best["y"]}

    def _allow_paste(self):
        """12.10 live, B13: the paste-permission dialog sys.input_text triggers renders in the
        PHONE's system language, not Instagram's own (confirmed: Instagram's UI was English on
        this phone while the paste dialog showed "'Instagram' moechte von 'lua' einsetzen /
        Einsetzen erlauben" -- matching only the English "Allow Paste" left it never dismissed,
        so the typed value never actually landed and field_holds() found the field still empty).
        The fleet is not uniformly set to English, so match every language seen so far."""
        if self.x.tap_text(r"^(Allow Paste|Einsetzen erlauben)$", 2):
            self.x.pause(1, 1.5)

    def fill(self, hints, value: str, what: str, submits_itself: bool = False) -> bool:
        f = self._field_guess(hints)
        if f is None:
            self.logger(f"no field for {what} ({list(hints)})")
            return False
        if what == "confirmation code":
            # 12.10, A8/A10/B9/B13 live (screenshot confirmed): the code field is 6 boxes
            # sitting well below the body paragraph -- a lot further than the ~28pt gap every
            # other field on this flow uses. The standard offset landed in the dead space
            # between the paragraph and the boxes, focused nothing, and the digits went
            # nowhere (6/6 misses before this). Widen the tap point for this one field.
            f = {**f, "y": f["label_y"] + self.CODE_GAP}
        self.x.tap(f["x"], f["y"])
        self.x.pause(0.6, 1.0)
        # clear whatever is there first -- OCR cannot tell "placeholder" from "typed value",
        # so always try the on-screen clear ("x") button once before typing.
        if what != "confirmation code":
            self._clear_field(f)
        self.x.type(value)
        self.x.pause(1, 1.5)
        self._allow_paste()
        self.x.pause(0.5, 0.8)
        self._words = []
        ok = self.field_holds(hints, value)
        if not ok and submits_itself:
            # 12.10, A8 live: the code field can auto-submit THIS fast -- by the time
            # field_holds() re-read the screen, Instagram had already moved on to "Create a
            # password", so the check correctly found no trace of the code (it was typed fine,
            # the screen just isn't showing it any more) and called it a miss. A field that
            # "submits_itself" leaving its own screen entirely is what success looks like, not
            # failure -- only a real miss leaves the SAME screen sitting there unchanged.
            still_here = any("confirmation code" in _norm(w["text"]) or "6-digit code" in _norm(w["text"])
                              for w in self._words)
            if not still_here:
                self.logger(f"{what}: screen moved on after typing -- reading that as submitted, not missed")
                return True
        if not ok and what in ("mobile number", "password", "username"):
            # 13.10 E19/E20 live: the FIRST number of every run came back "field does not hold" and was thrown
            # away, the second always went in -- the paste dialog shows up after our one quick look. Look again
            # longer, then retype once, before giving a perfectly good rented number back.
            self.logger(f"{what}: not in the field yet -- second look for the paste dialog, then one retype")
            if self.x.tap_text(r"^(Allow Paste|Einsetzen erlauben)$", 6):
                self.x.pause(1, 1.5)
            self._words = []
            ok = self.field_holds(hints, value)
            if not ok:
                self._clear_field(f)          # 13.10 Rene: password/number sometimes went in twice -- empty it first
                self.x.type(value)
                self.x.pause(1.5, 2.0)
                if self.x.tap_text(r"^(Allow Paste|Einsetzen erlauben)$", 6):
                    self.x.pause(1, 1.5)
                self._words = []
                ok = self.field_holds(hints, value)
        if not ok:
            self.logger(f"{what}: field does not hold {'***' if what == 'password' else value!r} after typing")
        return ok

    def _clear_field(self, f: dict) -> None:
        """Empty the field at `f`: tap its right end (cursor after any text) and send backspaces.
        13.10 E19/H2 live: the clear button OCRs as "X"/"©"/nothing and was never found; sys.input_text("\b")
        pastes instead of deleting. key.send_text("\b") really deletes (tested on H: a pre-filled username
        suggestion went away), so a retry or a suggestion can no longer end up in front of our value."""
        self.x.tap(self.SCREEN[0] * 0.8, f["y"])
        self.x.pause(0.5, 0.8)
        self.x.lua_out('key.send_text(string.rep("\\b", 60)) OUT = "ok"')
        self.x.pause(0.4, 0.7)

    @staticmethod
    def _ocr_fuzzy(s: str) -> str:
        """12.10, B13 live: the alias 'gauche_clank.0t@icloud.com' (digit zero) was typed
        correctly but OCR'd back as '...Ot@icloud.com' (letter O) -- a well-known Vision OCR
        confusion on these generated, random-looking local parts. Collapsing the usual
        look-alikes before comparing turns that into a pass instead of a false 'did not hold'
        that aborted a perfectly good signup."""
        # 24.10 (A1/A11 live, "could not get monk.fleeced_7l@icloud.com into the email field"): OCR also drops or
        # swaps the underscore/dot in these generated addresses -- compare letters and digits only.
        return re.sub(r"[^a-z0-9@]", "", _norm(s).replace("0", "o").replace("1", "l").replace("i", "l").replace("5", "s"))

    def field_holds(self, hints, value: str) -> bool:
        self.read_screen()
        hints_n = [_norm(h) for h in hints]
        for w in self._words:
            if _norm(value) in _norm(w["text"]):
                return True
        fuzzy_value = self._ocr_fuzzy(value)
        for w in self._words:
            if fuzzy_value in self._ocr_fuzzy(w["text"]):
                return True
        if value.isdigit() and len(value) >= 4:
            # 24.10 (B10 live, and A10/B9/B13 on 12.10): the confirmation code sits in 6 separate boxes, so the
            # OCR returns six one-digit words -- the whole code never appears inside ONE word. Join the digits
            # of each text row and look for the code there.
            rows: dict[int, list[dict]] = {}
            for w in self._words:
                if w["text"].strip().isdigit():
                    rows.setdefault(round(w["y"] / 14), []).append(w)
            for ws in rows.values():
                joined = "".join(w["text"].strip() for w in sorted(ws, key=lambda w: w["x"]))
                if value in joined:
                    return True
        if "@" in value and len(value.split("@")[0]) >= 8:
            # a long address is shown cut off ("monk.fleeced_7l@icl...") -- the local part alone is proof enough
            local = self._ocr_fuzzy(value.split("@")[0])
            if any(local in self._ocr_fuzzy(w["text"]) for w in self._words):
                return True
        return False

    def dismiss_keyboard(self) -> None:
        if not self._words:
            self.read_screen()
        for w in self._words:
            if _norm(w["text"]) in ("done", "fertig"):
                self._tap_word(w, "keyboard done")
                self._words = []
                return
        width, height = self.x.size()
        self.x.tap(width * 0.4, height * 0.12)
        time.sleep(random.uniform(0.6, 1.0))
        self._words = []

    def press_enter(self) -> None:
        # XXTouch has no WDA-style "send return key" -- the flow's own tap_label("Next")
        # is the real submit path on every screen seen so far; this is a best-effort
        # fallback that tries whatever looks like a keyboard return/go/next key.
        if not self.tap_label(("return", "Return", "Next", "Go", "Join")):
            self.logger("press_enter: no return-like key found -- relying on the flow's own Next tap")
        self._words = []

    def submit_with_keyboard(self) -> None:
        self.press_enter()

    # -- birthday wheel -----------------------------------------------------------
    def set_date(self, day: int, month: str, year: int) -> bool:
        """iOS birthday wheels. Rene 13.10: "einfach nach unten drehen und irgendein Datum ueber 18" -- the
        exact day never mattered, it only cost minutes (each OCR read ~12-30 s) and failed on every run E13-E18.
        So: turn ONLY the year wheel down until the date line says the person is an adult (19-30 years old, so
        a rounding day never lands on 17), keep whatever month/day the wheels show. `year` is the aim.

        13.10 live, why the old wheel code never moved anything: it swiped at y=400 while the wheels sit at
        ~79 % of the screen height, read "(N years old), Month D, YYYY" as ONE OCR item (it comes as two), and
        dragged the wrong way (finger DOWN brings earlier years into the middle)."""
        import datetime as _dt
        this_year = _dt.date.today().year
        w_scr, h_scr = self.SCREEN
        for _ in range(15):
            words = self.x.ocr()
            m = re.search(r"\b([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})\b", " | ".join(w["text"] for w in words))
            if not m:
                time.sleep(1.0)
                continue
            cur_year = int(m.group(3))
            age = this_year - cur_year
            if 19 <= age <= 35:
                self.logger(f"set_date: {m.group(0)} ({age}) -- adult, keeping it")
                return True
            wheel = [w for w in words if w["y"] > h_scr * 0.5]             # the picker, not the date line
            yr = next((w for w in wheel if w["text"].strip() == str(cur_year)), None)
            prev = next((w for w in wheel if w["text"].strip() == str(cur_year - 1)), None)
            wheel_y = yr["y"] if yr else h_scr * 0.79
            col_x = yr["x"] if yr else w_scr * 0.75
            row = abs(yr["y"] - prev["y"]) if yr and prev and abs(yr["y"] - prev["y"]) > 8 else h_scr * 0.039
            delta = cur_year - year                                          # >0: go to earlier years
            n = max(1, min(abs(delta), 10))
            down = delta > 0
            start = wheel_y - 2.2 * row if down else wheel_y + 2.2 * row
            # 13.10 H3 live (iPhone 8): 10 rows from y=313 ended at y=486 on a 445-high screen -- a swipe that
            # leaves the screen never moved the wheel. Keep the whole drag on the screen.
            room = (h_scr * 0.97 - start) if down else (start - h_scr * 0.55)
            n = max(1, min(n, int(room // row)))
            self.logger(f"set_date: {m.group(0)} ({age}) -> year wheel x={col_x:.0f} y={wheel_y:.0f} "
                        f"{'down' if down else 'up'} {n} rows")
            self.x.swipe(col_x, start, col_x, start + (n * row if down else -n * row), 0.6 + 0.05 * n)
            self.x.pause(1.2, 1.6)                                          # let the wheel settle before reading
        self.logger(f"set_date: no adult year reached (aim {year})")
        return False

    # -- recovery -----------------------------------------------------------------
    def keep_screen(self, reason: str, detail: str = "") -> str | None:
        """22.10 (Rene): picture + on-screen text of where the signup got stuck -> /opt/adbbot/logs/screens/."""
        from adb_bot.automation import iphone_diag
        return iphone_diag.keep_screen(self.x.ref, f"signup-{reason}", detail, container=self.container,
                                       xx=self.x, log_fn=self.logger)

    def restart_app(self) -> bool:
        # Rene 2026-10-20: Nie Instagram killen wenn Terms grad akzeptiert wurden (= Account live).
        # Siehe iphone_signup.py restart_app() -- derselbe Guard.
        if getattr(self, "terms_agreed", False):
            self.logger(f"restart via shortcut SKIPPED -- Terms were agreed, account should be created, do not kill IG now")
            return True
        if self.restarts >= 2:
            return False
        self.restarts += 1
        self.keep_screen("restart-app", f"restart {self.restarts}")
        # 12.10 (Rene): Instagram is ONLY ever opened by the container Shortcut (it sets Shadowrocket to Proxy
        # and checks IP/country/DNS first). This used to app.run Instagram directly before the Shortcut.
        ok, msg = iphone_farm_config.run_shortcut(self.device_rec, self.container)
        self.logger(f"restart via shortcut {self.container}: {ok} {msg[:60]}")
        up = False
        if ok:
            end = time.monotonic() + 120
            while time.monotonic() < end:
                time.sleep(3)
                if self.x.front() == IG_BUNDLE:
                    up = True
                    break
        time.sleep(3)
        self._words = []
        return up
