from unittest import TestCase

from adb_bot.clients import airtable as at
from adb_bot.automation import mlx_sync
from adb_bot.automation.mlx_sync import (
    NormalizedProfile,
    apply_sync,
    build_device_fields,
    build_profile_fields,
    build_proxy_fields,
    normalize_mlx_item,
    plan_sync,
)


# folder_id -> name, mirroring the MLX folders API the CLI passes into plan_sync.
FOLDERS = {"fld-nikki": "NIkki", "fld-luisa": "Luisa", "fld-default": "Default folder"}


def mlx_item(**overrides) -> dict:
    """A representative /mobile_profiles/phone/list item; override per test."""
    item = {
        "id": "624354174112432228",
        "serial_no": "158698",
        "serial_name": "Nikki 1",
        "status": 2,
        "created_at": "2026-06-01T10:00:00Z",
        "folder_id": "fld-nikki",
        # MLX's per-item group.name is the workspace GUID, not the model.
        "group": {"id": "g1", "name": "f03f25bc-eb13-4ec1-9cd6-80f14b3f9255"},
        "equipment_info": {
            "device_brand": "Samsung",
            "device_model": "Galaxy S21",
            "os_version": "Android 13",
            "country_name": "Germany",
            "time_zone": "Europe/Berlin",
            "phone_number": "+491234567",
        },
        "proxy": {"server": "1.2.3.4", "port": "8000", "type": "http"},
    }
    item.update(overrides)
    return item


def existing_row(**overrides) -> dict:
    """One `profiles_by_serial()` entry, already matching `mlx_item()`.

    Defaults to a fully-reconciled row so a test that cares about one field can
    override that field alone; anything left at the default must not produce a
    write.
    """
    row = {
        "record_id": "recX",
        "name": "Nikki 1",
        "api_id": "624354174112432228",
        "time_zone": "Europe/Berlin",
        "folder": "NIkki",
        "tags": (),
        "duplicate": False,
    }
    row.update(overrides)
    return row


class NormalizeTest(TestCase):
    def test_full_item(self):
        p = normalize_mlx_item(mlx_item(), FOLDERS)
        self.assertEqual(p.serial_no, "158698")
        self.assertEqual(p.api_id, "624354174112432228")
        self.assertEqual(p.name, "Nikki 1")
        self.assertTrue(p.status_active)
        self.assertEqual(p.model_name, "NIkki")  # folder name, verbatim casing
        self.assertEqual(p.time_zone, "Europe/Berlin")
        self.assertEqual(p.phone_model_os, "Samsung Galaxy S21 / Android 13")
        self.assertEqual(p.sim_number, "+491234567")
        self.assertEqual(p.proxy_endpoint, "1.2.3.4:8000")
        self.assertEqual(p.proxy_location, "Germany")

    def test_model_comes_from_folder_not_group(self):
        # group.name is the workspace GUID; it must never become the model.
        p = normalize_mlx_item(mlx_item(folder_id="fld-luisa"), FOLDERS)
        self.assertEqual(p.model_name, "Luisa")

    def test_missing_serial_or_id_is_unnormalizable(self):
        self.assertIsNone(normalize_mlx_item(mlx_item(serial_no=None), FOLDERS))
        self.assertIsNone(normalize_mlx_item(mlx_item(id=""), FOLDERS))

    def test_status_non_2_is_inactive(self):
        self.assertFalse(normalize_mlx_item(mlx_item(status=0), FOLDERS).status_active)

    def test_default_folder_has_no_model(self):
        p = normalize_mlx_item(mlx_item(folder_id="fld-default", serial_name="Blank 1 (6)"), FOLDERS)
        self.assertIsNone(p.model_name)

    def test_unknown_folder_id_has_no_model(self):
        p = normalize_mlx_item(mlx_item(folder_id="fld-missing"), FOLDERS)
        self.assertIsNone(p.model_name)

    def test_model_name_falls_back_to_serial_name_without_folder_map(self):
        # No folder map supplied -> derive from serial_name, stripping the suffix.
        self.assertEqual(normalize_mlx_item(mlx_item(serial_name="Luisa Link")).model_name, "Luisa")
        self.assertEqual(normalize_mlx_item(mlx_item(serial_name="Jasmin 10")).model_name, "Jasmin")
        self.assertEqual(normalize_mlx_item(mlx_item(serial_name="Blank 1 (6)")).model_name, "Blank")

    def test_partial_equipment_and_proxy(self):
        p = normalize_mlx_item(
            mlx_item(
                equipment_info={"device_brand": "Google", "os_version": "Android 14"},
                proxy={"server": "10.0.0.1"},  # no port
            ),
            FOLDERS,
        )
        self.assertEqual(p.phone_model_os, "Google / Android 14")
        self.assertEqual(p.proxy_endpoint, "10.0.0.1")
        self.assertIsNone(p.proxy_location)


class PlanSyncTest(TestCase):
    def test_new_profile_is_created(self):
        plan = plan_sync([mlx_item()], existing_by_serial={})
        self.assertEqual(len(plan.to_create), 1)
        self.assertEqual(plan.to_create[0].profile.serial_no, "158698")
        self.assertFalse(plan.to_update)
        self.assertFalse(plan.unchanged)

    def test_existing_missing_api_id_is_backfilled(self):
        existing = {"158698": existing_row(api_id=None)}
        plan = plan_sync([mlx_item()], existing, FOLDERS)
        self.assertEqual(len(plan.to_update), 1)
        upd = plan.to_update[0]
        self.assertEqual(upd.record_id, "recX")
        self.assertEqual(upd.updates, {at.F_PROF_MLX_API_ID: "624354174112432228"})

    def test_existing_missing_timezone_is_backfilled(self):
        existing = {"158698": existing_row(time_zone=None)}
        plan = plan_sync([mlx_item()], existing, FOLDERS)
        self.assertEqual(plan.to_update[0].updates, {at.F_PROF_TIME_ZONE: "Europe/Berlin"})

    def test_populated_api_id_and_timezone_are_never_overwritten(self):
        # Backfill, not reconcile: a value somebody corrected by hand stands,
        # even when MLX disagrees. Only name/folder/tags are MLX's to win.
        existing = {"158698": existing_row(api_id="hand-fixed", time_zone="Europe/Lisbon")}
        plan = plan_sync([mlx_item()], existing, FOLDERS)
        self.assertFalse(plan.to_update)

    def test_fully_synced_is_unchanged(self):
        plan = plan_sync([mlx_item()], {"158698": existing_row()}, FOLDERS)
        self.assertFalse(plan.to_create)
        self.assertFalse(plan.to_update)
        self.assertEqual(len(plan.unchanged), 1)
        self.assertEqual(plan.unchanged[0].reason, "already in sync")

    def test_unnormalizable_item_is_skipped(self):
        plan = plan_sync([mlx_item(serial_no=None, serial_name="Broken")], existing_by_serial={})
        self.assertFalse(plan.to_create)
        self.assertEqual(plan.skipped, [("Broken", "missing serial_no or 18-digit id")])

    def test_skip_staging_excludes_modelless_new_profiles(self):
        # "Default folder" profiles resolve to no model -> skipped when asked.
        staging = mlx_item(folder_id="fld-default", serial_name="Blank 1 (6)")
        plan = plan_sync([staging], existing_by_serial={}, folder_names=FOLDERS, skip_staging=True)
        self.assertEqual(plan.to_create, [])
        self.assertIn("staging", plan.skipped[0][1])

    def test_skip_staging_keeps_real_model_profiles(self):
        plan = plan_sync([mlx_item()], existing_by_serial={}, folder_names=FOLDERS, skip_staging=True)
        self.assertEqual(len(plan.to_create), 1)

    def test_staging_synced_by_default(self):
        staging = mlx_item(folder_id="fld-default", serial_name="Blank 1 (6)")
        plan = plan_sync([staging], existing_by_serial={}, folder_names=FOLDERS)
        self.assertEqual(len(plan.to_create), 1)

    def test_skip_staging_still_backfills_existing_rows(self):
        # An already-synced staging profile keeps getting its launch key filled in.
        staging = mlx_item(folder_id="fld-default", serial_name="Blank 1 (6)")
        existing = {"158698": existing_row(api_id=None, name="Blank 1 (6)", folder="Default folder")}
        plan = plan_sync([staging], existing, folder_names=FOLDERS, skip_staging=True)
        self.assertEqual(len(plan.to_update), 1)
        self.assertEqual(plan.to_update[0].updates, {at.F_PROF_MLX_API_ID: "624354174112432228"})

    def test_duplicate_serial_in_response_is_skipped_once(self):
        plan = plan_sync([mlx_item(), mlx_item(id="999")], existing_by_serial={})
        self.assertEqual(len(plan.to_create), 1)
        self.assertEqual(len(plan.skipped), 1)


class ReconcileTest(TestCase):
    """MLX wins on name, folder and tags -- and the cases where it must not."""

    def _updates(self, item, row, **kwargs):
        plan = plan_sync([item], {"158698": row}, FOLDERS, **kwargs)
        return plan.to_update[0].updates if plan.to_update else {}

    def test_rename_follows_multilogin(self):
        updates = self._updates(mlx_item(serial_name="Nikki 12"), existing_row())
        self.assertEqual(updates, {at.F_PROF_NAME: "Nikki 12"})

    def test_blank_style_name_is_replaced_by_the_real_one(self):
        # The 63-phone case: Airtable stuck on "Blank (24)" while MLX moved on.
        updates = self._updates(mlx_item(), existing_row(name="Blank (24)"))
        self.assertEqual(updates, {at.F_PROF_NAME: "Nikki 1"})

    def test_folder_move_follows_multilogin(self):
        updates = self._updates(mlx_item(folder_id="fld-luisa"), existing_row())
        self.assertEqual(updates, {at.F_PROF_MLX_FOLDER: "Luisa"})

    def test_staging_folder_is_recorded_even_though_it_is_no_model(self):
        # "which folder is it in" stays answerable for a phone in no model's
        # folder -- model resolution is a separate question.
        updates = self._updates(mlx_item(folder_id="fld-default"), existing_row())
        self.assertEqual(updates, {at.F_PROF_MLX_FOLDER: "Default folder"})

    def test_profile_in_no_folder_clears_the_field(self):
        updates = self._updates(mlx_item(folder_id="fld-gone"), existing_row())
        self.assertEqual(updates, {at.F_PROF_MLX_FOLDER: ""})

    def test_tags_are_mirrored_including_removals(self):
        item = mlx_item(tags=["Active / Posting", "Second Account"])
        updates = self._updates(item, existing_row(tags=("Created", "Issue")))
        self.assertEqual(updates, {at.F_PROF_MLX_TAGS: ["Active / Posting", "Second Account"]})

    def test_untagging_in_mlx_clears_the_field(self):
        updates = self._updates(mlx_item(), existing_row(tags=("Issue",)))
        self.assertEqual(updates, {at.F_PROF_MLX_TAGS: []})

    def test_same_tags_in_a_different_order_are_not_a_change(self):
        item = mlx_item(tags=["Issue", "Created"])
        self.assertEqual(self._updates(item, existing_row(tags=("Created", "Issue"))), {})

    def test_status_is_never_synced(self):
        # MLX status=2 means "enabled"; Airtable Status is the human park
        # switch. Neither direction of disagreement may produce a write.
        for status in (2, 0):
            updates = self._updates(mlx_item(status=status), existing_row())
            self.assertNotIn(at.F_PROF_STATUS, updates)

    def test_name_duplicated_in_mlx_is_not_propagated(self):
        # Three MLX profiles called "Jasmin 11" -> renaming would collide.
        items = [mlx_item(serial_name="Jasmin 11"),
                 mlx_item(serial_no="158699", id="2", serial_name="Jasmin 11")]
        plan = plan_sync(items, {"158698": existing_row(name="Blank (19)")}, FOLDERS)
        self.assertFalse(plan.to_update)
        self.assertIn("several profiles named", plan.unchanged[0].reason)

    def test_serial_claimed_by_two_airtable_rows_is_not_renamed(self):
        row = existing_row(name="Katja Link", duplicate=True)
        plan = plan_sync([mlx_item(serial_name="Katja 5")], {"158698": row}, FOLDERS)
        self.assertFalse(plan.to_update)
        self.assertIn("several Airtable rows", plan.unchanged[0].reason)

    def test_a_guarded_rename_still_lets_folder_and_tags_through(self):
        # The rename is the only unsafe part; the row should not be frozen.
        items = [mlx_item(serial_name="Jasmin 11"),
                 mlx_item(serial_no="158699", id="2", serial_name="Jasmin 11")]
        plan = plan_sync(items, {"158698": existing_row(name="Blank (19)", folder="Katja")},
                         FOLDERS)
        self.assertEqual(plan.to_update[0].updates, {at.F_PROF_MLX_FOLDER: "NIkki"})
        self.assertNotIn(at.F_PROF_NAME, plan.to_update[0].updates)

    def test_reconcile_off_falls_back_to_backfill_only(self):
        row = existing_row(name="Blank (24)", folder="Katja", tags=("Issue",), api_id=None)
        updates = self._updates(mlx_item(), row, reconcile=False)
        self.assertEqual(updates, {at.F_PROF_MLX_API_ID: "624354174112432228"})

    def test_previous_values_are_recorded_for_the_report(self):
        plan = plan_sync([mlx_item(serial_name="Luisa 3")],
                         {"158698": existing_row(name="Nikki 1")}, FOLDERS)
        self.assertEqual(plan.to_update[0].previous[at.F_PROF_NAME], "Nikki 1")


class FieldBuilderTest(TestCase):
    def setUp(self):
        self.p = normalize_mlx_item(mlx_item())

    def test_device_fields(self):
        fields = build_device_fields(self.p, model_id="recModel")
        self.assertEqual(fields[at.F_DEV_DEVICE_ID], "Nikki 1")
        self.assertEqual(fields[at.F_DEV_STATUS], "Active")
        self.assertEqual(fields[at.F_DEV_PHONE_MODEL_OS], "Samsung Galaxy S21 / Android 13")
        self.assertEqual(fields[at.F_DEV_MODEL], ["recModel"])

    def test_device_fields_without_model(self):
        self.assertNotIn(at.F_DEV_MODEL, build_device_fields(self.p, model_id=None))

    def test_proxy_fields_link_to_device(self):
        fields = build_proxy_fields(self.p, device_id="recDev")
        self.assertEqual(fields[at.F_PROX_PROVIDER], "Multilogin")
        self.assertEqual(fields[at.F_PROX_ENDPOINT], "1.2.3.4:8000")
        self.assertEqual(fields[at.F_PROX_ASSIGNED_DEVICE], ["recDev"])

    def test_profile_fields_store_both_ids_and_package(self):
        fields = build_profile_fields(self.p, device_id="recDev")
        self.assertEqual(fields[at.F_PROF_MLX_SERIAL], "158698")
        self.assertEqual(fields[at.F_PROF_MLX_API_ID], "624354174112432228")
        self.assertEqual(fields[at.F_PROF_APP_PACKAGE], "com.instagram.android")
        self.assertEqual(fields[at.F_PROF_DEVICE], ["recDev"])


class FakeClient:
    """Records create/update calls so apply_sync can be tested without a network."""

    def __init__(self):
        self.devices: list[dict] = []
        self.proxies: list[dict] = []
        self.profiles: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        # Flip to False to make every patch fail, the way a revoked token or a
        # field Airtable rejects would.
        self.update_ok = True

    def create_device(self, fields):
        self.devices.append(fields)
        return f"recDev{len(self.devices)}"

    def create_proxy(self, fields):
        self.proxies.append(fields)
        return f"recProx{len(self.proxies)}"

    def create_profile(self, fields):
        self.profiles.append(fields)
        return f"recProf{len(self.profiles)}"

    def update_profile(self, record_id, fields):
        if not self.update_ok:
            return False
        self.updates.append((record_id, fields))
        return True


class ApplySyncTest(TestCase):
    def test_dry_run_writes_nothing(self):
        plan = plan_sync([mlx_item()], existing_by_serial={})
        client = FakeClient()
        report = apply_sync(client, plan, models_by_name={"nikki": "recModel"}, dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.created, ["Nikki 1"])
        self.assertEqual(client.devices, [])  # nothing written

    def test_apply_creates_device_proxy_profile_linked(self):
        plan = plan_sync([mlx_item()], existing_by_serial={})
        client = FakeClient()
        report = apply_sync(client, plan, models_by_name={"nikki": "recModel"}, dry_run=False)
        self.assertEqual(report.created, ["Nikki 1"])
        self.assertEqual(len(client.devices), 1)
        self.assertEqual(client.devices[0][at.F_DEV_MODEL], ["recModel"])
        self.assertEqual(client.proxies[0][at.F_PROX_ASSIGNED_DEVICE], ["recDev1"])
        self.assertEqual(client.profiles[0][at.F_PROF_DEVICE], ["recDev1"])

    def test_apply_records_unmatched_model(self):
        # serial_name "Nikki 1" -> model "Nikki"; no Models row -> unmatched.
        plan = plan_sync([mlx_item()], existing_by_serial={})
        client = FakeClient()
        report = apply_sync(client, plan, models_by_name={}, dry_run=False)
        self.assertIn("Nikki", report.unmatched_models)
        # device still created, just without a Model link
        self.assertNotIn(at.F_DEV_MODEL, client.devices[0])

    def test_apply_reports_each_kind_of_change(self):
        row = existing_row(name="Blank (24)", folder="Katja", tags=("Issue",))
        plan = plan_sync([mlx_item(tags=["Created"])], {"158698": row}, FOLDERS)
        client = FakeClient()
        report = apply_sync(client, plan, models_by_name={}, dry_run=False)
        self.assertEqual(report.renamed, [("Blank (24)", "Nikki 1")])
        self.assertEqual(report.refoldered, [("Nikki 1", "NIkki")])
        self.assertEqual(report.retagged, ["Nikki 1"])
        # "Blank" -> "Nikki" moves the phone between models' content.
        self.assertEqual(report.renamed_model, [("Blank (24)", "Nikki 1")])

    def test_rename_within_a_model_is_not_a_model_change(self):
        plan = plan_sync([mlx_item(serial_name="Nikki 12")],
                         {"158698": existing_row()}, FOLDERS)
        report = apply_sync(FakeClient(), plan, models_by_name={}, dry_run=False)
        self.assertEqual(report.renamed, [("Nikki 1", "Nikki 12")])
        self.assertFalse(report.renamed_model)

    def test_apply_surfaces_a_refused_rename(self):
        items = [mlx_item(serial_name="Jasmin 11"),
                 mlx_item(serial_no="158699", id="2", serial_name="Jasmin 11")]
        plan = plan_sync(items, {"158698": existing_row(name="Blank (19)")}, FOLDERS)
        report = apply_sync(FakeClient(), plan, models_by_name={}, dry_run=False)
        self.assertEqual(len(report.refused), 1)
        self.assertIn("several profiles named", report.refused[0][1])

    def test_a_failed_update_is_not_counted_as_a_change(self):
        plan = plan_sync([mlx_item(serial_name="Nikki 12")],
                         {"158698": existing_row()}, FOLDERS)
        client = FakeClient()
        client.update_ok = False
        report = apply_sync(client, plan, models_by_name={}, dry_run=False)
        self.assertFalse(report.updated)
        self.assertFalse(report.renamed)
        self.assertEqual(len(report.errors), 1)

    def test_apply_backfills_update(self):
        existing = {"158698": existing_row(api_id=None)}
        plan = plan_sync([mlx_item()], existing, FOLDERS)
        client = FakeClient()
        report = apply_sync(client, plan, models_by_name={}, dry_run=False)
        self.assertEqual(report.updated, ["Nikki 1"])
        self.assertEqual(client.updates, [("recX", {at.F_PROF_MLX_API_ID: "624354174112432228"})])


class MobileListPaginationTest(TestCase):
    """The workspace has grown past one page; the sync must see all of it."""

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def _client_over(self, pages, total):
        """A client whose GET serves `pages` (a list of item-lists) in order."""
        from adb_bot.clients.multilogin.mobile_list import MultiloginMobileListClient

        calls = []

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(params["page"])
            index = params["page"] - 1
            items = pages[index] if index < len(pages) else []
            return self.FakeResponse({"data": {"items": items, "total": total}})

        client = MultiloginMobileListClient("tok")
        return client, fake_get, calls

    def test_walks_every_page(self):
        from unittest.mock import patch

        pages = [[{"serial_no": str(n)} for n in range(100)],
                 [{"serial_no": str(n)} for n in range(100, 136)]]
        client, fake_get, calls = self._client_over(pages, total=136)
        with patch("adb_bot.clients.multilogin.mobile_list.requests.get", fake_get):
            items = client.list_mobile_profiles()
        self.assertEqual(len(items), 136)
        self.assertEqual(calls, [1, 2])
        # The tail of the list -- the oldest profiles -- is what page 1 dropped.
        self.assertEqual(items[-1]["serial_no"], "135")

    def test_stops_on_a_short_page_when_total_is_wrong(self):
        from unittest.mock import patch

        # A `total` that overstates the truth must not spin forever.
        client, fake_get, calls = self._client_over([[{"serial_no": "1"}], []], total=9999)
        with patch("adb_bot.clients.multilogin.mobile_list.requests.get", fake_get):
            items = client.list_mobile_profiles()
        self.assertEqual(len(items), 1)
        self.assertEqual(calls, [1, 2])

    def test_single_page_makes_one_request(self):
        from unittest.mock import patch

        client, fake_get, calls = self._client_over([[{"serial_no": "1"}]], total=1)
        with patch("adb_bot.clients.multilogin.mobile_list.requests.get", fake_get):
            client.list_mobile_profiles()
        self.assertEqual(calls, [1])
