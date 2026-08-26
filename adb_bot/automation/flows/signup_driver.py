"""`SignupDriver` over a connected ADB target.

Built on `AdbChallengeDriver` so the two flows share one way of reading a
screen, dumping it and recording it. What is added here is everything the
2026-08-13 run had to learn the hard way:

* a **keyboard dismissal** before every submit -- the floating keyboard covers
  the button and receives the tap while the dump still reports the button as
  visible, which produced two taps that did nothing at all and nothing in the
  dump to explain them;
* a **fill** that clears by measured length and then proves the field holds
  what was intended, because a partial clear silently prepends the survivors
  and Instagram accepts the result;
* **tapping the clickable ancestor** of a labelled node, because several
  controls in this chain carry their text on a `clickable=false` child;
* a **date-picker** driver, because the birthday screen is an Android spinner
  that opens on today's date.
"""

from __future__ import annotations

import re
import time

from adb_bot.automation.flows.verification_driver import AdbChallengeDriver
from adb_bot.core.adb_commands import write_text

# How many characters a clear will delete at most. Long enough for a pre-filled
# email address, bounded so a runaway cannot sit there deleting for a minute.
MAX_CLEAR = 60

# Waited after dismissing the IME. Short, but the tap that follows lands on the
# real button rather than on a keyboard that is still animating away.
KEYBOARD_SETTLE = 1.0

# Characters Android and Instagram render as punctuation but which are not the
# ASCII ones anybody types into a label list. Android's own permission dialog
# spells its button `DON’T ALLOW` with U+2019, so a list containing "DON'T
# ALLOW" matched nothing and the run sat on the dialog until its repeat guard
# gave up -- one screen after the tap that creates the account.
#
# Normalising both sides is the fix rather than adding a second spelling of
# every label: the same character turns up in "I didn’t get the code" and
# anywhere else Instagram writes an apostrophe, and each of those would
# otherwise be its own silent miss.
_PUNCTUATION = {
    "’": "'",      # right single quotation mark
    "‘": "'",      # left single quotation mark
    "ʼ": "'",      # modifier letter apostrophe
    "“": '"',
    "”": '"',
    "–": "-",      # en dash
    "—": "-",      # em dash
    " ": " ",      # non-breaking space
}


def normalise_label(value) -> str:
    """A label reduced to what two spellings of it have in common."""
    text = str(value or "")
    for fancy, plain in _PUNCTUATION.items():
        text = text.replace(fancy, plain)
    return " ".join(text.split()).strip().lower()


class AdbSignupDriver(AdbChallengeDriver):
    """The signup flow's device seam."""

    # --- taps -----------------------------------------------------------------
    def _clickable_ancestor(self, node, root):
        """The node itself if it is clickable, else its nearest clickable parent."""
        parents = {child: parent for parent in root.iter() for child in parent}
        current = node
        for _ in range(6):
            if str(current.attrib.get("clickable", "")).lower() == "true":
                return current
            current = parents.get(current)
            if current is None:
                return None
        return None

    def tap_label(self, labels, require_clickable: bool = True) -> bool:
        """Tap the node whose text or description EQUALS one of `labels`.

        Exact matching is the safety property -- it is what stops "Not now"
        matching "now" -- and the ancestor walk is what makes it work at all on
        the screens whose labels sit on non-clickable children.

        `require_clickable=False` also taps a node with **no** clickable
        ancestor at all, at its own bounds. Gmail's settings list is the case
        that needs it: the account row renders its address on a plain view and
        the only clickable things on the whole screen are `Navigate up` and
        `More options`, so the row is untappable by the strict rule and the
        account's settings cannot be reached. Android still delivers a tap at
        those coordinates to whatever handles it. Off by default, because
        tapping something the screen never said was interactive is a guess, and
        everywhere else in this chain the strict rule is what keeps a stray tap
        from landing on the wrong control.
        """
        if self._root is None:
            self.read_screen()
        root = self._root
        if root is None:
            self._log("warning", "no screen to tap %s on", list(labels))
            return False

        wanted = [normalise_label(label) for label in labels]
        for node in root.iter():
            attrs = node.attrib
            for key in ("text", "content-desc"):
                value = normalise_label(attrs.get(key, ""))
                if not value or value not in wanted:
                    continue
                target = self._clickable_ancestor(node, root)
                if target is None:
                    if require_clickable:
                        continue
                    target = node
                center = self._center(target.attrib)
                if center is None:
                    continue
                self._log("info", "tapping %r at %s", attrs.get(key), center)
                return self._tap(center, f"{attrs.get(key)!r}",
                                 bounds=self._bounds(target.attrib))
        self._log("warning", "none of %s is on screen; clickable labels were %s",
                  list(labels), self._clickable_labels(root)[:20])
        return False

    # --- typing ---------------------------------------------------------------
    def fill(self, hints, value: str, what: str,
             submits_itself: bool = False, fallback_index: int | None = None) -> bool:
        """Clear, type, and prove the field holds exactly `value`.

        `submits_itself` is for fields that act on the last character rather
        than waiting for a button -- the confirmation code does, on its sixth
        digit. For those, an empty or vanished field after typing is the screen
        having moved on, which is success; reading it as failure produced
        "the confirmation code did not land; fields now hold ['']" on a run
        where the code had in fact been accepted.

        `fallback_index`, when given, is used only if no hint matches --
        picking the Nth field by screen position rather than refusing.
        `_pick_field`'s own refusal is right for a field whose *value* is
        expensive to get wrong (a phone number, silently burning a rented
        SMS lease) but wrong for a field whose worst case is a visible,
        recoverable "incorrect password". Confirmed live 2026-08-23
        (@daudkim272): a login screen with two genuinely hint-less fields
        (no `hint`/`content-desc` on either EditText, the labels are plain
        text elsewhere on screen) made the login form untypeable and the
        run waited out the whole budget on a screen nothing was ever
        entered into.
        """
        field = self._pick_field([h.lower() for h in hints])
        if field is None and fallback_index is not None:
            fields = self._edit_fields(self._root)
            if 0 <= fallback_index < len(fields):
                field = fields[fallback_index]
                self._log("info", "no hint matched for %s; falling back to "
                                  "field %d by position", what, fallback_index)
        if field is None:
            self._log("warning", "no field for %s (hints %s)", what, list(hints))
            return False
        if not self.act:
            return self._refuse(f"fill the {what} with {value!r}")

        if not self._tap(field["center"], f"the {what} field",
                         bounds=field.get("bounds")):
            return False
        time.sleep(0.6)

        existing = field.get("value") or ""
        # Clear by what is actually there. 14 backspaces against a 21-character
        # pre-filled address left `i1aikjg` behind, our address was appended to
        # it, and Instagram mailed a confirmation code to the result.
        deletions = min(max(len(existing) + 6, 24), MAX_CLEAR)
        self.adb_client.run_command(
            f"adb -s {self.target} shell input keyevent 123")
        for _ in range(deletions):
            self.adb_client.run_command(
                f"adb -s {self.target} shell input keyevent 67")

        self.adb_client.run_command(
            f"adb -s {self.target} shell {write_text(value)}")
        time.sleep(1.0)

        root, _xml = self._dump()
        if root is None:
            self._log("warning", "typed the %s but the screen would not dump", what)
            return True
        self._root = root
        for candidate in self._edit_fields(root):
            landed = (candidate["value"] or "").strip()
            if landed == value:
                self._log("info", "the %s field reads %r", what, landed)
                return True
            # Masked fields (password, code) render as bullets, so length is
            # the only thing that can be checked.
            if landed and set(landed) <= {"•", "●", "*"} and len(landed) == len(value):
                self._log("info", "the %s field holds %d masked characters",
                          what, len(landed))
                return True
        remaining = [f["value"] for f in self._edit_fields(root)]
        if submits_itself and not any(remaining):
            self._log("info", "the %s submitted itself and the screen moved on",
                      what)
            return True
        self._log("warning", "the %s did not land; fields now hold %s", what,
                  remaining)
        return False

    # --- the keyboard ---------------------------------------------------------
    def dismiss_keyboard(self) -> None:
        """Hide the IME so the next tap reaches the button under it."""
        if not self.act:
            self._refuse("dismiss the keyboard")
            return
        self.adb_client.run_command(f"adb -s {self.target} shell input keyevent 4")
        time.sleep(KEYBOARD_SETTLE)

    def press_enter(self) -> None:
        """Submit the focused field with the keyboard's own action key.

        The safe alternative to tapping a button when nothing is meant to be
        touched: `dismiss_keyboard()`'s BACK is not reliably consumed by the
        IME on every device and can fall through as real navigation (see
        `signup._submit_after_typing`'s docstring) -- ENTER (keyevent 66) only
        ever acts on a focused text field and has no such fallthrough.
        Confirmed for exactly this shape of problem on Google's own sign-in
        form (`google_signin._press_enter`).
        """
        if not self.act:
            self._refuse("press enter")
            return
        self.adb_client.run_command(f"adb -s {self.target} shell input keyevent 66")

    def showing_package(self, package: str) -> bool:
        """Is `package` the one that drew the screen?

        Asked of the UI dump itself, where every node carries the package that
        owns it. That is authoritative, unlike "does the classifier recognise
        this screen" -- which is what callers were using, and which answers no
        for every screen nobody has named yet. Instagram's "set up on new
        device" onboarding was in front, fully drawn, while a relaunch loop
        declared five times that Instagram would not come to the front.

        `dumpsys window` would also answer, and is what this deliberately
        avoids: it times out under concurrency and returns empty, which reads
        as "not in front" for a phone that is merely busy.
        """
        root = self._root
        if root is None:
            root, _xml = self._dump()
            if root is None:
                return False
            self._root = root
        wanted = str(package).strip().lower()
        for node in root.iter():
            if str(node.attrib.get("package", "") or "").lower() == wanted:
                return True
        return False

    def field_holds(self, hints, value: str) -> bool:
        """Does an input field currently hold exactly `value`?

        Equality against the field, not a search of the screen text. The
        difference is not academic: Instagram mutates a submitted handle by
        appending digits, so `sara65` becomes `sara652203` -- and a substring
        test against the whole screen says our handle is present when the box
        holds something else entirely. That misfire submitted Instagram's
        value while logging ours, seven times round a name/username loop,
        until the run gave up after thirty screens.

        Returns False when the screen cannot be read: "I cannot see it" must
        not be reported as "it is there".
        """
        root = self._root
        if root is None:
            root, _xml = self._dump()
            if root is None:
                return False
            self._root = root
        wanted = str(value).strip().lower()
        if not wanted:
            return False
        hints = tuple(str(h).lower() for h in (hints or ()))
        for candidate in self._edit_fields(root):
            if hints:
                hint = str(candidate.get("hint", "") or "").lower()
                if not any(h in hint for h in hints):
                    continue
            if str(candidate.get("value", "") or "").strip().lower() == wanted:
                return True
        return False

    def field_value(self, hints=()) -> str:
        """What an input field currently holds, or "" if it cannot be read.

        `field_holds` answers a yes/no about a value we already have; this is
        for the case where Instagram has put its **own** handle in the box and
        accepted it -- the value is the thing worth having, and retyping ours
        over a suggestion Instagram just called valid is how the username step
        loops until it is declared stuck.
        """
        root = self._root
        if root is None:
            root, _xml = self._dump()
            if root is None:
                return ""
            self._root = root
        hints = tuple(str(h).lower() for h in (hints or ()))
        for candidate in self._edit_fields(root):
            if hints:
                hint = str(candidate.get("hint", "") or "").lower()
                if not any(h in hint for h in hints):
                    continue
            value = str(candidate.get("value", "") or "").strip()
            if value:
                return value
        return ""

    def submit_with_keyboard(self) -> None:
        """Submit the focused field using the IME's own action key.

        The thing a tap on the button cannot do. Google's email screen needed
        this when tapping NEXT left the form redrawing itself, and Instagram's
        username screen does the same: it reports `input username is valid`,
        keeps `Next` enabled, and does not move when it is tapped.

        Deliberately without dismissing the keyboard first -- the IME action
        only exists while the keyboard is up, which is exactly why this reaches
        a case tapping cannot.
        """
        if not self.act:
            self._refuse("submit with the keyboard")
            return
        self.adb_client.run_command(
            f"adb -s {self.target} shell input keyevent 66")
        time.sleep(KEYBOARD_SETTLE)

    # --- the date picker ------------------------------------------------------
    def _picker_inputs(self, root):
        out = []
        for node in root.iter() if root is not None else []:
            attrs = node.attrib
            resource = str(attrs.get("resource-id", "") or "")
            if "numberpicker_input" not in resource:
                continue
            center = self._center(attrs)
            if center is not None:
                out.append({"center": center,
                            "value": str(attrs.get("text", "") or "")})
        return out

    def set_date(self, day: int, month: str, year: int) -> bool:
        """Type a birthday into the three spinners and confirm it.

        The picker opens on today's date, so accepting it unchanged claims the
        account holder was born this year. Its spinners expose editable
        `numberpicker_input` fields, which beats swiping a year picker 27 times.
        """
        root, _xml = self._dump()
        self._root = root
        if len(self._picker_inputs(root)) != 3:
            self._log("warning", "expected 3 date spinners, found %d",
                      len(self._picker_inputs(root)))
            return False
        if not self.act:
            return self._refuse(f"set the date to {day} {month} {year}")

        # One spinner at a time, each located in a **fresh** dump. Taking all
        # three positions from a single dump loses the dialog: the keyboard
        # opening moves it, so the second or third tap lands outside it, and a
        # tap outside a dialog dismisses the dialog. That is what turned the
        # first scripted run into a password/date-picker loop.
        for index, value in enumerate((str(day), str(month), str(year))):
            root, _xml = self._dump()
            self._root = root
            inputs = self._picker_inputs(root)
            if len(inputs) != 3:
                self._log("warning",
                          "the date picker went away after %d of 3 spinners "
                          "(found %d) -- not tapping blind", index, len(inputs))
                return False
            node = inputs[index]
            self.adb_client.run_command(
                f"adb -s {self.target} shell input tap {node['center'][0]} "
                f"{node['center'][1]}")
            time.sleep(0.6)
            self.adb_client.run_command(
                f"adb -s {self.target} shell input keyevent 123")
            for _ in range(8):
                self.adb_client.run_command(
                    f"adb -s {self.target} shell input keyevent 67")
            self.adb_client.run_command(
                f"adb -s {self.target} shell {write_text(value)}")
            time.sleep(0.5)

        root, _xml = self._dump()
        self._root = root
        self._log("info", "date spinners now read %s",
                  [n["value"] for n in self._picker_inputs(root)])

        # Try the button before touching Back. `dismiss_keyboard` sends Back,
        # and Back on an open dialog closes the dialog -- which throws away the
        # date that was just typed.
        if self.tap_label(("SET", "Set", "OK", "Done")):
            return True
        self._log("info", "SET is not reachable; dropping the keyboard first")
        self.dismiss_keyboard()
        root, _xml = self._dump()
        self._root = root
        return self.tap_label(("SET", "Set", "OK", "Done"))


def looks_like_date_picker(text: str | None) -> bool:
    """Whether `text` is the Android date dialog rather than an Instagram screen."""
    if not text:
        return False
    haystack = text.lower()
    return "set date" in haystack and bool(re.search(r"\b(19|20)\d{2}\b", haystack))
