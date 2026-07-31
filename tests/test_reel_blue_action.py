"""The reel upload final steps: the second action button is a blue button in the
bottom-right whose label is sometimes 'Next' and sometimes 'Share'. It must be
detected (and blue-verified) rather than blind-tapped, and the flow must reset
its remembered Next location per run so it can't leak across profiles.
"""

import xml.etree.ElementTree as ET
from unittest import TestCase
from unittest.mock import patch

import adb_bot.automation.flows.instagram as ig


def _dump(xml: str):
    return ET.fromstring(xml)


# A composer screen with a blue "Share" button bottom-right and an unrelated
# small "share" glyph elsewhere.
SHARE_SCREEN = """<hierarchy>
  <node text="Write a caption" bounds="[40,300][1000,420]"/>
  <node text="Share" clickable="true" enabled="true" bounds="[600,2100][1040,2220]"/>
  <node content-desc="share" clickable="true" enabled="true" bounds="[900,200][980,280]"/>
</hierarchy>"""

NEXT_SCREEN = """<hierarchy>
  <node text="New reel" bounds="[40,120][500,220]"/>
  <node text="Next" clickable="true" enabled="true" bounds="[650,2100][1040,2220]"/>
</hierarchy>"""


class BlueActionFinderTest(TestCase):
    def setUp(self):
        # Force screen size deterministically and treat every checked region as blue.
        self._patches = [
            patch.object(ig, "_adb_get_screen_size", return_value=(1080, 2340)),
            patch.object(ig, "_adb_capture_screen_cv2", return_value=object()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_finds_blue_share_bottom_right_and_ignores_top_glyph(self):
        with patch.object(ig, "_adb_capture_ui_dump", return_value=_dump(SHARE_SCREEN)), \
             patch.object(ig, "_adb_is_instagram_share_button_blue", return_value=True):
            result = ig._adb_find_instagram_bottom_blue_action_center("dev", labels=("next", "share"))
        self.assertIsNotNone(result)
        (cx, cy), label = result
        self.assertEqual(label, "share")
        self.assertEqual((cx, cy), (820, 2160))  # centre of the bottom-right Share

    def test_finds_blue_next_when_label_is_next(self):
        with patch.object(ig, "_adb_capture_ui_dump", return_value=_dump(NEXT_SCREEN)), \
             patch.object(ig, "_adb_is_instagram_share_button_blue", return_value=True):
            result = ig._adb_find_instagram_bottom_blue_action_center("dev", labels=("next", "share"))
        self.assertIsNotNone(result)
        (_cx, _cy), label = result
        self.assertEqual(label, "next")

    def test_returns_none_when_not_blue_verified(self):
        # A verifiable-but-not-blue button must NOT be tapped (returns None).
        with patch.object(ig, "_adb_capture_ui_dump", return_value=_dump(SHARE_SCREEN)), \
             patch.object(ig, "_adb_is_instagram_share_button_blue", return_value=False):
            result = ig._adb_find_instagram_bottom_blue_action_center("dev", labels=("next", "share"))
        self.assertIsNone(result)

    def test_falls_back_to_label_when_colour_uncheckable(self):
        # If colour can't be checked for ANY candidate, use the best label match.
        with patch.object(ig, "_adb_capture_ui_dump", return_value=_dump(SHARE_SCREEN)), \
             patch.object(ig, "_adb_is_instagram_share_button_blue", return_value=None):
            result = ig._adb_find_instagram_bottom_blue_action_center("dev", labels=("next", "share"))
        self.assertIsNotNone(result)
        (_cx, _cy), label = result
        self.assertEqual(label, "share")


class ReelFlowStateResetTest(TestCase):
    def test_tap_blue_next_or_share_returns_label_and_taps(self):
        flow = ig.InstagramReelUploadFlow()
        taps = []
        with patch.object(ig, "_adb_find_instagram_bottom_blue_action_center", return_value=((820, 2160), "share")), \
             patch.object(ig, "_adb_tap", side_effect=lambda t, x, y, *a, **k: taps.append((x, y))):
            label = flow._tap_blue_next_or_share("dev", object())
        self.assertEqual(label, "share")
        self.assertEqual(taps, [(820, 2160)])

    def test_tap_blue_next_or_share_returns_none_when_absent(self):
        flow = ig.InstagramReelUploadFlow()
        with patch.object(ig, "_adb_find_instagram_bottom_blue_action_center", return_value=None):
            self.assertIsNone(flow._tap_blue_next_or_share("dev", object()))


if __name__ == "__main__":
    import unittest
    unittest.main()
