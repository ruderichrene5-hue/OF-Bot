"""The new-device onboarding / ads-consent chain a fresh profile hits before the
feed (see the "Set up on new device" screenshots): Get started -> select
free-with-ads + Continue -> Agree cookies -> OK -> Allow contacts -> Android
ALLOW. `handle_blocking_prompts` must walk each screen via the UI dump, tap the
right button, and never tap "Don't allow" / "Subscribe".
"""

import xml.etree.ElementTree as ET
from unittest import TestCase
from unittest.mock import Mock, patch

from adb_bot.automation.flows import interruptions


def _dump(xml: str):
    return ET.fromstring(xml)


# --- Each blocker screen as a UI dump, with realistic button bounds ----------

SCREEN_ADS_INTRO = _dump("""<hierarchy>
  <node text="Choose if we process your data for ads"/>
  <node text="As part of laws in your region, you can choose whether you consent"/>
  <node text="Get started" clickable="true" bounds="[100,1300][900,1400]"/>
</hierarchy>""")

# Subscription choice: Continue is present but disabled until an option is
# picked. After selecting the radio we re-dump and Continue is enabled.
SCREEN_SUBSCRIPTION = _dump("""<hierarchy>
  <node text="Want to subscribe or continue using our products free of charge with ads?"/>
  <node text="Subscribe to use without ads" clickable="true" bounds="[80,600][1000,760]"/>
  <node text="Use free of charge with ads" clickable="true" bounds="[80,820][1000,980]"/>
  <node text="Continue" enabled="false" clickable="false" bounds="[100,1500][900,1600]"/>
</hierarchy>""")

SCREEN_SUBSCRIPTION_SELECTED = _dump("""<hierarchy>
  <node text="Want to subscribe or continue using our products free of charge with ads?"/>
  <node text="Use free of charge with ads" clickable="true" bounds="[80,820][1000,980]"/>
  <node text="Continue" enabled="true" clickable="true" bounds="[100,1500][900,1600]"/>
</hierarchy>""")

SCREEN_COOKIES = _dump("""<hierarchy>
  <node text="Use cookies on our products to personalise your ads and measure how they perform"/>
  <node text="By selecting Agree, you consent to Meta processing your data for ads"/>
  <node text="Agree" clickable="true" bounds="[100,1500][900,1600]"/>
</hierarchy>""")

SCREEN_MANAGE_ADS = _dump("""<hierarchy>
  <node text="You can manage your ad experience"/>
  <node text="Continue with personalised ads" clickable="true" bounds="[80,500][1000,650]"/>
  <node text="Switch to less-personalised ads" clickable="true" bounds="[80,800][1000,950]"/>
  <node text="OK" clickable="true" bounds="[100,1500][900,1600]"/>
</hierarchy>""")

SCREEN_CONTACTS_INTRO = _dump("""<hierarchy>
  <node text="Set up on new device"/>
  <node text="Allow access to contacts to find people to follow"/>
  <node text="Allow" clickable="true" bounds="[100,1400][900,1500]"/>
  <node text="Skip" clickable="true" bounds="[100,1550][900,1620]"/>
</hierarchy>""")

SCREEN_CONTACTS_ANDROID = _dump("""<hierarchy>
  <node text="Allow Instagram to access your contacts?"/>
  <node text="ALLOW" clickable="true" bounds="[100,900][900,1000]"/>
  <node text="DON'T ALLOW" clickable="true" bounds="[100,1050][900,1150]"/>
</hierarchy>""")

SCREEN_FEED = _dump("<hierarchy><node text='Stories'/><node text='For you'/></hierarchy>")


class OnboardingChainTest(TestCase):
    def _run_chain(self, dumps):
        taps = []
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump", side_effect=dumps), \
             patch("adb_bot.automation.flows.instagram._adb_tap",
                   side_effect=lambda t, x, y, *a, **k: taps.append((x, y))), \
             patch("adb_bot.automation.flows.interruptions.time.sleep"):
            handled = interruptions.handle_blocking_prompts("dev", Mock())
        return handled, taps

    def test_walks_full_new_device_chain(self):
        # Note: the subscription screen consumes an EXTRA dump (re-read after
        # selecting the radio, to tap the now-enabled Continue).
        dumps = [
            SCREEN_ADS_INTRO,               # round 1 -> Get started
            SCREEN_SUBSCRIPTION,            # round 2 -> select radio...
            SCREEN_SUBSCRIPTION_SELECTED,   #           ...re-dump -> Continue
            SCREEN_COOKIES,                 # round 3 -> Agree
            SCREEN_MANAGE_ADS,              # round 4 -> OK
            SCREEN_CONTACTS_INTRO,          # round 5 -> Allow
            SCREEN_CONTACTS_ANDROID,        # round 6 -> ALLOW
            SCREEN_FEED,                    # round 7 -> nothing known, stop
        ]
        handled, taps = self._run_chain(dumps)

        self.assertTrue(handled)
        self.assertEqual(taps, [
            (500, 1350),  # Get started
            (540, 900),   # Use free of charge with ads (radio)
            (500, 1550),  # Continue (now enabled)
            (500, 1550),  # Agree
            (500, 1550),  # OK
            (500, 1450),  # Allow (Instagram contacts screen)
            (500, 950),   # ALLOW (Android dialog) -- never DON'T ALLOW
        ])

    def test_never_taps_dont_allow_or_subscribe(self):
        _handled, taps = self._run_chain([SCREEN_CONTACTS_ANDROID, SCREEN_FEED])
        # Only the ALLOW centre (500, 950), never DON'T ALLOW (500, 1100).
        self.assertEqual(taps, [(500, 950)])

    def test_stops_when_feed_is_reached(self):
        handled, taps = self._run_chain([SCREEN_FEED])
        self.assertFalse(handled)
        self.assertEqual(taps, [])

    def test_detect_classifies_onboarding(self):
        with patch("adb_bot.automation.flows.instagram._adb_capture_ui_dump",
                   return_value=SCREEN_ADS_INTRO):
            kind = interruptions.detect_interruption("dev")[0]
        self.assertEqual(kind, interruptions.INTERRUPTION_ONBOARDING)


if __name__ == "__main__":
    import unittest
    unittest.main()
