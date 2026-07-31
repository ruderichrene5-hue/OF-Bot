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
