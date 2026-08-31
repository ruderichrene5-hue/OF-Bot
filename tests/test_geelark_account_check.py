import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_bot.automation import geelark_account_check as check
from adb_bot.automation import post_ledger


class RunGeelarkAccountCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "geelark_account_check_log.jsonl"
        self.ledger_path = Path(self._tmp.name) / "posted_reels.jsonl"
        self._app_data_patcher = patch("adb_bot.automation.geelark_account_check.get_app_data_dir",
                                       return_value=Path(self._tmp.name))
        self._app_data_patcher.start()
        self.addCleanup(self._app_data_patcher.stop)
        self._RealPostLedger = post_ledger.PostLedger

    def _ledger(self):
        return self._RealPostLedger(path=self.ledger_path)

    def test_a_never_touched_phone_gets_checked_and_found_healthy(self):
        with patch("adb_bot.automation.geelark_account_check.phones_by_tag",
                  return_value=[{"id": "ph-1", "serialName": "Lea 1 Geelark"}]) as by_tag, \
             patch("adb_bot.automation.geelark_account_check._launch",
                  return_value=(MagicMock(), "127.0.0.1:5555")) as launch, \
             patch("adb_bot.automation.geelark_account_check._check_for_challenge_and_abort",
                  return_value=None) as check_fn, \
             patch("adb_bot.automation.geelark_account_check.stop_session"), \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            results = check.run_geelark_account_check(adb_client=object(), logger=None)

        by_tag.assert_called_once()
        launch.assert_called_once()
        check_fn.assert_called_once()
        self.assertEqual(results, [{"phone_id": "ph-1", "name": "Lea 1 Geelark", "result": "healthy"}])
        self.assertTrue(self.log_path.exists())

    def test_a_phone_found_unhealthy_reports_the_new_tag(self):
        with patch("adb_bot.automation.geelark_account_check.phones_by_tag",
                  return_value=[{"id": "ph-2", "serialName": "Lea 2 Geelark"}]), \
             patch("adb_bot.automation.geelark_account_check._launch",
                  return_value=(MagicMock(), "127.0.0.1:5555")), \
             patch("adb_bot.automation.geelark_account_check._check_for_challenge_and_abort",
                  return_value="human verification"), \
             patch("adb_bot.automation.geelark_account_check.stop_session"), \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            results = check.run_geelark_account_check(adb_client=object(), logger=None)

        self.assertEqual(results[0]["result"], "human verification")

    def test_a_phone_already_posted_to_today_is_skipped(self):
        """The posting cycle already read this phone's screen once before
        doing anything else -- checking it again here is a wasted launch
        for the same answer."""
        record = post_ledger.ShareRecord(
            profile_id="ph-3", media_hash="a" * 64, status=post_ledger.STATUS_SHARED,
            shared_at=time.time(), platform="geelark")
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            from dataclasses import asdict
            handle.write(json.dumps(asdict(record)) + "\n")

        with patch("adb_bot.automation.geelark_account_check.phones_by_tag",
                  return_value=[{"id": "ph-3", "serialName": "Lea 3 Geelark"}]), \
             patch("adb_bot.automation.geelark_account_check._launch") as launch, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            results = check.run_geelark_account_check(adb_client=object(), logger=None)

        launch.assert_not_called()
        self.assertEqual(results, [])

    def test_a_phone_already_checked_by_this_sweep_today_is_not_checked_twice(self):
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), "phone_id": "ph-4",
                                    "name": "Lea 4 Geelark", "result": "healthy"}) + "\n")

        with patch("adb_bot.automation.geelark_account_check.phones_by_tag",
                  return_value=[{"id": "ph-4", "serialName": "Lea 4 Geelark"}]), \
             patch("adb_bot.automation.geelark_account_check._launch") as launch, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            results = check.run_geelark_account_check(adb_client=object(), logger=None)

        launch.assert_not_called()
        self.assertEqual(results, [])

    def test_an_mlx_ledger_entry_does_not_exempt_a_geelark_phone_from_checking(self):
        """post_ledger.py is shared with MLX -- an "mlx"-tagged entry for
        this id must not be read as "already checked today" here."""
        record = post_ledger.ShareRecord(
            profile_id="ph-5", media_hash="b" * 64, status=post_ledger.STATUS_SHARED,
            shared_at=time.time(), platform="mlx")
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            from dataclasses import asdict
            handle.write(json.dumps(asdict(record)) + "\n")

        with patch("adb_bot.automation.geelark_account_check.phones_by_tag",
                  return_value=[{"id": "ph-5", "serialName": "Lea 5 Geelark"}]), \
             patch("adb_bot.automation.geelark_account_check._launch",
                  return_value=(MagicMock(), "127.0.0.1:5555")) as launch, \
             patch("adb_bot.automation.geelark_account_check._check_for_challenge_and_abort",
                  return_value=None), \
             patch("adb_bot.automation.geelark_account_check.stop_session"), \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            results = check.run_geelark_account_check(adb_client=object(), logger=None)

        launch.assert_called_once()
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
