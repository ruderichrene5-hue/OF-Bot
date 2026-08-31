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

    def _seed(self, profile_id, media_hash, age_seconds, baseline_count=5, baseline_exact=True,
             platform="geelark"):
        record = post_ledger.ShareRecord(
            profile_id=profile_id, media_hash=media_hash, status=post_ledger.STATUS_SHARED,
            shared_at=time.time() - age_seconds, baseline_count=baseline_count,
            baseline_exact=baseline_exact, platform=platform)
        ledger = self._ledger()
        # Use the store's own append path so on-disk format matches production.
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record)) + "\n")
        return record

    def test_a_record_past_the_age_ceiling_is_abandoned_without_launching_anything(self):
        """Explicitly platform="geelark" here -- it must not need the phone
        list to know that, and must still never launch anything."""
        self._seed("ph-old", "deadbeef" * 8, age_seconds=25 * 3600)
        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch") as launch:
            client_cls.return_value.list_phones.return_value = []
            with patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
                results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)
        launch.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "abandoned")

    def test_a_record_in_window_gets_launched_and_resolved_as_posted(self):
        self._seed("ph-1", "cafebabe" * 8, age_seconds=20 * 60, baseline_count=5)
        fake_session = MagicMock()
        fake_target = "127.0.0.1:5555"
        fake_device = MagicMock()
        fake_current = MagicMock(value=6, exact=True)

        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch",
                  return_value=(fake_session, fake_target)) as launch, \
             patch("adb_bot.automation.geelark_recheck.stop_session"), \
             patch("adb_bot.automation.geelark_recheck.u2"), \
             patch("adb_bot.automation.geelark_recheck._open_instagram_u2",
                  return_value=fake_device) as open_ig, \
             patch("adb_bot.automation.geelark_recheck.waits.settle"), \
             patch("adb_bot.automation.geelark_recheck.InstagramReelUploadU2Flow") as flow_cls, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            client_cls.return_value.list_phones.return_value = [{"id": "ph-1"}]
            flow = flow_cls.return_value
            flow._open_profile_tab_u2.return_value = True
            flow._read_post_count_u2.return_value = fake_current

            results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)

        launch.assert_called_once()
        open_ig.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "posted")

        resolved = self._ledger().load()
        self.assertEqual(resolved["ph-1:" + "cafebabe" * 8].status,
                         post_ledger.STATUS_CONFIRMED)

    def test_instagram_never_coming_up_is_reported_without_reading_anything(self):
        self._seed("ph-2", "12345678" * 8, age_seconds=20 * 60, baseline_count=5)
        fake_session = MagicMock()
        fake_target = "127.0.0.1:5555"

        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch",
                  return_value=(fake_session, fake_target)), \
             patch("adb_bot.automation.geelark_recheck.stop_session"), \
             patch("adb_bot.automation.geelark_recheck.u2"), \
             patch("adb_bot.automation.geelark_recheck._open_instagram_u2", return_value=None), \
             patch("adb_bot.automation.geelark_recheck.InstagramReelUploadU2Flow") as flow_cls, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            client_cls.return_value.list_phones.return_value = [{"id": "ph-2"}]
            results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)

        flow_cls.return_value._open_profile_tab_u2.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["outcome"], "could_not_open_instagram")

    def test_a_two_account_phone_reads_each_handle_separately(self):
        """A count read only means anything for the account it was taken
        for -- reading once for the whole phone would score records for
        the OTHER account against the wrong count entirely."""
        self._seed("ph-3", "aaaaaaaa" * 8, age_seconds=20 * 60, baseline_count=5)
        record_a = self._ledger().load()["ph-3:" + "aaaaaaaa" * 8]
        record_a.target_handle = "handle_a"
        with self._ledger().path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record_a)) + "\n")

        record_b = post_ledger.ShareRecord(
            profile_id="ph-3", media_hash="bbbbbbbb" * 8, status=post_ledger.STATUS_SHARED,
            shared_at=time.time() - 20 * 60, baseline_count=9, baseline_exact=True,
            target_handle="handle_b")
        with self._ledger().path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record_b)) + "\n")

        fake_session = MagicMock()
        fake_target = "127.0.0.1:5555"
        fake_device = MagicMock()

        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch",
                  return_value=(fake_session, fake_target)), \
             patch("adb_bot.automation.geelark_recheck.stop_session"), \
             patch("adb_bot.automation.geelark_recheck.u2"), \
             patch("adb_bot.automation.geelark_recheck._open_instagram_u2",
                  return_value=fake_device), \
             patch("adb_bot.automation.geelark_recheck.waits.settle"), \
             patch("adb_bot.automation.geelark_recheck.InstagramReelUploadU2Flow") as flow_cls, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            client_cls.return_value.list_phones.return_value = [{"id": "ph-3"}]
            flow = flow_cls.return_value
            flow._ensure_account_state_u2.return_value = (True, "ok")
            flow._open_profile_tab_u2.return_value = True
            flow._read_post_count_u2.side_effect = [
                MagicMock(value=6, exact=True), MagicMock(value=9, exact=True)]

            results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)

        self.assertEqual(flow._ensure_account_state_u2.call_count, 2)
        switched_to = {c.args[2] for c in flow._ensure_account_state_u2.call_args_list}
        self.assertEqual(switched_to, {"handle_a", "handle_b"})
        outcomes = {r["media_hash"]: r["outcome"] for r in results}
        self.assertEqual(outcomes["aaaaaaaa" * 8], "posted")
        self.assertEqual(outcomes["bbbbbbbb" * 8], "failed")

    def test_an_mlx_tagged_record_is_never_touched_even_if_its_id_matches_a_geelark_phone(self):
        """post_ledger.py is shared with MLX (see ShareRecord.platform) --
        an explicit "mlx" tag must win over the phone-list fallback, not
        just be one more thing to guess from. Found live 2026-08-31: 495 of
        750 "GeeLark" shares that day were actually MLX."""
        self._seed("ph-shared-id", "eeeeeeee" * 8, age_seconds=20 * 60, platform="mlx")
        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch") as launch, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            # Even if the id happens to also be a real, current Geelark phone.
            client_cls.return_value.list_phones.return_value = [{"id": "ph-shared-id"}]
            results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)
        launch.assert_not_called()
        self.assertEqual(results, [])

    def test_an_untagged_record_not_in_the_current_phone_list_is_skipped(self):
        """Records written before the platform field existed have no tag at
        all -- the phone-list check is what still tells an old MLX record
        apart from an old GeeLark one for those."""
        self._seed("ph-untagged", "ffffffff" * 8, age_seconds=20 * 60, platform="")
        with patch("adb_bot.automation.geelark_recheck.GeelarkPhoneClient") as client_cls, \
             patch("adb_bot.automation.geelark_recheck._launch") as launch, \
             patch.object(post_ledger, "PostLedger", lambda path=None: self._ledger()):
            client_cls.return_value.list_phones.return_value = []  # not a Geelark phone
            results = geelark_recheck.run_geelark_recheck(adb_client=object(), logger=None)
        launch.assert_not_called()
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
