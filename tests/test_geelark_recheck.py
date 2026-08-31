import json
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_bot.automation import post_ledger
from adb_bot.automation import geelark_recheck


class RunGeelarkRecheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger_path = Path(self._tmp.name) / "posted_reels.jsonl"
        self._RealPostLedger = post_ledger.PostLedger

    def _ledger(self):
        return self._RealPostLedger(path=self.ledger_path)

    def _seed(self, profile_id, media_hash, age_seconds, baseline_count=5, baseline_exact=True):
        record = post_ledger.ShareRecord(
            profile_id=profile_id, media_hash=media_hash, status=post_ledger.STATUS_SHARED,
            shared_at=time.time() - age_seconds, baseline_count=baseline_count,
            baseline_exact=baseline_exact)
        ledger = self._ledger()
        # Use the store's own append path so on-disk format matches production.
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record)) + "\n")
        return record

    def test_a_record_past_the_age_ceiling_is_abandoned_without_launching_anything(self):
        self._seed("ph-old", "deadbeef" * 8, age_seconds=25 * 3600)
        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch") as launch:
            with patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
                results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)
        client_cls.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "abandoned")

    def test_a_record_in_window_gets_launched_and_resolved_as_posted(self):
        self._seed("ph-1", "cafebabe" * 8, age_seconds=20 * 60, baseline_count=5)
        fake_session = MagicMock()
        fake_target = "127.0.0.1:5555"
        fake_current = MagicMock(value=6, exact=True)

        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch",
                  return_value=(fake_session, fake_target)) as launch, \
             patch("adb_bot.automation.geelark_recheck.stop_session"), \
             patch("adb_bot.automation.geelark_recheck.u2") as u2_mod, \
             patch("adb_bot.automation.geelark_recheck.InstagramReelUploadU2Flow") as flow_cls, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            client_cls.return_value.list_phones.return_value = [{"id": "ph-1"}]
            u2_mod.connect.return_value = MagicMock()
            flow = flow_cls.return_value
            flow._open_profile_tab_u2.return_value = True
            flow._read_post_count_u2.return_value = fake_current

            results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)

        launch.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "posted")

        resolved = self._ledger().load()
        self.assertEqual(resolved["ph-1:" + "cafebabe" * 8].status,
                         post_ledger.STATUS_CONFIRMED)


if __name__ == "__main__":
    unittest.main()
