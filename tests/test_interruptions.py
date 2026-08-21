import xml.etree.ElementTree as ET
from unittest import TestCase
from unittest.mock import Mock, patch

from adb_bot.automation.flows import interruptions


def _dump(xml: str):
    return ET.fromstring(xml)


class InterruptionDetectionTest(TestCase):
    def _detect(self, xml, expect=None):
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=_dump(xml)):
            return interruptions.detect_interruption("dev", expect=expect)[0]

    def test_detects_human_verification(self):
        xml = """<hierarchy>
          <node text="Confirm you're human to use your account, shirinsminch"/>
          <node text="Continue"/>
          <node text="Takes about 30 seconds"/>
        </hierarchy>"""
        self.assertEqual(self._detect(xml), interruptions.INTERRUPTION_HUMAN_VERIFICATION)

    def test_detects_location_permission_screen(self):
        xml = """<hierarchy>
          <node text="To use Location services, allow Instagram to access your location"/>
          <node text="Continue"/>
        </hierarchy>"""
        self.assertEqual(self._detect(xml), interruptions.INTERRUPTION_PERMISSION)

    def test_detects_android_permission_dialog(self):
        xml = """<hierarchy>
          <node text="Allow Instagram to access this device's location?"/>
          <node text="WHILE USING THE APP"/>
          <node text="ONLY THIS TIME"/>
          <node text="DON'T ALLOW"/>
        </hierarchy>"""
        self.assertEqual(self._detect(xml), interruptions.INTERRUPTION_PERMISSION)

    def test_detects_stuck_edit_profile(self):
        # Title present but none of the form fields -> blank/spinner page.
        xml = """<hierarchy><node text="Edit profile"/></hierarchy>"""
        self.assertEqual(
            self._detect(xml, expect="edit_profile"),
            interruptions.INTERRUPTION_STUCK_EDIT_PROFILE,
        )

    def test_loaded_edit_profile_is_not_an_interruption(self):
        xml = """<hierarchy>
          <node text="Edit profile"/><node text="Name"/>
          <node text="Username"/><node text="Pronouns"/><node text="Bio"/>
        </hierarchy>"""
        self.assertEqual(self._detect(xml, expect="edit_profile"), interruptions.INTERRUPTION_NONE)


class PermissionGrantingTest(TestCase):
    def test_taps_while_using_the_app_and_never_dont_allow(self):
        dialog = _dump("""<hierarchy>
          <node text="Allow Instagram to access this device's location?"/>
          <node text="WHILE USING THE APP" clickable="true" bounds="[100,1000][900,1100]"/>
          <node text="ONLY THIS TIME" clickable="true" bounds="[100,1150][900,1250]"/>
          <node text="DON'T ALLOW" clickable="true" bounds="[100,1300][900,1400]"/>
        </hierarchy>""")
        clean = _dump("<hierarchy><node text='Edit profile'/><node text='Username'/></hierarchy>")

        adb = Mock()
        taps = []
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", side_effect=[dialog, clean]), \
             patch("adb_bot.automation.flows.instagram._adb_tap", side_effect=lambda t, x, y, *a, **k: taps.append((x, y))), \
             patch("adb_bot.automation.flows.interruptions.time.sleep"):
            handled = interruptions.handle_permission_prompts("dev", adb)

        self.assertTrue(handled)
        # Tapped the centre of "WHILE USING THE APP" (500, 1050) -- not DON'T ALLOW.
        self.assertEqual(taps, [(500, 1050)])

    def test_grants_photos_permission_with_allow_all(self):
        dialog = _dump("""<hierarchy>
          <node text="Allow Instagram to access photos and videos?"/>
          <node text="Allow all" clickable="true" bounds="[100,1000][900,1100]"/>
          <node text="Don't allow" clickable="true" bounds="[100,1200][900,1300]"/>
        </hierarchy>""")
        clean = _dump("<hierarchy><node text='Username'/></hierarchy>")

        taps = []
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", side_effect=[dialog, clean]), \
             patch("adb_bot.automation.flows.instagram._adb_tap", side_effect=lambda t, x, y, *a, **k: taps.append((x, y))), \
             patch("adb_bot.automation.flows.interruptions.time.sleep"):
            interruptions.handle_permission_prompts("dev", Mock())

        self.assertEqual(taps, [(500, 1050)])  # "Allow all", not "Don't allow"


class CheckAndHandleOutcomeTest(TestCase):
    def test_human_verification_outcome(self):
        xml = "<hierarchy><node text=\"Confirm you're human\"/></hierarchy>"
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", return_value=_dump(xml)):
            outcome = interruptions.check_and_handle("dev", Mock())
        self.assertEqual(outcome, interruptions.OUTCOME_HUMAN_VERIFICATION)

    def test_permission_outcome_is_restart(self):
        dialog = _dump("""<hierarchy>
          <node text="Allow Instagram to access this device's location?"/>
          <node text="WHILE USING THE APP" clickable="true" bounds="[100,1000][900,1100]"/>
        </hierarchy>""")
        clean = _dump("<hierarchy><node text='Username'/></hierarchy>")
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", side_effect=[dialog, dialog, clean]), \
             patch("adb_bot.automation.flows.instagram._adb_tap"), \
             patch("adb_bot.automation.flows.interruptions.time.sleep"):
            outcome = interruptions.check_and_handle("dev", Mock())
        self.assertEqual(outcome, interruptions.OUTCOME_RESTART)


_LOGIN_NOTICE = """<hierarchy>
  <node text="We Detected An Unusual Login"/>
  <node text="Someone logged into your account from a device you don't usually use. Was this you?"/>
  <node text="This Was Me" clickable="true" bounds="[100,1000][900,1100]"/>
  <node text="This Wasn't Me" clickable="true" bounds="[100,1150][900,1250]"/>
</hierarchy>"""

_FEED = """<hierarchy>
  <node content-desc="Search and explore"/>
  <node text="Add to story"/>
</hierarchy>"""


class LoginConfirmTest(TestCase):
    """The "Was this you?" notice: the bot presses the button itself.

    Before this, the screen's own wording ("we detected...") put it in the
    human-verification bucket, so a profile stopped posting until a person
    tapped one button on it.
    """

    def _run(self, screens, func="check_and_handle"):
        """Run a handler over a scripted sequence of screens, collecting taps."""
        dumps = [_dump(x) if isinstance(x, str) else x for x in screens]
        taps = []
        adb = Mock()
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", side_effect=dumps), \
             patch("adb_bot.automation.flows.instagram._adb_tap",
                   side_effect=lambda t, x, y, *a, **k: taps.append((x, y))), \
             patch("adb_bot.automation.flows.interruptions.time.sleep"):
            result = getattr(interruptions, func)("dev", adb)
        return result, taps

    def test_detects_the_notice(self):
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump",
                   return_value=_dump(_LOGIN_NOTICE)):
            kind = interruptions.detect_interruption("dev")[0]
        self.assertEqual(kind, interruptions.INTERRUPTION_LOGIN_CONFIRM)

    def test_taps_this_was_me_and_never_this_wasnt_me(self):
        """The single most expensive wrong tap on the fleet: the refusing
        button starts a password reset and takes the account away for good."""
        handled, taps = self._run([_LOGIN_NOTICE, _FEED], func="handle_login_confirm")
        self.assertTrue(handled)
        # Centre of "This Was Me" (500, 1050) -- not "This Wasn't Me" (500, 1200).
        self.assertEqual(taps, [(500, 1050)])

    def test_check_and_handle_clears_it_and_asks_for_a_restart(self):
        # detect, then the handler's look + its confirming look, then the
        # re-classify afterwards.
        outcome, taps = self._run([_LOGIN_NOTICE, _LOGIN_NOTICE, _FEED, _FEED])
        self.assertEqual(outcome, interruptions.OUTCOME_RESTART)
        self.assertEqual(taps, [(500, 1050)])
        self.assertIsNone(interruptions.account_flag_for(outcome),
                          "clearing a login notice must not flag the account")

    def test_an_unpressable_notice_falls_back_to_the_old_behaviour(self):
        """No button we recognise -> report it exactly as before this existed.

        The fallback direction is the whole safety argument: this change can
        clear a screen or leave it alone, never make one worse.
        """
        odd = """<hierarchy>
          <node text="We detected an unusual login. Was this you?"/>
          <node text="Ja, das war ich" clickable="true" bounds="[100,1000][900,1100]"/>
        </hierarchy>"""
        outcome, taps = self._run([odd, odd])
        self.assertEqual(outcome, interruptions.OUTCOME_HUMAN_VERIFICATION)
        self.assertEqual(taps, [], "nothing may be tapped when the label is unknown")

    def test_a_notice_that_will_not_go_away_is_left_to_a_person(self):
        screens = [_LOGIN_NOTICE] * 8
        outcome, taps = self._run(screens)
        self.assertEqual(outcome, interruptions.OUTCOME_HUMAN_VERIFICATION)
        self.assertLessEqual(len(taps), 3, "must not keep tapping a screen that never changes")

    def test_a_checkpoint_behind_the_notice_is_reported(self):
        """Confirming a login can reveal the real thing queued behind it."""
        checkpoint = """<hierarchy>
          <node text="Confirm you're human to use your account"/>
          <node text="Takes about 30 seconds"/>
        </hierarchy>"""
        outcome, _taps = self._run([_LOGIN_NOTICE, _LOGIN_NOTICE, checkpoint, checkpoint])
        self.assertEqual(outcome, interruptions.OUTCOME_HUMAN_VERIFICATION)

    def test_a_real_checkpoint_is_still_a_checkpoint(self):
        """The notice handling must not swallow the screens a person owns."""
        checkpoint = """<hierarchy>
          <node text="Confirm you're human to use your account, shirinsminch"/>
          <node text="Takes about 30 seconds"/>
          <node text="Continue" clickable="true" bounds="[100,1000][900,1100]"/>
        </hierarchy>"""
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump",
                   return_value=_dump(checkpoint)):
            kind = interruptions.detect_interruption("dev")[0]
        self.assertEqual(kind, interruptions.INTERRUPTION_HUMAN_VERIFICATION)

    def test_the_blocking_chain_clears_it_too(self):
        """It turns up between a launch and the feed, so the chain walker that
        clears the onboarding screens has to know it as well."""
        handled, taps = self._run([_LOGIN_NOTICE, _FEED], func="handle_blocking_prompts")
        self.assertTrue(handled)
        self.assertEqual(taps, [(500, 1050)])
