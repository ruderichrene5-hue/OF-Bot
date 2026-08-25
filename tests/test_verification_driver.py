"""The device driver's judgement calls, tested without a phone.

Everything here is about the two ways this driver can quietly do damage: tapping
the wrong control (the exact-label rule) and typing into the wrong box (the
field picker). Both fail silently on a real device -- a wrong tap looks like
Instagram being slow, and a phone number typed into the wrong field burns a
rented number with no error anywhere -- so they are pinned here instead.
"""

from __future__ import annotations

import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from adb_bot.automation.flows import instagram as ig
from adb_bot.automation.flows import verification_driver as vd
from adb_bot.core import human_timing


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
        self.reauth_calls = []
        self.reauth_result = True

    def run_command(self, command):
        self.commands.append(command)
        return ""

    def reauthenticate(self, target, logger=None):
        self.reauth_calls.append(target)
        return self.reauth_result

    @property
    def taps(self):
        """Plain taps, plus held taps (`_tap(press=True)`) -- a zero-distance
        `input swipe x y x y ms`, distinct from a real scroll swipe whose
        start and end coordinates differ."""
        out = []
        for c in self.commands:
            if "input tap" in c:
                out.append(c)
                continue
            match = re.search(r"input swipe (\d+) (\d+) (\d+) (\d+)", c)
            if match and match.group(1) == match.group(3) \
                    and match.group(2) == match.group(4):
                out.append(c)
        return out

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


def _driver(root=None, act=True, adb=None, logger=None, rand=None):
    driver = vd.AdbChallengeDriver("dev:1", adb or FakeAdb(),
                                   logger=logger, act=act, settle_seconds=0,
                                   rand=rand)
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
    def setUp(self):
        # The happy path sleeps 3s twice for the picker to settle -- stub it
        # out, same pattern as LateRenderingCaptchaTest.
        self._saved_sleep = vd.time.sleep
        vd.time.sleep = lambda *_a, **_k: None
        self.addCleanup(lambda: setattr(vd.time, "sleep", self._saved_sleep))

    def test_the_photo_challenge_is_declined_without_a_configured_picture(self):
        """A random or unowned face is worse than handing back to a person --
        `photo_source_path` unset is the only case this still refuses."""
        logger = Recording()
        self.assertFalse(_driver(_root(), logger=logger).upload_photo())
        self.assertIn("needs a person", logger.text())

    def test_a_configured_picture_that_does_not_exist_is_declined(self):
        logger = Recording()
        driver = vd.AdbChallengeDriver(
            "dev:1", FakeAdb(), logger=logger, act=True, settle_seconds=0,
            photo_source_path="/nonexistent/photo.jpg")
        driver._root = _root()

        self.assertFalse(driver.upload_photo())
        self.assertIn("does not exist", logger.text())

    def _photo_driver(self, logger=None, adb=None):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self._tmp.write(b"fake jpeg bytes")
        self._tmp.close()
        driver = vd.AdbChallengeDriver(
            "dev:1", adb or FakeAdb(), logger=logger or Recording(),
            act=True, settle_seconds=0, photo_source_path=self._tmp.name)
        driver._root = _root(_button("Upload photo instead"))
        return driver

    def tearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            Path(tmp.name).unlink(missing_ok=True)

    def test_a_push_failure_declines_without_tapping_anything(self):
        adb = FakeAdb()
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=False):
            driver = self._photo_driver(adb=adb)
            self.assertFalse(driver.upload_photo())
        self.assertEqual(adb.taps, [])

    def test_a_picture_that_never_verifies_on_device_declines(self):
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=False):
            driver = self._photo_driver()
            self.assertFalse(driver.upload_photo())

    def test_no_upload_photo_instead_button_declines(self):
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True):
            driver = self._photo_driver()
            driver._root = _root()  # no button this time

            self.assertFalse(driver.upload_photo())

    def test_no_photo_cell_in_the_picker_declines(self):
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True), \
             mock.patch.object(ig, "_adb_capture_ui_dump",
                               return_value=_root()):  # picker with nothing in it
            driver = self._photo_driver()

            self.assertFalse(driver.upload_photo())

    def test_the_full_happy_path_taps_upload_photo_then_cell_then_submit(self):
        """The real screen sequence, confirmed live 2026-08-23 (Nikki new 1):
        'Upload photo instead' lands on an instructions screen ('Upload a
        photo'), which opens a bottom sheet ('Choose From Gallery' / 'Take
        photo') over the *same* screen. That hands off to Android's own
        permission dialog ("Allow Instagram to access photos and videos on
        this device?") -- a completely different package
        (com.android.permissioncontroller), confirmed via `dumpsys` and a
        screenshot, which is why nothing about it shows up as an Instagram
        screen. Only once that is answered does the real picker open, then
        'Submit' to confirm."""
        intermediate_root = _root(_button("Menu"), _button("Upload a photo"),
                                  _button("Submit"),
                                  _button("Record video instead"))
        sheet_root = _root(_button("Choose From Gallery"),
                           _button("Take photo"), _button("Upload a photo"),
                           _button("Submit"))
        allow_root = _root(_button("ALLOW"), _button("DON'T ALLOW"))
        picker_root = _root(
            '<node class="android.widget.ImageView" bounds="[0,300][300,600]" '
            'content-desc="Photo, taken today"/>',
        )
        confirm_root = _root(_button("Submit", bounds="[600,50][900,150]"))
        adb = FakeAdb()
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True), \
             mock.patch.object(ig, "_adb_capture_ui_dump",
                               side_effect=[intermediate_root, sheet_root,
                                           allow_root, allow_root,
                                           picker_root, confirm_root]):
            driver = self._photo_driver(adb=adb)

            self.assertTrue(driver.upload_photo())
        # Upload photo instead, Upload a photo, Choose From Gallery, Allow,
        # the photo cell, then Submit.
        self.assertEqual(len(adb.taps), 6)

    def test_the_permission_dialog_is_skipped_when_already_granted(self):
        """A phone that already granted photo access on an earlier attempt
        must not wait on a dialog that is never going to appear -- the Allow
        retry loop exhausts its attempts and moves on rather than blocking."""
        sheet_root = _root(_button("Choose From Gallery"))
        picker_root = _root(
            '<node class="android.widget.ImageView" bounds="[0,300][300,600]" '
            'content-desc="Photo, taken today"/>',
        )
        # sheet_root, sheet_root (gallery found), then an endless supply of
        # picker_root for the Allow retries (all miss) and everything after.
        import itertools
        dumps = itertools.chain([sheet_root, sheet_root],
                                itertools.repeat(picker_root))
        adb = FakeAdb()
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True), \
             mock.patch.object(ig, "_adb_capture_ui_dump",
                               side_effect=lambda *a, **kw: next(dumps)):
            driver = self._photo_driver(adb=adb)

            self.assertTrue(driver.upload_photo())
        # Upload photo instead, Choose From Gallery, straight to the photo
        # cell -- no Allow tap (never found), no Submit button on this
        # fixture.
        self.assertEqual(len(adb.taps), 3)

    def test_the_allow_dialog_is_retried_before_giving_up(self):
        """Confirmed live 2026-08-23: a single dump 3s after tapping 'Choose
        From Gallery' found nothing and the run moved straight to searching
        for a picker that had not opened yet, because
        GrantPermissionsActivity had not finished its transition in. Same
        patience as the gallery sheet's own retry."""
        sheet_root = _root(_button("Choose From Gallery"))
        pending_root = _root(_button("Menu"), _button("Upload a photo"),
                             _button("Submit"), _button("Record video "
                                                        "instead"))
        allow_root = _root(_button("ALLOW"), _button("DON'T ALLOW"))
        picker_root = _root(
            '<node class="android.widget.ImageView" bounds="[0,300][300,600]" '
            'content-desc="Photo, taken today"/>',
        )
        adb = FakeAdb()
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True), \
             mock.patch.object(ig, "_adb_capture_ui_dump",
                               side_effect=[sheet_root, sheet_root,
                                           pending_root, pending_root,
                                           allow_root, picker_root,
                                           picker_root]):
            driver = self._photo_driver(adb=adb)

            self.assertTrue(driver.upload_photo())
        # Upload photo instead, Choose From Gallery, Allow (found on the 3rd
        # attempt), the photo cell. No Submit button on this fixture.
        self.assertEqual(len(adb.taps), 4)

    def test_the_gallery_sheet_is_retried_before_giving_up(self):
        """Confirmed live 2026-08-23: the sheet was visibly on screen (a real
        screenshot showed it) while three uiautomator dumps 2s apart in a row
        still missed it. Patience, not a coordinate guess, is the fix --
        pinning that a late-arriving dump is still picked up."""
        stale_root = _root(_button("Menu"), _button("Upload a photo"),
                           _button("Submit"), _button("Record video instead"))
        sheet_root = _root(_button("Choose From Gallery"),
                           _button("Take photo"))
        picker_root = _root(
            '<node class="android.widget.ImageView" bounds="[0,300][300,600]" '
            'content-desc="Photo, taken today"/>',
        )
        import itertools
        dumps = itertools.chain(
            [stale_root, stale_root, stale_root, sheet_root],
            itertools.repeat(picker_root))
        adb = FakeAdb()
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True), \
             mock.patch.object(ig, "_adb_capture_ui_dump",
                               side_effect=lambda *a, **kw: next(dumps)):
            driver = self._photo_driver(adb=adb)

            self.assertTrue(driver.upload_photo())
        # Upload photo instead, Upload a photo, Choose From Gallery (found on
        # the 3rd retry), the photo cell. No Allow/Submit button on this
        # fixture.
        self.assertEqual(len(adb.taps), 4)

    def test_a_swallowed_tap_on_the_gallery_row_is_retried(self):
        """Confirmed live 2026-08-23: a screenshot taken right after tapping
        'Choose From Gallery' showed the identical, untouched sheet -- the
        tap simply did not register, at coordinates provably inside the
        row's own clickable bounds. This is a different failure from the
        dump missing the row (the other two tests above): the row was found
        every time here, the tap itself just didn't land the first time."""
        sheet_root = _root(_button("Choose From Gallery"))
        allow_root = _root(_button("ALLOW"), _button("DON'T ALLOW"))
        picker_root = _root(
            '<node class="android.widget.ImageView" bounds="[0,300][300,600]" '
            'content-desc="Photo, taken today"/>',
        )
        adb = FakeAdb()
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True), \
             mock.patch.object(ig, "_adb_capture_ui_dump",
                               side_effect=[sheet_root,   # "Upload a photo"? no
                                           sheet_root,   # gallery search: found
                                           sheet_root,   # after tap #1: still there
                                           allow_root,    # after tap #2: moved on
                                           allow_root,    # Allow search: found
                                           picker_root,   # the photo cell
                                           picker_root]):  # final Submit check
            driver = self._photo_driver(adb=adb)

            self.assertTrue(driver.upload_photo())
        # Upload photo instead, Choose From Gallery (x2 taps -- the first
        # didn't register), Allow, the photo cell. No Submit on this fixture.
        self.assertEqual(len(adb.taps), 5)

    def test_a_picker_reached_without_the_intermediate_screen_still_works(self):
        """Some accounts may skip straight to the picker -- the intermediate
        tap is best-effort, not required."""
        picker_root = _root(
            '<node class="android.widget.ImageView" bounds="[0,300][300,600]" '
            'content-desc="Photo, taken today"/>',
        )
        adb = FakeAdb()
        with mock.patch.object(ig, "_adb_push_media_to_device",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_exists",
                               return_value=True), \
             mock.patch.object(ig, "_adb_verify_remote_media_matches_local",
                               return_value=True), \
             mock.patch.object(ig, "_adb_wait_for_media_store_index",
                               return_value=True), \
             mock.patch.object(ig, "_adb_capture_ui_dump",
                               return_value=picker_root):
            driver = self._photo_driver(adb=adb)

            self.assertTrue(driver.upload_photo())
        # Upload photo instead, then straight to the photo cell (no Submit
        # button on this fixture, so no third tap).
        self.assertEqual(len(adb.taps), 2)


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
        driver._screencap = lambda force=False: screencap
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
        for name in ("read_screen", "refresh_feed", "choose_sms_method",
                     "enter_phone", "enter_code", "request_new_number",
                     "upload_photo", "capture_captcha_image", "enter_captcha"):
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


class RefreshFeedTest(unittest.TestCase):
    """Pulling the feed down, for the challenge Instagram withholds on the open.

    The gesture is derived from the screen size on purpose: a hard-coded
    coordinate is a pull-to-refresh on one phone model and a drag across the
    story tray on another, and this fleet is not one model.
    """

    def _sized(self, adb, size, act=True, foreground="com.instagram.android/.X"):
        driver = _driver(_root(), act=act, adb=adb)
        import adb_bot.automation.flows.instagram as ig
        self._saved = ig._adb_get_screen_size
        self._saved_fg = ig._adb_get_foreground_activity
        ig._adb_get_screen_size = lambda target, logger=None: size
        ig._adb_get_foreground_activity = lambda target, logger=None: foreground
        self.addCleanup(lambda: setattr(ig, "_adb_get_screen_size", self._saved))
        self.addCleanup(
            lambda: setattr(ig, "_adb_get_foreground_activity", self._saved_fg))
        return driver

    def test_it_swipes_down_the_middle_of_the_screen(self):
        adb = FakeAdb()
        driver = self._sized(adb, (1080, 2340))

        self.assertTrue(driver.refresh_feed())
        swipes = [c for c in adb.commands if "input swipe" in c]
        self.assertEqual(len(swipes), 1)
        x1, y1, x2, y2, duration = (int(n) for n in swipes[0].split()[-5:])
        self.assertEqual((x1, x2), (540, 540), "the swipe must be down the middle")
        self.assertLess(y1, y2, "a refresh pulls downward")
        self.assertGreater(y1, 0, "starting at the very top grabs the status bar")
        self.assertGreaterEqual(duration, 500,
                                "a fast flick scrolls the feed instead of refreshing")

    def test_an_unreadable_screen_size_refuses_rather_than_guessing(self):
        adb = FakeAdb()
        driver = self._sized(adb, None)

        self.assertFalse(driver.refresh_feed())
        self.assertEqual([c for c in adb.commands if "input swipe" in c], [],
                         "with no size there is no safe gesture to make")

    def test_observe_mode_does_not_touch_the_phone(self):
        adb = FakeAdb()
        driver = self._sized(adb, (1080, 2340), act=False)

        self.assertFalse(driver.refresh_feed())
        self.assertEqual(adb.commands, [])

    def test_it_will_not_swipe_when_instagram_is_not_in_front(self):
        """A downward swipe on the Android launcher pulls the notification
        shade down. `Luisa 7` ended a run with the shade open over a phone
        Instagram had quietly dropped out of, because this fired blind."""
        adb = FakeAdb()
        driver = self._sized(adb, (1080, 2340), foreground=None)

        self.assertFalse(driver.refresh_feed())
        self.assertEqual([c for c in adb.commands if "input swipe" in c], [],
                         "no gesture on a screen we have not identified")


class CurvedSwipeTest(unittest.TestCase):
    """`curved_swipe` -- a real multi-point gesture via uiautomator2, with a
    fall back to the plain straight-line swipe if u2 is unavailable for any
    reason. Never touches the real u2/adb -- `uiautomator2.connect` is
    mocked throughout."""

    def test_the_happy_path_drives_u2_and_releases_it(self):
        adb = FakeAdb()
        driver = _driver(adb=adb, rand=random.Random(1))
        fake_device = mock.MagicMock()

        with mock.patch("uiautomator2.connect", return_value=fake_device) as connect:
            self.assertTrue(driver.curved_swipe(100, 2000, 100, 500, "test"))

        connect.assert_called_once_with("dev:1")
        fake_device.swipe_points.assert_called_once()
        points, kwargs = fake_device.swipe_points.call_args
        self.assertEqual(points[0][0], (100, 2000))
        self.assertEqual(points[0][-1], (100, 500))
        self.assertIn("duration", kwargs)
        fake_device.stop_uiautomator.assert_called_once()
        # No straight-line fallback command sent on the happy path.
        self.assertEqual([c for c in adb.commands if "input swipe" in c], [])

    def test_a_broken_u2_connection_falls_back_to_a_straight_swipe(self):
        """u2 is a materially different mechanism from the raw dump the rest
        of this driver depends on -- a flow must never be worse off for
        having tried the curved path."""
        adb = FakeAdb()
        driver = _driver(adb=adb)

        with mock.patch("uiautomator2.connect", side_effect=RuntimeError("no agent")):
            self.assertTrue(driver.curved_swipe(100, 2000, 100, 500, "test"))

        swipes = [c for c in adb.commands if "input swipe" in c]
        self.assertEqual(len(swipes), 1)
        x1, y1, x2, y2, _duration = _swipe_fields(swipes[0])
        self.assertEqual((x1, y1), (100, 2000))
        self.assertEqual((x2, y2), (100, 500))

    def test_swipe_points_itself_raising_also_falls_back(self):
        adb = FakeAdb()
        driver = _driver(adb=adb)
        fake_device = mock.MagicMock()
        fake_device.swipe_points.side_effect = RuntimeError("gesture rejected")

        with mock.patch("uiautomator2.connect", return_value=fake_device):
            self.assertTrue(driver.curved_swipe(100, 2000, 100, 500, "test"))

        self.assertEqual(len([c for c in adb.commands if "input swipe" in c]), 1)

    def test_a_stop_uiautomator_failure_is_not_fatal(self):
        """The gesture already landed by the time release is attempted --
        failing to tear down cleanly must not be reported as the swipe
        itself having failed."""
        adb = FakeAdb()
        driver = _driver(adb=adb)
        fake_device = mock.MagicMock()
        fake_device.stop_uiautomator.side_effect = RuntimeError("already gone")

        with mock.patch("uiautomator2.connect", return_value=fake_device):
            self.assertTrue(driver.curved_swipe(100, 2000, 100, 500, "test"))

        fake_device.swipe_points.assert_called_once()
        # The teardown failure must not also trigger the straight-line
        # fallback -- the gesture already happened via u2.
        self.assertEqual([c for c in adb.commands if "input swipe" in c], [])

    def test_observe_mode_touches_nothing(self):
        adb = FakeAdb()
        driver = _driver(adb=adb, act=False)

        with mock.patch("uiautomator2.connect") as connect:
            self.assertFalse(driver.curved_swipe(100, 2000, 100, 500, "test"))

        connect.assert_not_called()
        self.assertEqual(adb.commands, [])


class BlankCaptchaTest(unittest.TestCase):
    """Instagram's captcha image does not always render.

    `Laila 3`, 2026-08-12: the image node was present and correctly sized
    (900x225) and **pure white** on every look across 90 seconds. Cropping that
    and sending it to 2captcha buys a solve of a blank rectangle, gets nonsense
    back, types it in, and burns one of the account's captcha attempts on a
    screen nobody could ever have read.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adbbot-captcha-"))
        try:
            import cv2  # noqa: F401
        except Exception:
            self.skipTest("cv2 unavailable")

    def _write(self, name, array):
        import cv2
        path = self.tmp / name
        cv2.imwrite(str(path), array)
        return path

    def test_a_flat_image_is_blank(self):
        import numpy as np
        white = np.full((225, 900, 3), 255, dtype=np.uint8)
        self.assertIs(vd.image_is_blank(self._write("white.png", white)), True)

    def test_an_image_with_marks_on_it_is_not_blank(self):
        import cv2
        import numpy as np
        strip = np.full((225, 900, 3), 255, dtype=np.uint8)
        cv2.putText(strip, "A7K2QX", (60, 150), cv2.FONT_HERSHEY_SIMPLEX,
                    4.0, (0, 0, 0), 8)
        self.assertIs(vd.image_is_blank(self._write("code.png", strip)), False)

    def test_an_unreadable_file_is_undecidable_not_blank(self):
        """None, not True: refusing on "could not judge" would ground the flow
        on any box where the screenshot did not write."""
        self.assertIsNone(vd.image_is_blank(self.tmp / "does-not-exist.png"))


class NewCaptchaTest(unittest.TestCase):
    """`Get a new code` -- the screen's own way out when the image is blank.

    Deliberately its own label list. Both this and the change-number link mean
    "try again", but on different screens: sharing them would let a captcha
    failure tap a change-number link and abandon a number about to receive.
    """

    def test_it_taps_the_new_image_link(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Get a new code")), act=True, adb=adb)
        self.assertTrue(driver.request_new_captcha())
        self.assertTrue(adb.taps)

    def test_it_reports_when_the_screen_offers_no_such_link(self):
        driver = _driver(_root(_button("Next")), act=True, adb=FakeAdb())
        self.assertFalse(driver.request_new_captcha())

    def test_it_does_not_borrow_the_change_number_link(self):
        """`Update mobile number` belongs to the code screen. Tapping it here
        would abandon a rented number that may be seconds from receiving."""
        adb = FakeAdb()
        driver = _driver(_root(_button("Update mobile number")), act=True, adb=adb)
        self.assertFalse(driver.request_new_captcha())
        self.assertEqual(adb.taps, [])

    def test_observe_mode_does_not_tap(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Get a new code")), act=False, adb=adb)
        self.assertFalse(driver.request_new_captcha())
        self.assertEqual(adb.commands, [])


class SwitchToSmsTest(unittest.TestCase):
    """"Send code via SMS" on the "we sent a code to WhatsApp" screen.

    Confirmed live 2026-08-23 (Cloe new 21, @cloe.5214): a number rented from
    an SMS pool can never receive a WhatsApp message, so three numbers in a
    row timed out on this exact screen before this existed.
    """

    def test_it_taps_the_switch_link(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Send code via SMS")), act=True, adb=adb)
        self.assertTrue(driver.offers_sms_instead())
        self.assertTrue(driver.request_sms_instead())
        self.assertTrue(adb.taps)

    def test_it_reports_when_the_screen_offers_no_such_link(self):
        """The normal case once already on SMS -- no button, nothing to tap."""
        driver = _driver(_root(_button("Update mobile number")), act=True,
                         adb=FakeAdb())
        self.assertFalse(driver.offers_sms_instead())
        self.assertFalse(driver.request_sms_instead())

    def test_it_does_not_borrow_the_change_number_link(self):
        """`Update mobile number` belongs to a different screen (the code
        timeout retry). Tapping it here would abandon a rented number."""
        adb = FakeAdb()
        driver = _driver(_root(_button("Update mobile number")), act=True,
                         adb=adb)
        self.assertFalse(driver.request_sms_instead())
        self.assertEqual(adb.taps, [])

    def test_observe_mode_does_not_tap(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Send code via SMS")), act=False, adb=adb)
        self.assertTrue(driver.offers_sms_instead())
        self.assertFalse(driver.request_sms_instead())
        self.assertEqual(adb.commands, [])


class LateRenderingCaptchaTest(unittest.TestCase):
    """Instagram draws the captcha screen before the image arrives.

    `Laila 3`, 2026-08-12: blank at t=0, a readable six-digit strip by t=15s,
    from the same node at the same bounds. A single screencap would have called
    that unreadable and asked for a replacement image it did not need.
    """

    def setUp(self):
        self.slept = []
        self._saved = vd.time.sleep
        vd.time.sleep = self.slept.append
        self.addCleanup(lambda: setattr(vd.time, "sleep", self._saved))

    def _driver_returning(self, sequence):
        """A driver whose crop is blank/readable per `sequence`."""
        driver = _driver(_root(), act=True, adb=FakeAdb())
        self.calls = []

        def fake_once():
            blank = sequence[min(len(self.calls), len(sequence) - 1)]
            self.calls.append(blank)
            return (None, True) if blank else ("/tmp/captcha.png", False)

        driver._capture_captcha_once = fake_once
        return driver

    def test_it_waits_for_an_image_that_has_not_rendered_yet(self):
        driver = self._driver_returning([True, True, False])
        self.assertEqual(driver.capture_captcha_image(), "/tmp/captcha.png")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.slept), 2, "one wait between each look")

    def test_an_image_already_there_is_returned_without_waiting(self):
        driver = self._driver_returning([False])
        self.assertEqual(driver.capture_captcha_image(), "/tmp/captcha.png")
        self.assertEqual(self.slept, [])

    def test_it_gives_up_when_the_image_never_renders(self):
        driver = self._driver_returning([True])
        self.assertIsNone(driver.capture_captcha_image(attempts=3))
        self.assertEqual(len(self.calls), 3)

    def test_a_failed_screencap_is_not_retried(self):
        """Blank means "wait, it may arrive". A broken screencap will not fix
        itself, and retrying it just spends the run's time."""
        driver = _driver(_root(), act=True, adb=FakeAdb())
        calls = []
        driver._capture_captcha_once = lambda: (calls.append(1), (None, False))[1]
        self.assertIsNone(driver.capture_captcha_image())
        self.assertEqual(len(calls), 1)


class GloginDroppedMidRunTest(unittest.TestCase):
    """A dropped glogin session answers every adb command with the same short
    text instead of real data. Confirmed live 2026-08-25: a 22-minute
    Gmail-install retry loop read this as "screen is empty" on every single
    poll, because nothing was watching for it -- the marker never reaches the
    classified screen text, only the raw bytes underneath it."""

    def setUp(self):
        self.adb = FakeAdb()
        self.driver = _driver(_root(), act=True, adb=self.adb)

    def _run_hidden(self, replies):
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            data = replies[min(len(calls) - 1, len(replies) - 1)]
            return mock.MagicMock(stdout=data)

        return fake, calls

    def test_the_dropped_session_is_reauthenticated_and_the_shot_retried(self):
        fake, calls = self._run_hidden([
            b"error: you should run glogin to login first",
            b"\x89PNG-real-bytes-here",
        ])
        with mock.patch.object(vd, "run_hidden", fake):
            result = self.driver._screencap(force=True)

        self.assertEqual(result, b"\x89PNG-real-bytes-here")
        self.assertEqual(self.adb.reauth_calls, ["dev:1"])
        self.assertEqual(len(calls), 2, "the screenshot must actually be retried")

    def test_a_failed_reauth_gives_up_rather_than_looping(self):
        fake, calls = self._run_hidden([
            b"error: you should run glogin to login first",
        ])
        self.adb.reauth_result = False
        with mock.patch.object(vd, "run_hidden", fake):
            result = self.driver._screencap(force=True)

        self.assertIsNone(result)
        self.assertEqual(len(calls), 1, "no data to retry with after a failed reauth")

    def test_a_real_screenshot_never_triggers_reauth(self):
        """The marker check only applies to short replies -- a real PNG's
        opening bytes must never accidentally be read as the error text."""
        fake, calls = self._run_hidden([b"\x89PNG" + b"\x00" * 5000])
        with mock.patch.object(vd, "run_hidden", fake):
            result = self.driver._screencap(force=True)

        self.assertEqual(len(result), 5004)
        self.assertEqual(self.adb.reauth_calls, [])
        self.assertEqual(len(calls), 1)

    def test_read_screen_recovers_once_the_dropped_session_is_fixed(self):
        """emanuelnewbyp601@gmail.com, 2026-08-25: glogin dropped mid-run, the
        UI dump and the OCR fallback both went silently empty on every single
        poll, and a 22-minute Gmail-install retry loop read that as "no
        Install button anywhere" -- neither path had any reauth of its own.
        `read_screen()` must notice an all-empty read, use `_screencap()`
        (which already knows how to reauthenticate) as a probe, and retry the
        dump once the session is actually back."""
        dump_calls = []

        def fake_dump():
            dump_calls.append(1)
            if len(dump_calls) == 1:
                return None, None
            return _root(_button("Install")), b"<xml/>"

        self.driver._dump = fake_dump
        self.driver.screenshots = False  # matches every round script tonight

        fake, _ = self._run_hidden([
            b"error: you should run glogin to login first",
            b"\x89PNG-real-bytes-here",
        ])
        with mock.patch.object(vd, "run_hidden", fake):
            text = self.driver.read_screen()

        self.assertIn("install", text)
        self.assertEqual(self.driver._source, "ui-dump")
        self.assertEqual(len(dump_calls), 2, "the dump must be retried once "
                                             "the session recovers")

    def test_read_screen_does_not_retry_the_dump_when_the_probe_stays_dead(self):
        """A genuinely offline phone must not get an extra dump call it has
        no chance of answering -- the probe failing is itself the answer."""
        dump_calls = []
        self.driver._dump = lambda: (dump_calls.append(1), (None, None))[1]
        self.driver.screenshots = False

        fake, _ = self._run_hidden([b""])
        with mock.patch.object(vd, "run_hidden", fake):
            text = self.driver.read_screen()

        self.assertEqual(text, "")
        self.assertEqual(len(dump_calls), 1)


class DismissConfirmationTest(unittest.TestCase):
    """`Done` on the "You're back on Instagram" screen a cleared chain ends on."""

    def test_it_taps_done(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Done")), act=True, adb=adb)
        self.assertTrue(driver.dismiss_confirmation())
        self.assertTrue(adb.taps)

    def test_no_such_button_is_not_an_error(self):
        """The chain is already solved when this runs; nothing about the result
        depends on it."""
        driver = _driver(_root(_button("Next")), act=True, adb=FakeAdb())
        self.assertFalse(driver.dismiss_confirmation())

    def test_observe_mode_does_not_tap(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Done")), act=False, adb=adb)
        self.assertFalse(driver.dismiss_confirmation())
        self.assertEqual(adb.commands, [])


class PermissionDialogLaunchTest(unittest.TestCase):
    """An Android permission dialog is not a failed launch.

    `632451306307322212`, 2026-08-12: Instagram was running fine, but
    `com.android.permissioncontroller/.GrantPermissionsActivity` held the
    foreground for the whole 75s launch window. The probe refused to read the
    screen -- right, since whatever is on it says nothing about the account --
    but nothing was ever going to dismiss the dialog, so the profile was simply
    unreachable. Re-issuing the start intent, which is what the loop did for
    75 seconds, does nothing at all in that state.
    """

    class _Clock:
        """Stands in for the `time` module inside verification_probe.

        A fake clock rather than a stubbed `sleep`: with only `sleep` faked the
        loop spins against a *real* 75-second deadline, which is 75 seconds of
        wall-clock per test. Swapping the module attribute also leaves the
        global `time` module alone.
        """

        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    def setUp(self):
        from adb_bot.automation import verification_probe as vp
        self.vp = vp
        self.clock = self._Clock()
        self._real_time = vp.time
        vp.time = self.clock
        self.addCleanup(lambda: setattr(vp, "time", self._real_time))

    class _Logger:
        def __init__(self):
            self.lines = []

        def info(self, message, *args):
            self.lines.append(message % args if args else message)

        warning = error = info

        def text(self):
            return " | ".join(self.lines)

    def _run(self, foregrounds, grant_returns=True):
        """Drive `_open_instagram` over a scripted sequence of foreground apps."""
        vp, calls = self.vp, {"grants": 0}
        ig = __import__("adb_bot.automation.flows.instagram",
                        fromlist=["instagram"])
        state = {"i": 0}

        def foreground_activity(target, logger=None):
            i = min(state["i"], len(foregrounds) - 1)
            state["i"] += 1
            app = foregrounds[i]
            return app if vp.INSTAGRAM_PACKAGE in app else None

        def clear(target, adb_client, logger):
            calls["grants"] += 1
            return grant_returns

        adb = FakeAdb()
        adb.run_command = lambda cmd: (
            f"package:{vp.INSTAGRAM_PACKAGE}" if "pm list packages" in cmd else "")

        saved_fg = ig._adb_get_foreground_activity
        saved_app = vp._foreground_app
        saved_clear = vp._clear_permission_dialog
        ig._adb_get_foreground_activity = foreground_activity
        vp._foreground_app = lambda t, a: foregrounds[min(state["i"] - 1,
                                                         len(foregrounds) - 1)]
        vp._clear_permission_dialog = clear
        self.addCleanup(lambda: setattr(ig, "_adb_get_foreground_activity", saved_fg))
        self.addCleanup(lambda: setattr(vp, "_foreground_app", saved_app))
        self.addCleanup(lambda: setattr(vp, "_clear_permission_dialog", saved_clear))

        logger = self._Logger()
        ok = vp._open_instagram("dev:1", adb, logger)
        return ok, calls["grants"], logger

    def test_a_permission_dialog_is_granted_and_the_launch_succeeds(self):
        ok, grants, logger = self._run([
            "com.android.permissioncontroller/.GrantPermissionsActivity",
            f"{self.vp.INSTAGRAM_PACKAGE}/.activity.MainTabActivity",
        ])
        self.assertTrue(ok)
        self.assertEqual(grants, 1)
        self.assertIn("permission dialog", logger.text())

    def test_a_chain_of_dialogs_is_worked_through(self):
        perm = "com.android.permissioncontroller/.GrantPermissionsActivity"
        ok, grants, _ = self._run(
            [perm, perm, perm, f"{self.vp.INSTAGRAM_PACKAGE}/.activity.MainTabActivity"])
        self.assertTrue(ok)
        self.assertEqual(grants, 3)

    def test_a_dialog_that_never_clears_still_ends_the_launch(self):
        """Bounded on purpose: a dialog that keeps coming back must not hold a
        phone for ever, or an unattended pass stops on its first bad profile."""
        ok, _, _ = self._run(
            ["com.android.permissioncontroller/.GrantPermissionsActivity"] * 200,
            grant_returns=False)
        self.assertFalse(ok)


class CaptchaWordmarkTest(unittest.TestCase):
    """The Instagram wordmark passes every shape test a captcha strip does.

    On 2026-08-12 a run whose captcha image had not been drawn yet cropped the
    header logo instead, and 2captcha read it back as "Instagram" -- a paid
    solve of a logo, typed into the answer box. Only its width gives it away.
    """

    def test_the_wordmark_shape_alone_still_looks_like_a_captcha(self):
        """Which is why the width fraction is needed at all."""
        self.assertTrue(vd.looks_like_captcha(330, 156))

    def test_a_real_captcha_strip_spans_most_of_the_screen(self):
        for width, height in ((900, 225), (916, 241)):
            self.assertGreaterEqual(width / 1080, vd._CAPTCHA_MIN_WIDTH_FRACTION,
                                    f"{width}x{height}")

    def test_the_wordmark_does_not(self):
        self.assertLess(330 / 1080, vd._CAPTCHA_MIN_WIDTH_FRACTION)


class OcrFallbackTest(unittest.TestCase):
    """The fallback for screens that produce no UI dump, which never once ran.

    It is gated on a `flow` being passed in, and neither the probe nor the
    runner passes one -- so every such screen was read as empty, classified as
    "not a working Instagram", and handed to a person. `Luisa 9` and
    `Jasmin 6` both went that way on 2026-08-12.
    """

    def _driver_with_empty_dump(self, flow=None):
        driver = vd.AdbChallengeDriver("dev:1", FakeAdb(), logger=None,
                                       flow=flow, act=True, settle_seconds=0,
                                       screenshots=False)
        driver._dump = lambda: (None, None)
        return driver

    def test_a_caller_that_supplies_no_flow_still_gets_ocr(self):
        driver = self._driver_with_empty_dump()
        provider = driver._ocr_provider()
        self.assertIsNotNone(provider)
        self.assertTrue(hasattr(provider, "_ocr_screen_text"))

    def test_an_explicit_flow_still_wins(self):
        class Explicit:
            def _ocr_screen_text(self, target, logger=None):
                return "from the caller's flow"

        flow = Explicit()
        self.assertIs(self._driver_with_empty_dump(flow)._ocr_provider(), flow)

    def test_ocr_text_is_used_when_the_dump_is_empty(self):
        class Explicit:
            def _ocr_screen_text(self, target, logger=None):
                return "confirm you're human enter the code from the image"

        driver = self._driver_with_empty_dump(Explicit())
        self.assertIn("confirm you're human", driver.read_screen())
        self.assertEqual(driver._source, "ocr")

    def test_a_dump_that_worked_is_never_replaced_by_ocr(self):
        """OCR is the fallback, not a second opinion -- it is slower and less
        exact, and the dump is what every tap is taken from."""
        class Explicit:
            called = False

            def _ocr_screen_text(self, target, logger=None):
                Explicit.called = True
                return "ocr text"

        driver = _driver(_root(_button("Send code")), act=True, adb=FakeAdb())
        driver.flow = Explicit()
        driver._dump = lambda: (_root(_button("Send code")), b"<xml/>")
        driver.read_screen()
        self.assertFalse(Explicit.called)

    def test_a_broken_ocr_provider_does_not_cost_the_read(self):
        class Exploding:
            def _ocr_screen_text(self, target, logger=None):
                raise RuntimeError("tesseract is gone")

        driver = self._driver_with_empty_dump(Exploding())
        self.assertEqual(driver.read_screen(), "")


class AdvanceIntroTest(unittest.TestCase):
    """Continue on the screen that introduces a challenge."""

    def test_it_presses_continue(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Continue")), act=True, adb=adb)
        self.assertTrue(driver.advance_intro())
        self.assertTrue(adb.taps)

    def test_no_button_is_reported_not_guessed_at(self):
        driver = _driver(_root(_button("Send code")), act=True, adb=FakeAdb())
        self.assertFalse(driver.advance_intro())

    def test_observe_mode_does_not_tap(self):
        adb = FakeAdb()
        driver = _driver(_root(_button("Continue")), act=False, adb=adb)
        self.assertFalse(driver.advance_intro())
        self.assertEqual(adb.commands, [])


def _swipe_fields(command: str) -> tuple[int, int, int, int, int]:
    match = re.search(r"input swipe (\d+) (\d+) (\d+) (\d+) (\d+)", command)
    return tuple(int(g) for g in match.groups())


class HumanTapTimingTest(unittest.TestCase):
    """`_tap` no longer sends a bare, instant `input tap` -- every tap goes
    down, holds for a jittered span, then lifts, at a point nudged a few
    pixels off the target's own centre. Real timing/pixel realism is only
    worth anything if it is actually never the identical number twice, so
    that is what most of this proves, not just "it still runs"."""

    def test_a_tap_is_never_a_bare_input_tap(self):
        adb = FakeAdb()
        driver = _driver(adb=adb)
        driver._tap((500, 150), "something")
        self.assertEqual(len(adb.commands), 1)
        self.assertNotIn("input tap", adb.commands[0])
        x1, y1, x2, y2, _duration = _swipe_fields(adb.commands[0])
        self.assertEqual((x1, y1), (x2, y2), "a tap presses and lifts at "
                                            "the same point")

    def test_the_dwell_duration_is_not_a_single_fixed_number(self):
        adb = FakeAdb()
        driver = _driver(adb=adb)
        for _ in range(20):
            driver._tap((500, 150), "something")
        durations = {_swipe_fields(c)[4] for c in adb.commands}
        self.assertGreater(len(durations), 1,
                           "20 taps all held for the exact same duration")
        for d in durations:
            self.assertGreaterEqual(d, human_timing.DWELL_MIN_MS)
            self.assertLessEqual(d, human_timing.DWELL_MAX_MS)

    def test_a_held_tap_keeps_the_original_150ms_floor(self):
        """The RecyclerView ripple-timing fix (2026-08-23) needed *at least*
        ~150ms -- randomising dwell must never quietly drop back under the
        floor that fix was for."""
        adb = FakeAdb()
        driver = _driver(adb=adb)
        for _ in range(20):
            driver._tap((500, 150), "a gallery row", press=True)
        durations = [_swipe_fields(c)[4] for c in adb.commands]
        self.assertGreaterEqual(min(durations), 150)

    def test_jitter_never_leaves_the_targets_own_bounds(self):
        bounds = (400, 500, 600, 560)  # a 200x60 button
        adb = FakeAdb()
        driver = _driver(adb=adb)
        for _ in range(30):
            driver._tap((500, 530), "a button", bounds=bounds)
        x1, y1, x2, y2 = bounds
        for c in adb.commands:
            x, y, _x2, _y2, _d = _swipe_fields(c)
            self.assertTrue(x1 <= x <= x2, f"{x} left the button's own box")
            self.assertTrue(y1 <= y <= y2, f"{y} left the button's own box")

    def test_a_raw_coordinate_with_no_bounds_gets_a_small_fixed_jitter(self):
        adb = FakeAdb()
        driver = _driver(adb=adb)
        for _ in range(30):
            driver._tap((500, 150), "a reCAPTCHA grid cell")
        for c in adb.commands:
            x, y, _x2, _y2, _d = _swipe_fields(c)
            self.assertLessEqual(abs(x - 500), human_timing.JITTER_NO_BOUNDS_PX)
            self.assertLessEqual(abs(y - 150), human_timing.JITTER_NO_BOUNDS_PX)

    def test_a_seeded_rand_makes_taps_reproducible(self):
        """Not for production -- for a test or a debugging session that needs
        the exact same sequence twice."""
        adb_a, adb_b = FakeAdb(), FakeAdb()
        driver_a = _driver(adb=adb_a, rand=random.Random(42))
        driver_b = _driver(adb=adb_b, rand=random.Random(42))
        for _ in range(5):
            driver_a._tap((500, 150), "x")
            driver_b._tap((500, 150), "x")
        self.assertEqual(adb_a.commands, adb_b.commands)
