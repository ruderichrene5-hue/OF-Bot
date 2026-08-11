"""The device half of the verification flow: a real `ChallengeDriver` over ADB.

`flows/verification.py` decides *what* to do with a challenge screen. This is
the half that actually touches a phone -- read the screen, tap the SMS option,
type a number, type a code, screenshot the captcha. It is deliberately a
separate module: the orchestration is pure and unit-tested against a fake
driver, and everything that can only be checked with a handset in front of you
lives here.

**Every screen is recorded.** A verification run is the hardest thing in this
repo to debug after the fact: it is rare, it costs money, it happens on somebody
else's flagged account, and by the time anyone looks the screen is long gone.
So each pass writes the UI dump, a screenshot and the extracted text into a
per-run folder under `~/.adb_bot/verification/`, alongside a `screens.jsonl`
giving the classification of each one. When a run ends as `stuck` or
`needs_human`, that folder is the answer to "what was it actually looking at?" --
and it is also how the marker lists in `verification.py` get corrected from real
screens instead of guesses (TODO 3.2).

**Observation mode.** Constructed with `act=False` the driver reads and records
but never taps, types or rents anything: every action logs what it *would* have
done and returns False. That is what makes it safe to point at a live flagged
profile before the selectors have ever been confirmed -- see
`verification_probe.py`, which is the intended way to run this the first time.

**The house rule applies.** Buttons are tapped only on an EXACT label match
taken from a UI dump, never from OCR, so "Next" can never be confused with
"Not now". Screen *text* may come from OCR when the screen refuses to dump, but
text only ever decides what kind of screen this is -- it never decides where to
tap.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.automation.flows import verification
from adb_bot.automation.flows.interruptions import _dump_text
from adb_bot.core.adb_commands import back, tap, write_text
from adb_bot.core.proc import run as run_hidden

# Where a run's screens are kept. One folder per run, never cleaned up
# automatically -- these are small (a dump is ~100KB, a screenshot ~200KB) and
# they are the only record of a screen that no longer exists.
RUN_ROOT = Path.home() / ".adb_bot" / "verification"

# How long to let a screen settle after a tap before reading it again.
SETTLE_SECONDS = 2.5

# Buttons that advance a verification screen, in preferred order. EXACT matches.
# "next" and "continue" lead because they are the primary action; "confirm" and
# "submit" follow; "done" is last because it also appears on screens that are
# already finished.
_SUBMIT_LABELS = (
    "next", "continue", "confirm", "submit", "send", "verify", "done", "ok",
)

# The SMS option on the "how do you want to get the code?" chooser. Exact match
# again -- "email" must never win because it happens to contain a substring.
_SMS_OPTION_LABELS = (
    "text message", "send sms", "sms", "text me", "phone", "mobile number",
    "send code to phone", "text",
)

# Getting from the code screen back to the phone screen.
_NEW_NUMBER_LABELS = (
    "change number", "use a different number", "change phone number",
    "i didn't get the code", "didn't get the code", "try another way",
    "use another method", "resend code",
)

# Hints/labels that identify the field a phone number goes in.
_PHONE_FIELD_HINTS = ("phone", "mobile", "number")

# ...and the one a code goes in.
_CODE_FIELD_HINTS = ("code", "confirmation", "security", "verification", "digit")

# ...and the captcha answer box.
_CAPTCHA_FIELD_HINTS = ("characters", "text", "captcha", "code", "answer", "type")

# Smallest captcha strip worth cropping to, in pixels.
_CAPTCHA_MIN_WIDTH = 200
_CAPTCHA_MIN_HEIGHT = 40

# How much wider than tall a captcha image is. This is the load-bearing part of
# the filter: without it a square profile avatar passes every other test and,
# being larger in area than a wide short strip, wins -- and the solver is then
# asked to read characters out of somebody's face.
_CAPTCHA_MIN_ASPECT = 1.8


def looks_like_captcha(width: int, height: int) -> bool:
    """Whether an on-screen image's shape is that of a captcha strip.

    Captchas are wide, short and reasonably large. Icons fail on size, avatars
    and photos fail on aspect, and a full-screen background fails on aspect too.
    """
    if width < _CAPTCHA_MIN_WIDTH or height < _CAPTCHA_MIN_HEIGHT:
        return False
    return width >= height * _CAPTCHA_MIN_ASPECT


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _safe(name: str) -> str:
    """A filesystem-safe slug for a profile name like 'Jil 2'."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "run")).strip("-") or "run"


def _snippet(text, limit: int = 400) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    handler = getattr(logger, level, None)
    if callable(handler):
        handler(message, *args)


class VerificationRecorder:
    """Writes every screen a run sees to disk, so it can be looked at later.

    Nothing here is required for the flow to work -- a recorder that cannot
    write (read-only disk, no home directory) degrades to logging and the run
    continues. Losing the evidence must never lose the account.
    """

    def __init__(self, label: str, root: Path | None = None, logger=None,
                 enabled: bool = True) -> None:
        self.logger = logger
        self.enabled = enabled
        self.index = 0
        self.dir: Path | None = None
        if not enabled:
            return
        base = Path(root) if root else RUN_ROOT
        try:
            self.dir = base / f"{_safe(label)}-{_now_stamp()}"
            self.dir.mkdir(parents=True, exist_ok=True)
            _emit(logger, "info", "verification: recording this run to %s", self.dir)
        except Exception as exc:
            self.dir = None
            _emit(logger, "warning",
                  "verification: could not open a recording folder (%s); "
                  "the run continues without saved screens", exc)

    # --- writing --------------------------------------------------------------
    def _write(self, name: str, data, binary: bool = False) -> Path | None:
        if self.dir is None:
            return None
        path = self.dir / name
        try:
            if binary:
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8")
            return path
        except Exception as exc:
            _emit(self.logger, "warning",
                  "verification: could not write %s (%s)", name, exc)
            return None

    def screen(self, challenge: str, text: str, source: str,
               xml: bytes | None = None, png: bytes | None = None,
               note: str = "") -> dict:
        """Record one screen. Returns the manifest entry (also useful in logs)."""
        self.index += 1
        stem = f"{self.index:02d}-{_safe(challenge)}"
        entry = {
            "index": self.index,
            "at": datetime.now(timezone.utc).isoformat(),
            "challenge": challenge,
            "source": source,
            "note": note,
            "text": text or "",
        }
        if self.dir is not None:
            if xml:
                self._write(f"{stem}.xml", xml, binary=True)
            if png:
                self._write(f"{stem}.png", png, binary=True)
            self._write(f"{stem}.txt", text or "")
            try:
                with (self.dir / "screens.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")
            except Exception as exc:
                _emit(self.logger, "warning",
                      "verification: could not append to screens.jsonl (%s)", exc)
        return entry

    def event(self, message: str) -> None:
        """A one-line note in the run folder -- taps, decisions, outcomes."""
        if self.dir is None:
            return
        stamped = f"{datetime.now(timezone.utc).isoformat()}  {message}\n"
        try:
            with (self.dir / "run.log").open("a", encoding="utf-8") as handle:
                handle.write(stamped)
        except Exception:
            pass

    def artifact(self, name: str, data: bytes) -> Path | None:
        return self._write(name, data, binary=True)


class AdbChallengeDriver:
    """`ChallengeDriver` over a connected ADB target.

    `act=False` makes every action a no-op that logs its intent -- read-only
    reconnaissance against a live profile. `act=True` is the real thing.
    """

    def __init__(self, target: str, adb_client, logger=None, flow=None,
                 recorder: VerificationRecorder | None = None,
                 act: bool = True, settle_seconds: float = SETTLE_SECONDS,
                 screenshots: bool = True) -> None:
        self.target = target
        self.adb_client = adb_client
        self.logger = logger
        self.flow = flow
        self.recorder = recorder
        self.act = act
        self.settle_seconds = settle_seconds
        # A screenshot off an MLX cloud phone is ~2MB and takes about ten
        # seconds, which dominates the time between looks. Worth it on a real
        # run -- the picture is often the only way to understand a screen the
        # dump renders as word salad -- but switchable off when the cadence
        # matters more than the pictures.
        self.screenshots = screenshots
        self._root = None          # the dump behind the last read_screen()
        self._text = ""
        self._source = "none"

    # --- logging --------------------------------------------------------------
    def _log(self, level: str, message: str, *args) -> None:
        _emit(self.logger, level, "verification[%s]: " + message, self.target, *args)
        if self.recorder is not None:
            try:
                self.recorder.event(message % args if args else message)
            except Exception:
                pass

    def _refuse(self, what: str) -> bool:
        """Observation mode: say what would have happened, do nothing."""
        self._log("info", "OBSERVE ONLY -- would %s", what)
        return False

    # --- raw device access ----------------------------------------------------
    def _screencap(self, force: bool = False) -> bytes | None:
        """PNG bytes of the current screen, or None.

        `force` is for the captcha, which needs the picture regardless of
        whether routine screenshots are switched off.
        """
        if not self.screenshots and not force:
            return None
        started = time.monotonic()
        try:
            result = run_hidden(["adb", "-s", self.target, "exec-out", "screencap", "-p"],
                                shell=False, check=False, capture_output=True)
        except Exception as exc:
            self._log("warning", "screencap failed (%s)", exc)
            return None
        data = result.stdout or b""
        if not data:
            self._log("warning", "screencap returned nothing")
        else:
            self._log("info", "screenshot: %d KB in %.1fs",
                      len(data) // 1024, time.monotonic() - started)
        return data or None

    def _dump(self):
        """(root, xml_bytes) for the current screen, or (None, None)."""
        from adb_bot.automation.flows import instagram as ig
        try:
            root = ig._adb_capture_ui_dump(self.target, logger=self.logger,
                                           idle_retries=2)
        except Exception as exc:
            self._log("warning", "UI dump raised (%s)", exc)
            return None, None
        if root is None:
            return None, None
        xml = None
        try:
            from xml.etree import ElementTree
            xml = ElementTree.tostring(root, encoding="utf-8")
        except Exception:
            pass
        return root, xml

    def _tap(self, center, description: str) -> bool:
        x, y = center
        if not self.act:
            return self._refuse(f"tap {description} at ({x}, {y})")
        self._log("info", "tapping %s at (%s, %s)", description, x, y)
        self.adb_client.run_command(f"adb -s {self.target} shell {tap(x, y)}")
        time.sleep(self.settle_seconds)
        return True

    # --- reading --------------------------------------------------------------
    def read_screen(self) -> str:
        """Lowercased visible text, recording the dump and a screenshot.

        Text comes from the UI dump when the screen will produce one and from
        OCR when it will not (several Instagram screens never reach an idle
        state). Which source was used is logged and recorded, because a run that
        went wrong on an OCR-only screen is a different problem from one that
        went wrong with a full dump available.
        """
        root, xml = self._dump()
        text = _dump_text(root)
        source = "ui-dump"

        if not text and self.flow is not None and hasattr(self.flow, "_ocr_screen_text"):
            try:
                text = (self.flow._ocr_screen_text(self.target, logger=self.logger) or "")
                source = "ocr"
            except Exception as exc:
                self._log("warning", "OCR fallback raised (%s)", exc)
        if not text:
            source = "none"

        self._root, self._text, self._source = root, text, source
        challenge = verification.classify_challenge(text)

        png = self._screencap()
        if self.recorder is not None:
            self.recorder.screen(challenge, text, source, xml=xml, png=png)

        self._log("info", "screen read via %s -> %s | text=%r",
                  source, challenge, _snippet(text))
        if source == "ui-dump" and root is not None:
            fields = self._edit_fields(root)
            self._log("info", "screen has %d input field(s) and %d clickable label(s)",
                      len(fields), len(self._clickable_labels(root)))
            for field in fields:
                self._log("info", "  field: hint=%r value=%r at %s",
                          field["hint"], field["value"], field["center"])
        return text

    # --- dump introspection ---------------------------------------------------
    @staticmethod
    def _center(attrs):
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                          attrs.get("bounds", "") or "")
        if not match:
            return None
        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            return None
        return (x1 + x2) // 2, (y1 + y2) // 2

    @staticmethod
    def _bounds(attrs):
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                          attrs.get("bounds", "") or "")
        return tuple(map(int, match.groups())) if match else None

    def _edit_fields(self, root) -> list:
        """Every EditText on screen, with its hint and current value."""
        out = []
        if root is None:
            return out
        for node in root.iter():
            attrs = node.attrib
            if "edittext" not in str(attrs.get("class", "") or "").lower():
                continue
            center = self._center(attrs)
            if center is None:
                continue
            out.append({
                "center": center,
                "bounds": self._bounds(attrs),
                "value": str(attrs.get("text", "") or "").strip(),
                "hint": " ".join(filter(None, (
                    str(attrs.get("content-desc", "") or "").strip(),
                    str(attrs.get("hint", "") or "").strip(),
                    str(attrs.get("resource-id", "") or "").rsplit("/", 1)[-1],
                ))).strip().lower(),
                "focused": str(attrs.get("focused", "")).lower() == "true",
            })
        return out

    def _clickable_labels(self, root) -> list:
        """Exact labels of clickable nodes -- what a tap could legitimately hit."""
        out = []
        if root is None:
            return out
        for node in root.iter():
            attrs = node.attrib
            if str(attrs.get("clickable", "")).lower() != "true":
                continue
            for key in ("text", "content-desc"):
                value = str(attrs.get(key, "") or "").strip()
                if value:
                    out.append(value)
        return out

    def _find_exact(self, labels) -> tuple | None:
        """Center of a node whose text/desc EQUALS one of `labels`.

        The exact match is the safety property: it is what stops "Not now" from
        matching "now" and "Don't allow" from matching "allow".
        """
        if self._root is None:
            return None
        from adb_bot.automation.flows import instagram as ig
        for label in labels:
            center = ig._find_center_by_exact_label(self._root, (label,))
            if center is not None:
                self._log("info", "matched button %r at %s", label, center)
                return center
        return None

    def _pick_field(self, hints) -> dict | None:
        """The EditText most likely to be the one `hints` describes.

        Preference order: a hint word matches, then the already-focused field,
        then -- only if the screen has exactly one field -- that one. A screen
        with several unlabelled fields returns None rather than guessing, since
        typing a phone number into the wrong box is silent and wastes the number.
        """
        fields = self._edit_fields(self._root)
        if not fields:
            self._log("warning", "no input field on screen")
            return None

        for field in fields:
            if any(hint in field["hint"] for hint in hints):
                self._log("info", "chose field by hint %r", field["hint"])
                return field
        for field in fields:
            if field["focused"]:
                self._log("info", "chose the focused field (no hint matched)")
                return field
        if len(fields) == 1:
            self._log("info", "chose the only field on screen (no hint matched)")
            return fields[0]

        self._log("warning",
                  "%d input fields and none identifiable from %s -- refusing to "
                  "guess which one to type into", len(fields), list(hints))
        return None

    # --- typing ---------------------------------------------------------------
    def _clear_field(self, field) -> None:
        """Empty a field that already holds text (a re-asked screen keeps it)."""
        existing = field.get("value") or ""
        if not existing:
            return
        if not self.act:
            self._refuse(f"clear {len(existing)} character(s) from the field")
            return
        self._log("info", "clearing %d existing character(s)", len(existing))
        # MOVE_END, then one DEL per character. Slower than a select-all but it
        # works on every keyboard and cannot leave a partial value behind.
        self.adb_client.run_command(
            f"adb -s {self.target} shell input keyevent 123")
        for _ in range(min(len(existing) + 4, 40)):
            self.adb_client.run_command(
                f"adb -s {self.target} shell input keyevent 67")

    def _type(self, field, value: str, what: str) -> bool:
        """Tap a field and type `value` into it, then read it back."""
        if not self._tap(field["center"], f"the {what} field"):
            return False
        self._clear_field(field)
        if not self.act:
            return self._refuse(f"type the {what} {value!r}")

        self._log("info", "typing the %s (%d characters)", what, len(value))
        self.adb_client.run_command(
            f"adb -s {self.target} shell {write_text(value)}")
        time.sleep(1.0)

        # Read it back. `input text` silently drops characters when the field
        # loses focus mid-type, and a half-typed number fails in a way that
        # looks exactly like a bad SMS pool.
        root, _xml = self._dump()
        if root is not None:
            self._root = root
            for candidate in self._edit_fields(root):
                if candidate["value"] and value.endswith(candidate["value"][-4:]):
                    self._log("info", "the %s field now reads %r", what,
                              candidate["value"])
                    return True
            self._log("warning",
                      "typed the %s but no field reads it back; fields now: %s",
                      what, [f["value"] for f in self._edit_fields(root)])
        return True

    def _submit(self) -> bool:
        """Tap whatever advances this screen."""
        center = self._find_exact(_SUBMIT_LABELS)
        if center is None:
            self._log("warning",
                      "no submit button found; clickable labels were %s",
                      self._clickable_labels(self._root)[:20])
            return False
        return self._tap(center, "the submit button")

    # --- ChallengeDriver ------------------------------------------------------
    def choose_sms_method(self) -> bool:
        self._log("info", "handling the 'how do you want to get the code' chooser")
        center = self._find_exact(_SMS_OPTION_LABELS)
        if center is None:
            self._log("warning",
                      "no SMS option matched exactly; clickable labels were %s",
                      self._clickable_labels(self._root)[:20])
            return False
        if not self._tap(center, "the SMS option"):
            return False
        # Some builds need the choice confirmed, others advance on the tap. A
        # missing submit button here is normal, so its absence is not a failure.
        self.read_screen()
        self._submit()
        return True

    def enter_phone(self, number: str) -> bool:
        self._log("info", "entering the phone number %s", number)
        field = self._pick_field(_PHONE_FIELD_HINTS)
        if field is None:
            return False
        if not self._type(field, number, "phone number"):
            return False
        if not self.act:
            return False
        return self._submit()

    def enter_code(self, code: str) -> bool:
        self._log("info", "entering the confirmation code %s", code)
        field = self._pick_field(_CODE_FIELD_HINTS)
        if field is None:
            return False
        if not self._type(field, code, "confirmation code"):
            return False
        if not self.act:
            return False
        # A 6-digit code box often submits itself on the last digit; a missing
        # button is therefore not a failure here.
        self.read_screen()
        self._submit()
        return True

    def request_new_number(self) -> bool:
        self._log("info", "getting back to the phone number screen")
        center = self._find_exact(_NEW_NUMBER_LABELS)
        if center is not None:
            if not self._tap(center, "the change-number link"):
                return False
            # The link may open a second screen that offers the real change.
            self.read_screen()
            follow = self._find_exact(_NEW_NUMBER_LABELS)
            if follow is not None:
                self._tap(follow, "the change-number option")
            return True

        self._log("info",
                  "no change-number link (labels were %s); pressing Back instead",
                  self._clickable_labels(self._root)[:20])
        if not self.act:
            return self._refuse("press Back to reach the phone screen")
        self.adb_client.run_command(f"adb -s {self.target} shell {back()}")
        time.sleep(self.settle_seconds)
        return True

    def upload_photo(self) -> bool:
        """Not implemented, on purpose -- see TODO 3.4.

        The photo challenge wants a picture of a person. Nothing in this repo
        owns one: the model's media folder holds reel clips, and a frame from a
        reel may not pass Instagram's check. Worse, the wrong face on the wrong
        account is a real harm that failing this step is not, so this returns
        False and the profile goes back to a human. Implementing it is a
        decision about *which* picture, not a coding problem.
        """
        self._log("warning",
                  "photo challenge: no picture source is configured, so this "
                  "profile needs a person (TODO 3.4)")
        return False

    def capture_captcha_image(self) -> str | None:
        """Save the captcha image for the solver. Returns a local PNG path."""
        png = self._screencap(force=True)
        if not png:
            self._log("warning", "captcha: could not screenshot the challenge")
            return None

        full = None
        if self.recorder is not None:
            full = self.recorder.artifact(
                f"captcha-{self.recorder.index:02d}-full.png", png)
        if full is None:
            # No recorder, or it could not write. Fall back to a temp file --
            # the solver needs a real path on disk.
            import tempfile
            handle, path = tempfile.mkstemp(prefix="captcha-", suffix=".png")
            os.close(handle)
            Path(path).write_bytes(png)
            full = Path(path)

        cropped = self._crop_captcha(full)
        if cropped is not None:
            self._log("info", "captcha: cropped the challenge image to %s", cropped)
            return str(cropped)

        # An uncropped screenshot still reaches a human solver at 2captcha, who
        # can see which characters are being asked for -- worse odds than a
        # clean crop, but far better than giving up. It is logged as degraded so
        # a run that fails this way is not mistaken for a bad pool.
        self._log("warning",
                  "captcha: could not isolate the image; sending the whole "
                  "screenshot (%s), which is less accurate", full)
        return str(full)

    def _crop_captcha(self, png_path: Path):
        """Crop to the captcha image node from the dump, if it can be found."""
        if self._root is None:
            return None
        try:
            import cv2
        except Exception:
            self._log("info", "captcha: cv2 unavailable, cannot crop")
            return None

        best = None
        for node in self._root.iter():
            attrs = node.attrib
            class_name = str(attrs.get("class", "") or "").lower()
            if "imageview" not in class_name and "image" not in class_name:
                continue
            bounds = self._bounds(attrs)
            if bounds is None:
                continue
            x1, y1, x2, y2 = bounds
            width, height = x2 - x1, y2 - y1
            if not looks_like_captcha(width, height):
                continue
            area = width * height
            if best is None or area > best[0]:
                best = (area, bounds)

        if best is None:
            self._log("info", "captcha: no image node on screen looked like a "
                              "captcha (checked every ImageView's shape)")
            return None

        try:
            import cv2
            image = cv2.imread(str(png_path))
            if image is None:
                return None
            x1, y1, x2, y2 = best[1]
            height, width = image.shape[:2]
            # Pad a little: the characters often overflow the node's own box.
            pad = 8
            x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
            x2, y2 = min(width, x2 + pad), min(height, y2 + pad)
            crop = image[y1:y2, x1:x2]
            if crop.size == 0:
                return None
            out = Path(str(png_path).replace("-full.png", "-crop.png"))
            if out == png_path:
                out = png_path.with_name(png_path.stem + "-crop.png")
            cv2.imwrite(str(out), crop)
            return out
        except Exception as exc:
            self._log("warning", "captcha: cropping failed (%s)", exc)
            return None

    def enter_captcha(self, text: str) -> bool:
        self._log("info", "entering the captcha answer %r", text)
        field = self._pick_field(_CAPTCHA_FIELD_HINTS)
        if field is None:
            return False
        if not self._type(field, text, "captcha answer"):
            return False
        if not self.act:
            return False
        return self._submit()
