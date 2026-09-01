import unittest
from unittest.mock import patch

from adb_bot.automation.flows import instagram


class AdbPushRetryTest(unittest.TestCase):
    """Added 2026-09-01: a real push failure that day
    (file_sync_client.cpp:473 protocol fault: failed to read stat response:
    Success) was a pure network hiccup on the tunnel to a remote cloud
    phone -- a retry, not a fresh composer attempt, is the appropriate fix."""

    def test_succeeds_on_the_first_attempt_without_retrying(self):
        with patch.object(instagram, "_adb_push_media_to_device_once",
                         return_value=True) as once, \
             patch("time.sleep") as sleep:
            ok = instagram._adb_push_media_to_device("target", "local.mp4", "remote.mp4")
        self.assertTrue(ok)
        once.assert_called_once()
        sleep.assert_not_called()

    def test_retries_after_a_failure_and_succeeds_on_the_second_attempt(self):
        with patch.object(instagram, "_adb_push_media_to_device_once",
                         side_effect=[False, True]) as once, \
             patch("time.sleep") as sleep:
            ok = instagram._adb_push_media_to_device("target", "local.mp4", "remote.mp4",
                                                      retry_delay_seconds=5.0)
        self.assertTrue(ok)
        self.assertEqual(once.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_gives_up_after_max_attempts(self):
        with patch.object(instagram, "_adb_push_media_to_device_once",
                         return_value=False) as once, \
             patch("time.sleep"):
            ok = instagram._adb_push_media_to_device("target", "local.mp4", "remote.mp4",
                                                      max_attempts=3)
        self.assertFalse(ok)
        self.assertEqual(once.call_count, 3)


if __name__ == "__main__":
    unittest.main()
