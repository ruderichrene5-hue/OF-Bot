"""_open_reel_composer_u2 gained one retry (2026-08-31) after live evidence:
a batch's second post found Instagram mid-transition right after the
Home-tab recovery tap (a flat 1.5s sleep, no confirmation), and the
composer never appeared. These pin the retry itself, independent of the
real device-interaction details (dump scanning, taps, selectors) which are
mocked out here.
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

from adb_bot.automation.flows.instagram_reel import InstagramReelUploadU2Flow


class OpenReelComposerRetryTest(TestCase):
    def setUp(self):
        self.flow = InstagramReelUploadU2Flow()
        self.flow._recover_to_instagram_u2 = MagicMock()
        self.flow._find_create_via_dump_u2 = MagicMock(return_value=(100, 200))
        self.flow._dismiss_popups_u2 = MagicMock()
        self.d = MagicMock()
        self.d.click = MagicMock()

    def test_succeeds_on_the_first_attempt_without_any_recovery(self):
        self.flow._first_present = MagicMock(return_value=object())
        with patch("adb_bot.automation.flows.instagram_reel.waits.settle"), \
             patch("adb_bot.automation.flows.instagram_reel._u2_find", return_value=None), \
             patch("adb_bot.automation.flows.instagram_reel.instagram_module."
                  "_ensure_instagram_home_feed_u2") as recover_feed:
            ok = self.flow._open_reel_composer_u2(self.d, "1.2.3.4:5555", lambda *a, **k: None)
        self.assertTrue(ok)
        self.flow._dismiss_popups_u2.assert_not_called()
        recover_feed.assert_not_called()
        # Only the one attempt's worth of taps -- no retry loop ran.
        self.assertEqual(self.flow._find_create_via_dump_u2.call_count, 1)

    def test_a_failed_first_attempt_gets_exactly_one_recovery_and_retry(self):
        # None (gallery never appears) on attempt 1, an object on attempt 2.
        self.flow._first_present = MagicMock(side_effect=[None, object()])
        with patch("adb_bot.automation.flows.instagram_reel.waits.settle"), \
             patch("adb_bot.automation.flows.instagram_reel._u2_find", return_value=None), \
             patch("adb_bot.automation.flows.instagram_reel.instagram_module."
                  "_ensure_instagram_home_feed_u2") as recover_feed:
            ok = self.flow._open_reel_composer_u2(self.d, "1.2.3.4:5555", lambda *a, **k: None)
        self.assertTrue(ok)
        self.flow._dismiss_popups_u2.assert_called_once()
        recover_feed.assert_called_once()
        self.assertEqual(self.flow._find_create_via_dump_u2.call_count, 2)

    def test_two_failed_attempts_gives_up_and_returns_false(self):
        self.flow._first_present = MagicMock(return_value=None)
        with patch("adb_bot.automation.flows.instagram_reel.waits.settle"), \
             patch("adb_bot.automation.flows.instagram_reel._u2_find", return_value=None), \
             patch("adb_bot.automation.flows.instagram_reel.instagram_module."
                  "_ensure_instagram_home_feed_u2") as recover_feed:
            ok = self.flow._open_reel_composer_u2(self.d, "1.2.3.4:5555", lambda *a, **k: None)
        self.assertFalse(ok)
        self.flow._dismiss_popups_u2.assert_called_once()
        recover_feed.assert_called_once()
        self.assertEqual(self.flow._find_create_via_dump_u2.call_count, 2)
