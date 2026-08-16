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

    def tap_label(self, labels) -> bool:
        """Tap the node whose text or description EQUALS one of `labels`.

        Exact matching is the safety property -- it is what stops "Not now"
        matching "now" -- and the ancestor walk is what makes it work at all on
        the screens whose labels sit on non-clickable children.
        """
        if self._root is None:
            self.read_screen()
        root = self._root
        if root is None:
            self._log("warning", "no screen to tap %s on", list(labels))
            return False

        wanted = [str(label).strip().lower() for label in labels]
        for node in root.iter():
            attrs = node.attrib
            for key in ("text", "content-desc"):
                value = str(attrs.get(key, "") or "").strip().lower()
                if not value or value not in wanted:
                    continue
                target = self._clickable_ancestor(node, root)
                if target is None:
                    continue
                center = self._center(target.attrib)
                if center is None:
                    continue
                self._log("info", "tapping %r at %s", attrs.get(key), center)
                return self._tap(center, f"{attrs.get(key)!r}")
        self._log("warning", "none of %s is on screen; clickable labels were %s",
                  list(labels), self._clickable_labels(root)[:20])
        return False

    # --- typing ---------------------------------------------------------------
    def fill(self, hints, value: str, what: str,
             submits_itself: bool = False) -> bool:
        """Clear, type, and prove the field holds exactly `value`.

        `submits_itself` is for fields that act on the last character rather
        than waiting for a button -- the confirmation code does, on its sixth
        digit. For those, an empty or vanished field after typing is the screen
        having moved on, which is success; reading it as failure produced
        "the confirmation code did not land; fields now hold ['']" on a run
        where the code had in fact been accepted.
        """
        field = self._pick_field([h.lower() for h in hints])
        if field is None:
            self._log("warning", "no field for %s (hints %s)", what, list(hints))
            return False
        if not self.act:
            return self._refuse(f"fill the {what} with {value!r}")

        if not self._tap(field["center"], f"the {what} field"):
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
