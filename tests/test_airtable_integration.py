from unittest import TestCase
from unittest.mock import Mock, patch

from adb_bot.clients.airtable import AirtableClient
from adb_bot.automation.airtable_runner import _parse_record, _status_to_airtable_fields


class AirtableClientTest(TestCase):
    def test_list_ready_records_filters_and_parses(self):
        client = AirtableClient("tok", "app123", "Profiles")

        page = Mock()
        page.json.return_value = {
            "records": [
                {"id": "rec1", "fields": {"Multilogin Profile ID": "111", "Flow": "update_bio"}},
                {"id": "rec2", "fields": {"Multilogin Profile ID": "222", "Flow": "warm_up_process"}},
            ]
        }
        page.raise_for_status.return_value = None

        with patch("adb_bot.clients.airtable.requests.get", return_value=page) as mock_get:
            records = client.list_ready_records()

        self.assertEqual([r["id"] for r in records], ["rec1", "rec2"])
        args, kwargs = mock_get.call_args
        self.assertIn("app123", args[0])
        self.assertIn("Profiles", args[0])
        self.assertEqual(kwargs["params"]["filterByFormula"], "{Status}='Ready'")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")

    def test_list_ready_records_follows_pagination(self):
        client = AirtableClient("tok", "app123", "Profiles")

        first = Mock()
        first.json.return_value = {"records": [{"id": "rec1", "fields": {}}], "offset": "off1"}
        first.raise_for_status.return_value = None
        second = Mock()
        second.json.return_value = {"records": [{"id": "rec2", "fields": {}}]}
        second.raise_for_status.return_value = None

        with patch("adb_bot.clients.airtable.requests.get", side_effect=[first, second]) as mock_get:
            records = client.list_ready_records()

        self.assertEqual([r["id"] for r in records], ["rec1", "rec2"])
        self.assertEqual(mock_get.call_count, 2)

    def test_update_record_patches_fields(self):
        client = AirtableClient("tok", "app123", "Profiles")
        resp = Mock()
        resp.raise_for_status.return_value = None

        with patch("adb_bot.clients.airtable.requests.patch", return_value=resp) as mock_patch:
            ok = client.update_record("rec1", {"Status": "Done"})

        self.assertTrue(ok)
        args, kwargs = mock_patch.call_args
        self.assertTrue(args[0].endswith("/rec1"))
        self.assertEqual(kwargs["json"], {"fields": {"Status": "Done"}})

    def test_update_record_never_raises(self):
        client = AirtableClient("tok", "app123", "Profiles")
        with patch("adb_bot.clients.airtable.requests.patch", side_effect=RuntimeError("boom")):
            self.assertFalse(client.update_record("rec1", {"Status": "Done"}))


class AirtableRunnerHelpersTest(TestCase):
    def test_status_mapping(self):
        self.assertEqual(_status_to_airtable_fields("done")["Status"], "Done")
        self.assertEqual(_status_to_airtable_fields("done")["Last Result"], "success")
        self.assertEqual(_status_to_airtable_fields("already_had_bio")["Status"], "Skipped")
        self.assertEqual(_status_to_airtable_fields("already_had_bio")["Last Result"], "already_had_bio")
        self.assertEqual(_status_to_airtable_fields("adb_connect_failed")["Last Result"], "failed_to_connect_adb")
        self.assertEqual(_status_to_airtable_fields("failed")["Status"], "Failed")
        # Non-terminal statuses must not produce a write.
        self.assertEqual(_status_to_airtable_fields("running"), {})
        self.assertEqual(_status_to_airtable_fields("connecting"), {})

    def test_parse_record_valid(self):
        spec = _parse_record({
            "id": "rec1",
            "fields": {"Multilogin Profile ID": "111", "Flow": "update_bio", "Bio": "hello"},
        })
        self.assertEqual(spec["profile_id"], "111")
        self.assertEqual(spec["flow"], "update_bio")
        self.assertEqual(spec["bio"], "hello")
        self.assertIsNone(spec["caption"])

    def test_parse_record_missing_fields(self):
        self.assertIsNone(_parse_record({"id": "r", "fields": {"Flow": "update_bio"}}))
        self.assertIsNone(_parse_record({"id": "r", "fields": {"Multilogin Profile ID": "111"}}))
