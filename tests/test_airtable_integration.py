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


class ProfileTargetsByModelTest(TestCase):
    """Grouping the MLX profile inventory by model, for targets='profiles'."""

    ROWS = [
        {"id": "p1", "fields": {"Profile Name": "Jil 2", "MLX API ID": "222"}},
        {"id": "p2", "fields": {"Profile Name": "Jil 1", "MLX API ID": "111"}},
        {"id": "p3", "fields": {"Profile Name": "Katja 3", "MLX API ID": "333"}},
        # Excluded: the per-model link-in-bio account, not a posting target.
        {"id": "p4", "fields": {"Profile Name": "Jil I Link Account", "MLX API ID": "444"}},
        # Excluded: nothing can be launched without the 18-digit key.
        {"id": "p5", "fields": {"Profile Name": "Jil 9", "MLX API ID": ""}},
        # Excluded: parked by a person (flagged account, or an unused MLX
        # staging profile). Status is the only switch a profile-driven target
        # has -- there is no Accounts row to hold the health guards.
        {"id": "p6", "fields": {"Profile Name": "Jil 4", "MLX API ID": "666",
                                "Status": "Inactive"}},
        {"id": "p7", "fields": {"Profile Name": "Blank 1 (3)", "MLX API ID": "777",
                                "Status": "Inactive"}},
        # Kept: an explicitly Active row, and a row whose Status was never set.
        {"id": "p8", "fields": {"Profile Name": "Katja 1", "MLX API ID": "888",
                                "Status": "Active"}},
    ]

    def _targets(self, **kwargs):
        client = AirtableClient("tok", "app123", "Profiles")
        with patch.object(AirtableClient, "_list_table", return_value=self.ROWS):
            return client.profile_targets_by_model(**kwargs)

    def test_groups_by_first_word_of_the_profile_name(self):
        targets = self._targets()
        self.assertEqual(sorted(targets), ["jil", "katja"])
        # Sorted by name, so a run's fan-out order is stable.
        self.assertEqual([t["handle"] for t in targets["jil"]], ["Jil 1", "Jil 2"])
        self.assertEqual(targets["jil"][0], {"profile_id": "p2", "handle": "Jil 1",
                                             "profile_name": "Jil 1", "launch_id": "111",
                                             "slot": "Primary", "ig_handle": None})

    def test_link_profiles_and_keyless_profiles_are_dropped(self):
        handles = [t["handle"] for t in self._targets()["jil"]]
        self.assertNotIn("Jil I Link Account", handles)
        self.assertNotIn("Jil 9", handles)

    def test_link_profiles_can_be_opted_back_in(self):
        targets = self._targets(include_link_profiles=True)
        self.assertIn("Jil I Link Account", [t["handle"] for t in targets["jil"]])

    def test_inactive_profiles_are_dropped(self):
        targets = self._targets()
        self.assertNotIn("Jil 4", [t["handle"] for t in targets["jil"]])
        # A model whose every profile is parked disappears entirely, so nothing
        # downstream spoofs or queues for it.
        self.assertNotIn("blank", targets)

    def test_active_and_unset_status_are_both_kept(self):
        handles = [t["handle"] for t in self._targets()["katja"]]
        self.assertIn("Katja 1", handles)   # Status: Active
        self.assertIn("Katja 3", handles)   # Status never set


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


class ListTableMaxRecordsTest(TestCase):
    """A reachability probe must not paginate the whole table.

    `page_size` alone never bounds the work: the offset loop runs to the end of
    the table regardless, so a small page size costs *more* requests, not fewer.
    `doctor` learned this the slow way on 2026-08-05 -- its 6-table probe took
    minutes because `page_size=1` fetched one record per HTTP round trip.
    """

    def _page(self, payload):
        page = Mock()
        page.json.return_value = payload
        page.raise_for_status.return_value = None
        return page

    def test_max_records_stops_after_one_request(self):
        client = AirtableClient("tok", "app123", "Profiles")
        pages = [self._page({"records": [{"id": "rec1"}], "offset": "off1"}),
                 self._page({"records": [{"id": "rec2"}], "offset": "off2"})]
        with patch("adb_bot.clients.airtable.requests.get", side_effect=pages) as mock_get:
            rows = client._list_table("Table", page_size=1, max_records=1)

        self.assertEqual([r["id"] for r in rows], ["rec1"])
        self.assertEqual(mock_get.call_count, 1, "offset was followed despite max_records")
        self.assertEqual(mock_get.call_args.kwargs["params"]["maxRecords"], 1)

    def test_without_max_records_pagination_is_unchanged(self):
        client = AirtableClient("tok", "app123", "Profiles")
        pages = [self._page({"records": [{"id": "rec1"}], "offset": "off1"}),
                 self._page({"records": [{"id": "rec2"}]})]
        with patch("adb_bot.clients.airtable.requests.get", side_effect=pages) as mock_get:
            rows = client._list_table("Table")

        self.assertEqual([r["id"] for r in rows], ["rec1", "rec2"])
        self.assertEqual(mock_get.call_count, 2)
        self.assertNotIn("maxRecords", mock_get.call_args.kwargs["params"])


class DoctorAirtableProbeTest(TestCase):
    def test_probe_bounds_every_table_read(self):
        from adb_bot.automation import doctor
        seen = []

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def _list_table(self, table, **kwargs):
                seen.append((table, kwargs.get("max_records")))
                return []

        with patch("adb_bot.clients.airtable.AirtableClient", FakeClient):
            doctor.check_airtable("tok", "app123")

        self.assertTrue(seen, "probe read no tables at all")
        for table, cap in seen:
            self.assertEqual(cap, 1, f"{table} probe was unbounded")


class WarmupRunLogTest(TestCase):
    """The filter that decides which runs the warm-up is allowed to know about.

    This read used to name `warm_up_process` alone, and the client's plan ends
    with a scroll-only day that runs as `instagram_scroll`. Every day-4 row --
    155 attempts across 46 profiles -- was filtered out before the dashboard
    ever saw it, so the tab could neither show the failures nor ever credit a
    success. The filter is the whole bug, so these tests pin the formula itself
    rather than the rows that come back.
    """

    def _formula(self, client, *args):
        page = Mock()
        page.json.return_value = {"records": []}
        page.raise_for_status.return_value = None
        with patch("adb_bot.clients.airtable.requests.get", return_value=page) as mock_get:
            client.warmup_run_log(*args)
        return mock_get.call_args.kwargs["params"]["filterByFormula"]

    def test_default_asks_for_every_warm_up_flow(self):
        client = AirtableClient("tok", "app123", "Profiles")
        self.assertEqual(
            self._formula(client),
            "OR({Flow}='warm_up_process',{Flow}='instagram_scroll',"
            "{Flow}='update_profile_picture',{Flow}='update_bio_u2')",
        )

    def test_one_flow_is_a_bare_equality(self):
        # A one-armed OR() is valid Airtable but noise in the request log, and
        # the string is what a person compares against the base's own view.
        client = AirtableClient("tok", "app123", "Profiles")
        self.assertEqual(self._formula(client, "instagram_scroll"),
                         "{Flow}='instagram_scroll'")
        self.assertEqual(self._formula(client, ["instagram_scroll"]),
                         "{Flow}='instagram_scroll'")

    def test_blank_flow_names_are_dropped(self):
        client = AirtableClient("tok", "app123", "Profiles")
        self.assertEqual(self._formula(client, ["  warm_up_process ", "", "  "]),
                         "{Flow}='warm_up_process'")

    def test_apostrophe_raises_instead_of_corrupting_the_formula(self):
        # Unescaped interpolation: a stray quote does not narrow the result, it
        # 422s the listing, which `report.warmup_progress` reports as an error
        # and the dashboard draws as an empty warm-up tab.
        client = AirtableClient("tok", "app123", "Profiles")
        with self.assertRaises(ValueError):
            client.warmup_run_log("it's_a_flow")
        with self.assertRaises(ValueError):
            client.warmup_run_log(["warm_up_process", "it's_a_flow"])

    def test_no_flows_at_all_raises_rather_than_reading_the_whole_table(self):
        client = AirtableClient("tok", "app123", "Profiles")
        with self.assertRaises(ValueError):
            client.warmup_run_log([])

    def test_rows_come_back_newest_first(self):
        client = AirtableClient("tok", "app123", "Profiles")
        rows = [{"id": "old", "fields": {"Run At": "2026-08-01T10:00:00.000Z"}},
                {"id": "new", "fields": {"Run At": "2026-08-09T10:00:00.000Z"}}]
        with patch.object(AirtableClient, "_list_table", return_value=rows):
            self.assertEqual([r["id"] for r in client.warmup_run_log()], ["new", "old"])

    def test_flow_names_match_the_automation_definition(self):
        """The guard for the literals `clients` cannot import.

        `clients` is underneath `automation` and must not import it back, so the
        four flow names live twice. Nothing but this test stops a rename landing
        in `lifecycle` alone -- and the failure mode of that drift is silent:
        the Run Log read simply stops matching the rows the runner writes.
        """
        from adb_bot.automation import warmup_completion
        from adb_bot.clients import airtable as at

        self.assertEqual(at.WARMUP_RUN_FLOWS, warmup_completion.WARMUP_RUN_FLOWS)


class AccountsTableAbsentTest(TestCase):
    """The Accounts table was deleted from the production base on 2026-08-18.

    Airtable answers an unknown table name with 403, so this arrived looking
    like an auth failure and stopped every posting tick for seven hours with
    711 rows due. Posting is profile-driven, so the table's absence must cost
    the planner nothing.
    """

    @staticmethod
    def _http_error(status):
        import requests
        response = Mock()
        response.status_code = status
        return requests.HTTPError(f"{status} Client Error", response=response)

    def setUp(self):
        AirtableClient._accounts_absent_logged = False

    def test_absent_accounts_table_reads_as_empty(self):
        client = AirtableClient("tok", "app123", "Profiles")
        for status in (403, 404):
            with self.subTest(status=status):
                AirtableClient._accounts_absent_logged = False
                with patch.object(AirtableClient, "_list_table",
                                  side_effect=self._http_error(status)):
                    self.assertEqual(client.list_accounts(), [])
                    self.assertEqual(client.accounts_by_id(), {})

    def test_dead_token_still_raises(self):
        # 401 is a real credentials failure and must not be swallowed, or a
        # broken token would look like an ordinary schema change.
        import requests
        client = AirtableClient("tok", "app123", "Profiles")
        with patch.object(AirtableClient, "_list_table",
                          side_effect=self._http_error(401)):
            with self.assertRaises(requests.HTTPError):
                client.list_accounts()

    def test_absence_is_explained_once_per_process(self):
        client = AirtableClient("tok", "app123", "Profiles")
        with patch.object(AirtableClient, "_list_table",
                          side_effect=self._http_error(403)):
            with patch("builtins.print") as printed:
                client.list_accounts()
                client.list_accounts()
                client.list_accounts()
        self.assertEqual(printed.call_count, 1)

    def test_queue_loops_account_read_is_tolerant_too(self):
        # active_accounts_by_model is a *second*, separate read of the same
        # table, and it is what the queue loop uses to create posting rows.
        # Patching only list_accounts left the queue loop still dying on the
        # 403 and quietly creating nothing, which would have drained the
        # backlog and then stopped the fleet again a day later.
        client = AirtableClient("tok", "app123", "Profiles")
        with patch.object(AirtableClient, "models_by_recid", return_value={}):
            with patch.object(AirtableClient, "_list_table",
                              side_effect=self._http_error(403)):
                self.assertEqual(client.active_accounts_by_model(), {})
