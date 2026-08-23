"""The signup driver's device-level judgement calls, tested without a phone.

Both cases here cost a real run: a submit button that was tapped from a stale
screen, and a date picker that vanished because a tap landed outside it.
"""

from __future__ import annotations

import unittest
from xml.etree import ElementTree

from adb_bot.automation.flows.signup_driver import AdbSignupDriver


def _root(*nodes: str):
    return ElementTree.fromstring(f"<hierarchy>{''.join(nodes)}</hierarchy>")


def _spinner(value, bounds):
    return (f'<node class="android.widget.EditText" bounds="{bounds}" '
            f'text="{value}" content-desc="" hint="" '
            f'resource-id="android:id/numberpicker_input" clickable="true"/>')


def _button(label, bounds="[700,1500][900,1600]"):
    return (f'<node class="android.widget.Button" bounds="{bounds}" '
            f'text="{label}" content-desc="" clickable="true"/>')


def _edit(bounds, text="", hint=""):
    return (f'<node class="android.widget.EditText" bounds="{bounds}" '
            f'text="{text}" content-desc="" hint="{hint}" clickable="true"/>')


PICKER_BOUNDS = ("[100,1000][300,1100]", "[400,1000][600,1100]",
                 "[700,1000][900,1100]")


def _picker(values=("13", "Aug", "2026")):
    return _root(*(_spinner(v, b) for v, b in zip(values, PICKER_BOUNDS)),
                 _button("SET"), _button("CANCEL", "[400,1500][600,1600]"))


class FakeAdb:
    def __init__(self):
        self.commands = []

    def run_command(self, command):
        self.commands.append(command)
        return ""

    @property
    def taps(self):
        return [c for c in self.commands if "input tap" in c]

    @property
    def typed(self):
        return [c for c in self.commands if "input text" in c]

    @property
    def keyevents(self):
        return [c for c in self.commands if "input keyevent" in c]


class ScriptedDriver(AdbSignupDriver):
    """Serves a queued sequence of dumps, one per `_dump` call."""

    def __init__(self, adb, dumps):
        super().__init__("device:1", adb, logger=None, act=True,
                         screenshots=False)
        self._dumps = list(dumps)
        self.dump_calls = 0

    def _dump(self):
        self.dump_calls += 1
        root = self._dumps.pop(0) if self._dumps else _root()
        return root, b"<hierarchy/>"

    def _screencap(self, force=False):
        return None


class DatePickerTest(unittest.TestCase):
    def test_each_spinner_is_located_in_a_fresh_dump(self):
        """Reusing one dump's positions is what lost the dialog: the keyboard
        moves it, the next tap lands outside, and that dismisses it."""
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [_picker()] * 6)
        self.assertTrue(driver.set_date(12, "April", 1999))

        self.assertEqual(driver.dump_calls, 5)   # 1 probe + 3 spinners + 1 after
        self.assertEqual(adb.typed[-3:],
                         ["adb -s device:1 shell input text 12",
                          "adb -s device:1 shell input text April",
                          "adb -s device:1 shell input text 1999"])

    def test_a_picker_that_vanishes_mid_way_is_not_tapped_blind(self):
        adb = FakeAdb()
        # Probe and the first spinner are fine; then the dialog is gone.
        driver = ScriptedDriver(adb, [_picker(), _picker(), _root(_button("Next"))])
        self.assertFalse(driver.set_date(12, "April", 1999))
        # Exactly one spinner was typed into before it gave up.
        self.assertEqual(len(adb.typed), 1)

    def test_back_is_not_pressed_while_the_dialog_is_still_open(self):
        """Back closes the dialog, throwing away the date just typed."""
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [_picker()] * 6)
        driver.set_date(12, "April", 1999)
        self.assertNotIn("adb -s device:1 shell input keyevent 4", adb.commands)

    def test_the_keyboard_is_dropped_only_when_set_cannot_be_found(self):
        adb = FakeAdb()
        no_button = _root(*(_spinner(v, b)
                            for v, b in zip(("13", "Aug", "2026"), PICKER_BOUNDS)))
        driver = ScriptedDriver(adb, [_picker(), _picker(), _picker(), _picker(),
                                      no_button, _picker()])
        self.assertTrue(driver.set_date(12, "April", 1999))
        self.assertIn("adb -s device:1 shell input keyevent 4", adb.commands)


class TapLabelTest(unittest.TestCase):
    def test_a_label_on_a_child_taps_its_clickable_parent(self):
        """Several controls in this chain carry their text on a dead child."""
        root = ElementTree.fromstring(
            '<hierarchy><node class="android.view.View" clickable="true" '
            'bounds="[0,100][1000,200]">'
            '<node class="android.widget.TextView" clickable="false" '
            'bounds="[50,120][400,180]" text="I agree" content-desc=""/>'
            '</node></hierarchy>')
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [root])
        driver._root = root
        self.assertTrue(driver.tap_label(("I agree",)))
        self.assertEqual(adb.taps, ["adb -s device:1 shell input tap 500 150"])

    def test_an_absent_label_is_refused_rather_than_guessed(self):
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [_root(_button("Loading"))])
        driver._root = _root(_button("Loading"))
        self.assertFalse(driver.tap_label(("Next",)))
        self.assertEqual(adb.taps, [])


class FillFallbackIndexTest(unittest.TestCase):
    """`fill(..., fallback_index=N)` -- confirmed live 2026-08-23 (@daudkim272):
    a real Instagram login screen had two EditTexts with no `hint` and no
    `content-desc` at all (the labels were separate text elsewhere on
    screen), so `_pick_field` correctly refused both fields and the login
    form was never typed into. Position is a safe fallback here specifically
    because a wrong guess on a login form just earns a visible "incorrect
    password", not a silently wasted resource like a phone number would be.
    """

    def _two_hintless_fields(self):
        return _root(_edit("[0,500][900,600]"), _edit("[0,700][900,800]"))

    def test_falls_back_to_the_field_at_that_position(self):
        after_typing = _root(_edit("[0,500][900,600]", text="daudkim272"),
                             _edit("[0,700][900,800]"))
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [after_typing])
        driver._root = self._two_hintless_fields()

        self.assertTrue(driver.fill(("username",), "daudkim272", "username",
                                    fallback_index=0))
        self.assertTrue(adb.taps)

    def test_without_fallback_index_it_still_refuses(self):
        """The default behaviour for every other caller must not change."""
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [])
        driver._root = self._two_hintless_fields()

        self.assertFalse(driver.fill(("username",), "daudkim272", "username"))
        self.assertEqual(adb.taps, [])

    def test_an_out_of_range_fallback_index_still_refuses(self):
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [])
        driver._root = self._two_hintless_fields()

        self.assertFalse(driver.fill(("username",), "daudkim272", "username",
                                     fallback_index=5))
        self.assertEqual(adb.taps, [])

    def test_a_matching_hint_never_needs_the_fallback(self):
        """The fallback only ever fires once hint-matching has already
        failed -- a real hint always wins."""
        root = _root(_edit("[0,500][900,600]", hint="username"),
                     _edit("[0,700][900,800]"))
        after_typing = _root(_edit("[0,500][900,600]", text="daudkim272",
                                   hint="username"),
                             _edit("[0,700][900,800]"))
        adb = FakeAdb()
        driver = ScriptedDriver(adb, [after_typing])
        driver._root = root

        self.assertTrue(driver.fill(("username",), "daudkim272", "username",
                                    fallback_index=1))
        # Tapped the hinted field (y~550), not the fallback one (y~750).
        self.assertIn("550", adb.taps[0])


if __name__ == "__main__":
    unittest.main()
