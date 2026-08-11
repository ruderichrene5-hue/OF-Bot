"""The device driver's judgement calls, tested without a phone.

Everything here is about the two ways this driver can quietly do damage: tapping
the wrong control (the exact-label rule) and typing into the wrong box (the
field picker). Both fail silently on a real device -- a wrong tap looks like
Instagram being slow, and a phone number typed into the wrong field burns a
rented number with no error anywhere -- so they are pinned here instead.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from adb_bot.automation.flows import instagram as ig
from adb_bot.automation.flows import verification_driver as vd


def _root(*nodes: str):
    """Build a UI-dump root from `<node .../>` fragments."""
    return ElementTree.fromstring(f"<hierarchy>{''.join(nodes)}</hierarchy>")


def _edit(bounds="[100,200][900,300]", text="", desc="", hint="", rid="",
          focused="false"):
    return (f'<node class="android.widget.EditText" bounds="{bounds}" '
            f'text="{text}" content-desc="{desc}" hint="{hint}" '
            f'resource-id="{rid}" focused="{focused}" clickable="true"/>')


def _button(label, bounds="[100,900][900,1000]", clickable="true"):
    return (f'<node class="android.widget.Button" bounds="{bounds}" '
            f'text="{label}" content-desc="" clickable="{clickable}"/>')


class FakeAdb:
    """Records every command instead of running it."""

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


class Recording:
    """A logger that keeps what it was told, so tests can assert on warnings."""

    def __init__(self):
        self.lines = []

    def _add(self, level):
        def handler(message, *args):
            self.lines.append((level, message % args if args else message))
        return handler

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug"):
            return self._add(name)
        raise AttributeError(name)

    def text(self):
        return " | ".join(line for _level, line in self.lines)


def _driver(root=None, act=True, adb=None, logger=None):
    driver = vd.AdbChallengeDriver("dev:1", adb or FakeAdb(),
                                   logger=logger, act=act, settle_seconds=0)
    driver._root = root
    return driver


class FieldPickerTest(unittest.TestCase):
    """Which box gets typed into."""

    def test_prefers_a_field_whose_hint_matches(self):
        root = _root(_edit(bounds="[0,100][500,200]", desc="Email"),
                     _edit(bounds="[0,300][500,400]", desc="Mobile number"))
        field = _driver(root)._pick_field(vd._PHONE_FIELD_HINTS)
        self.assertEqual(field["center"], (250, 350))

    def test_falls_back_to_the_focused_field(self):
        root = _root(_edit(bounds="[0,100][500,200]"),
                     _edit(bounds="[0,300][500,400]", focused="true"))
        field = _driver(root)._pick_field(vd._PHONE_FIELD_HINTS)
        self.assertEqual(field["center"], (250, 350))

    def test_a_single_unlabelled_field_is_taken(self):
        root = _root(_edit(bounds="[0,100][500,200]"))
        field = _driver(root)._pick_field(vd._CODE_FIELD_HINTS)
        self.assertIsNotNone(field)

    def test_several_unidentifiable_fields_are_refused(self):
        """Guessing here silently burns a rented number, so it must not guess."""
        logger = Recording()
        root = _root(_edit(bounds="[0,100][500,200]"),
                     _edit(bounds="[0,300][500,400]"),
                     _edit(bounds="[0,500][500,600]"))
        self.assertIsNone(_driver(root, logger=logger)._pick_field(vd._PHONE_FIELD_HINTS))
        self.assertIn("refusing to guess", logger.text())

    def test_no_field_at_all_is_refused(self):
        self.assertIsNone(_driver(_root(_button("Next")))._pick_field(("phone",)))


class ExactLabelTest(unittest.TestCase):
    """The rule that keeps a tap off the wrong control."""

    def test_a_substring_never_matches(self):
        """'Not now' contains 'now' -- and must never be tapped as one."""
        driver = _driver(_root(_button("Not now")))
        self.assertIsNone(driver._find_exact(("now",)))

    def test_an_exact_label_matches(self):
        driver = _driver(_root(_button("Next", bounds="[0,0][100,100]")))
        self.assertEqual(driver._find_exact(vd._SUBMIT_LABELS), (50, 50))

    def test_the_preferred_label_wins_over_a_later_one(self):
        driver = _driver(_root(_button("Done", bounds="[0,0][100,100]"),
                               _button("Next", bounds="[0,200][100,300]")))
        self.assertEqual(driver._find_exact(vd._SUBMIT_LABELS), (50, 250))

    def test_sms_option_does_not_match_email(self):
        driver = _driver(_root(_button("Email"), _button("WhatsApp")))
        self.assertIsNone(driver._find_exact(vd._SMS_OPTION_LABELS))

    def test_missing_button_reports_what_was_on_screen(self):
        """The log has to name the labels, or a wrong marker list is invisible."""
        logger = Recording()
        driver = _driver(_root(_button("Weiter")), logger=logger)
        self.assertFalse(driver._submit())
        self.assertIn("Weiter", logger.text())


class ObserveModeTest(unittest.TestCase):
    """--apply off must reach the phone with reads only."""

    def test_no_taps_or_typing_are_issued(self):
        adb = FakeAdb()
        root = _root(_edit(desc="Phone number"), _button("Next"))
        driver = _driver(root, act=False, adb=adb)

        self.assertFalse(driver.enter_phone("15551234567"))
        self.assertFalse(driver.enter_code("123456"))
        self.assertFalse(driver.request_new_number())

        self.assertEqual(adb.taps, [])
        self.assertEqual(adb.typed, [])
        self.assertEqual(adb.commands, [])

    def test_it_says_what_it_would_have_done(self):
        logger = Recording()
        driver = _driver(_root(_edit(desc="Phone number")), act=False,
                         logger=logger)
        driver.enter_phone("15551234567")
        self.assertIn("OBSERVE ONLY", logger.text())

    def test_acting_mode_does_tap_and_type(self):
        adb = FakeAdb()
        root = _root(_edit(desc="Phone number"), _button("Next"))
        driver = _driver(root, act=True, adb=adb)
        driver._dump = lambda: (root, None)

        self.assertTrue(driver.enter_phone("15551234567"))
        self.assertTrue(adb.taps)
        self.assertTrue(adb.typed)
        self.assertIn("15551234567", adb.typed[0])


class PhotoTest(unittest.TestCase):
    def test_the_photo_challenge_is_declined_not_faked(self):
        """Uploading the wrong face is worse than handing back to a person."""
        logger = Recording()
        self.assertFalse(_driver(_root(), logger=logger).upload_photo())
        self.assertIn("needs a person", logger.text())


class ClearFieldTest(unittest.TestCase):
    def test_an_empty_field_is_not_cleared(self):
        adb = FakeAdb()
        driver = _driver(_root(), adb=adb)
        driver._clear_field({"value": ""})
        self.assertEqual(adb.commands, [])

    def test_an_occupied_field_is_emptied_before_typing(self):
        adb = FakeAdb()
        driver = _driver(_root(), adb=adb)
        driver._clear_field({"value": "+49160"})
        self.assertTrue(any("keyevent 123" in c for c in adb.commands))
        self.assertGreaterEqual(sum("keyevent 67" in c for c in adb.commands), 6)


class RecorderTest(unittest.TestCase):
    """The evidence trail -- and its refusal to break a run when it fails."""

    def test_a_screen_lands_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = vd.VerificationRecorder("Jil 2", root=Path(tmp))
            recorder.screen("phone", "enter your mobile number", "ui-dump",
                            xml=b"<hierarchy/>", png=b"\x89PNG")
            files = sorted(p.name for p in recorder.dir.iterdir())
            self.assertIn("01-phone.xml", files)
            self.assertIn("01-phone.png", files)
            self.assertIn("01-phone.txt", files)
            self.assertIn("screens.jsonl", files)

    def test_the_name_is_made_filesystem_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = vd.VerificationRecorder("Jil 2 / test", root=Path(tmp))
            self.assertNotIn("/", recorder.dir.name.replace(tmp, ""))

    def test_an_unwritable_recorder_does_not_stop_the_run(self):
        logger = Recording()
        recorder = vd.VerificationRecorder(
            "x", root=Path("/proc/nonexistent/nope"), logger=logger)
        self.assertIsNone(recorder.dir)
        recorder.screen("phone", "text", "ui-dump")   # must not raise
        recorder.event("still fine")

    def test_recording_can_be_switched_off(self):
        recorder = vd.VerificationRecorder("x", enabled=False)
        self.assertIsNone(recorder.dir)


class ReadScreenTest(unittest.TestCase):
    """read_screen is the one method the whole loop depends on."""

    def _patched(self, root, screencap=b"\x89PNG", flow=None):
        recorder = vd.VerificationRecorder("t", enabled=False)
        driver = vd.AdbChallengeDriver("dev:1", FakeAdb(), logger=Recording(),
                                       flow=flow, recorder=recorder,
                                       settle_seconds=0)
        driver._dump = lambda: (root, b"<hierarchy/>")
        driver._screencap = lambda: screencap
        return driver

    def test_text_comes_from_the_dump(self):
        root = _root('<node text="Enter the code we sent" bounds="[0,0][10,10]"/>')
        driver = self._patched(root)
        self.assertIn("enter the code", driver.read_screen())
        self.assertEqual(driver._source, "ui-dump")

    def test_ocr_covers_a_screen_that_will_not_dump(self):
        class Flow:
            def _ocr_screen_text(self, target, logger=None):
                return "Enter your mobile number"

        driver = self._patched(_root(), flow=Flow())
        self.assertIn("mobile number", driver.read_screen())
        self.assertEqual(driver._source, "ocr")

    def test_an_unreadable_screen_is_empty_not_an_exception(self):
        driver = self._patched(None)
        driver._dump = lambda: (None, None)
        self.assertEqual(driver.read_screen(), "")
        self.assertEqual(driver._source, "none")


class CaptchaShapeTest(unittest.TestCase):
    """Which image on screen is the captcha.

    Getting this wrong is not a crash -- it sends somebody's avatar to a human
    solver, who types whatever they see, and the account spends an attempt on
    it. So the shape test is pinned rather than left to the eye.
    """

    def test_a_wide_short_strip_is_the_captcha(self):
        self.assertTrue(vd.looks_like_captcha(600, 160))

    def test_an_icon_is_too_small(self):
        self.assertFalse(vd.looks_like_captcha(48, 48))

    def test_a_square_avatar_is_rejected_on_aspect(self):
        """It beats a real captcha on area, so only the aspect rule stops it."""
        self.assertFalse(vd.looks_like_captcha(400, 400))

    def test_a_tall_photo_is_rejected(self):
        self.assertFalse(vd.looks_like_captcha(400, 900))

    def test_a_full_screen_background_is_rejected(self):
        self.assertFalse(vd.looks_like_captcha(1080, 2400))

    def test_a_short_wide_banner_still_needs_height(self):
        self.assertFalse(vd.looks_like_captcha(600, 20))


class ProtocolTest(unittest.TestCase):
    def test_the_driver_satisfies_the_protocol_the_loop_expects(self):
        from adb_bot.automation.flows.verification import ChallengeDriver
        self.assertIsInstance(_driver(_root()), ChallengeDriver)

    def test_every_protocol_method_exists(self):
        driver = _driver(_root())
        for name in ("read_screen", "choose_sms_method", "enter_phone",
                     "enter_code", "request_new_number", "upload_photo",
                     "capture_captcha_image", "enter_captcha"):
            self.assertTrue(callable(getattr(driver, name, None)), name)


class RealPhoneScreenTest(unittest.TestCase):
    """The first real challenge screen this flow ever saw.

    Captured from `Blank (10)` on 2026-08-11
    (`~/.adb_bot/verification/Blank-10-20260811-222623/`). Reproduced here node
    for node, because everything about how the driver handles a phone screen was
    guesswork until this dump existed -- and two of the guesses were wrong.
    """

    SCREEN = _root(
        '<node text="Get support" bounds="[900,100][1100,160]" clickable="true"/>',
        '<node text="Enter your mobile number" bounds="[60,400][900,470]"/>',
        '<node text="You\'ll need to confirm this mobile number with a code via '
        'SMS or WhatsApp." bounds="[60,500][1200,600]"/>',
        '<node text="DE +49" bounds="[60,775][300,838]" clickable="true"/>',
        _edit(bounds="[314,775][1228,838]", hint="Phone number"),
        _button("Send code", bounds="[60,950][1228,1050]"),
    )

    def test_the_number_field_is_found_by_its_hint(self):
        field = _driver(self.SCREEN)._pick_field(vd._PHONE_FIELD_HINTS)
        self.assertIsNotNone(field)
        self.assertEqual(field["center"], (771, 806))

    def test_send_code_is_recognised_as_the_submit_button(self):
        """'send' does NOT exact-match 'Send code'.

        The first version of _SUBMIT_LABELS listed only the short forms, so it
        would have typed the number correctly and then found no button -- a
        rented number burned for nothing, reported as a driver failure.
        """
        driver = _driver(self.SCREEN)
        self.assertEqual(driver._find_exact(vd._SUBMIT_LABELS), (644, 1000))

    def test_the_country_picker_is_read(self):
        self.assertEqual(_driver(self.SCREEN).read_country_code(), "49")

    def test_a_screen_with_no_picker_reads_none(self):
        driver = _driver(_root(_edit(hint="Phone number")))
        self.assertIsNone(driver.read_country_code())

    def test_the_get_support_link_is_not_mistaken_for_a_submit(self):
        driver = _driver(_root(
            '<node text="Get support" bounds="[900,100][1100,160]" clickable="true"/>'))
        self.assertIsNone(driver._find_exact(vd._SUBMIT_LABELS))


class RealCodeScreenTest(unittest.TestCase):
    """The real code screen, from `Blank (13)` on 2026-08-11.

    Recording: `~/.adb_bot/verification/Blank-13-20260811-223021/`.
    """

    SCREEN = _root(
        '<node text="Get support" bounds="[900,100][1100,160]" clickable="true"/>',
        '<node text="Enter confirmation code" bounds="[60,400][900,470]"/>',
        '<node text="Enter the 6-digit confirmation code we sent via SMS to '
        '+4967870390593. It may take up to a minute for you to receive this '
        'code." bounds="[60,500][1200,700]"/>',
        _edit(bounds="[147,937][1113,1003]", hint="6-digit code"),
        _button("Request new code", bounds="[60,1100][1200,1180]"),
        _button("Next", bounds="[60,1250][1200,1330]"),
        _button("Update mobile number", bounds="[60,1400][1200,1480]"),
    )

    def test_the_code_field_is_found_by_its_hint(self):
        field = _driver(self.SCREEN)._pick_field(vd._CODE_FIELD_HINTS)
        self.assertIsNotNone(field)
        self.assertEqual(field["center"], (630, 970))

    def test_next_is_the_submit_button(self):
        self.assertEqual(_driver(self.SCREEN)._find_exact(vd._SUBMIT_LABELS),
                         (630, 1290))

    def test_a_new_number_means_update_mobile_number(self):
        """Not 'Request new code' -- that resends to a number already refunded.

        Taking the resend would burn another 45-second wait and count a second
        failure against a provider that did nothing wrong.
        """
        self.assertEqual(_driver(self.SCREEN)._find_exact(vd._NEW_NUMBER_LABELS),
                         (630, 1440))

    def test_no_resend_label_is_in_the_new_number_list(self):
        joined = " ".join(vd._NEW_NUMBER_LABELS)
        self.assertNotIn("resend", joined)
        self.assertNotIn("request new code", joined)

    def test_the_phone_and_code_screens_pick_different_buttons(self):
        """Both screens have a submit; they must not be the same control."""
        phone = _driver(RealPhoneScreenTest.SCREEN)._find_exact(vd._SUBMIT_LABELS)
        code = _driver(self.SCREEN)._find_exact(vd._SUBMIT_LABELS)
        self.assertNotEqual(phone, code)


class ProbeProfileLookupTest(unittest.TestCase):
    """Resolving --profile to one MultiLogin profile.

    The fleet has 169 profiles, many with near-identical names, and picking the
    wrong one means launching somebody else's account -- so the name match is
    exact, never a prefix.
    """

    ITEMS = [
        {"id": "624743063687856180", "serial_name": "Jil 2", "tags": ["Issue"]},
        {"id": "626422241281769608", "serial_name": "Jil 20", "tags": []},
        {"id": "111", "serial_name": "Blank (2)", "tags": []},
        # Names really are duplicated in this workspace -- these two ids both
        # answer to "Blank (10)" (seen 2026-08-11).
        {"id": "632162578940362822", "serial_name": "Blank (10)", "tags": ["Issue"]},
        {"id": "631357418634805297", "serial_name": "Blank (10)", "tags": ["Issue"]},
    ]

    def _find(self, wanted):
        from adb_bot.automation.verification_probe import _find_profile
        return _find_profile(self.ITEMS, wanted)

    def test_a_duplicated_name_is_refused_not_guessed(self):
        """Picking one silently would rent numbers against an unchosen account."""
        from adb_bot.automation.verification_probe import AmbiguousProfile
        with self.assertRaises(AmbiguousProfile):
            self._find("Blank (10)")

    def test_the_refusal_names_both_ids(self):
        from adb_bot.automation.verification_probe import AmbiguousProfile
        try:
            self._find("Blank (10)")
        except AmbiguousProfile as exc:
            self.assertIn("632162578940362822", str(exc))
            self.assertIn("631357418634805297", str(exc))
        else:
            self.fail("expected AmbiguousProfile")

    def test_an_id_resolves_a_duplicated_name(self):
        self.assertEqual(self._find("631357418634805297")["serial_name"],
                         "Blank (10)")

    def test_an_exact_name_matches(self):
        self.assertEqual(self._find("Jil 2")["id"], "624743063687856180")

    def test_a_prefix_does_not_steal_the_longer_name(self):
        """'Jil 2' must not resolve to 'Jil 20', nor the other way round."""
        self.assertEqual(self._find("Jil 20")["id"], "626422241281769608")

    def test_the_name_is_case_and_space_insensitive(self):
        self.assertEqual(self._find("  jil 2 ")["id"], "624743063687856180")

    def test_an_id_also_works(self):
        self.assertEqual(self._find("111")["serial_name"], "Blank (2)")

    def test_an_unknown_name_is_none(self):
        self.assertIsNone(self._find("Nobody 9"))


if __name__ == "__main__":
    unittest.main()
