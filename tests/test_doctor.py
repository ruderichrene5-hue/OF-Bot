from unittest import TestCase

from adb_bot.automation import doctor
from adb_bot.automation.doctor import FAIL, PASS, WARN, CheckResult


class ResultTest(TestCase):
    def test_ok_property(self):
        self.assertTrue(CheckResult("x", PASS).ok)
        self.assertFalse(CheckResult("x", WARN).ok)
        self.assertFalse(CheckResult("x", FAIL).ok)


class ExitCodeTest(TestCase):
    def test_fail_is_nonzero(self):
        self.assertEqual(doctor.exit_code([CheckResult("a", PASS), CheckResult("b", FAIL)]), 1)

    def test_warnings_alone_are_zero(self):
        # A warning means "that loop isn't configured yet", not a broken setup.
        self.assertEqual(doctor.exit_code([CheckResult("a", PASS), CheckResult("b", WARN)]), 0)

    def test_all_pass_is_zero(self):
        self.assertEqual(doctor.exit_code([CheckResult("a", PASS)]), 0)


class CredentialCheckTest(TestCase):
    def test_airtable_without_token_fails_with_hint(self):
        result = doctor.check_airtable("", "appX")
        self.assertEqual(result.status, FAIL)
        self.assertIn("AIRTABLE_TOKEN", result.hint)

    def test_multilogin_without_token_fails_with_hint(self):
        result = doctor.check_multilogin("")
        self.assertEqual(result.status, FAIL)
        self.assertIn("MULTILOGIN_TOKEN", result.hint)


class DriveCheckTest(TestCase):
    def test_unconfigured_is_only_a_warning(self):
        # Drive is optional -- the pipeline can read a local folder instead.
        self.assertEqual(doctor.check_drive("", "").status, WARN)

    def test_half_configured_fails(self):
        self.assertEqual(doctor.check_drive("key.json", "").status, FAIL)
        self.assertEqual(doctor.check_drive("", "folderid").status, FAIL)


class SpooferCheckTest(TestCase):
    def test_unconfigured_is_warning(self):
        self.assertEqual(doctor.check_spoofer("", "").status, WARN)

    def test_missing_interpreter_fails(self):
        result = doctor.check_spoofer("C:/nope/python.exe", "C:/nope")
        self.assertEqual(result.status, FAIL)
        self.assertIn("interpreter missing", result.detail)


class PathsCheckTest(TestCase):
    def test_drive_configured_counts_as_raw_source(self):
        results = doctor.check_paths("", "", "folder123")
        self.assertEqual(results[0].status, PASS)
        self.assertIn("Drive", results[0].detail)

    def test_missing_local_raw_dir_fails(self):
        results = doctor.check_paths("C:/definitely/not/here", "", "")
        self.assertEqual(results[0].status, FAIL)

    def test_no_raw_source_warns(self):
        results = doctor.check_paths("", "", "")
        self.assertEqual(results[0].status, WARN)

    def test_missing_out_dir_is_warning_not_failure(self):
        # The pipeline creates it on first run.
        results = doctor.check_paths("", "C:/not/created/yet", "folder")
        self.assertEqual(results[1].status, WARN)


class ReportTest(TestCase):
    def test_report_lists_every_check_and_the_totals(self):
        results = [CheckResult("Airtable", PASS, "base ok"),
                   CheckResult("FFmpeg", WARN, "missing", "install it"),
                   CheckResult("ADB", FAIL, "not found", "add to PATH")]
        text = doctor.format_report(results)
        for name in ("Airtable", "FFmpeg", "ADB"):
            self.assertIn(name, text)
        self.assertIn("1 passed, 1 warning(s), 1 failure(s)", text)
        self.assertIn("install it", text)      # hints shown for non-PASS
        self.assertIn("Fix the [FAIL]", text)

    def test_hints_hidden_for_passing_checks(self):
        text = doctor.format_report([CheckResult("X", PASS, "fine", "unused hint")])
        self.assertNotIn("unused hint", text)
        self.assertIn("All good.", text)

    def test_warn_only_report_says_ready(self):
        text = doctor.format_report([CheckResult("X", PASS), CheckResult("Y", WARN, "n/a")])
        self.assertIn("Ready.", text)


class RunChecksTest(TestCase):
    def test_uses_injected_settings_and_returns_results(self):
        class FakeSettings:
            get_saved_airtable_token = staticmethod(lambda: "")
            get_saved_airtable_base_id = staticmethod(lambda: "appX")
            get_saved_bearer_token = staticmethod(lambda: "")
            get_saved_raw_videos_dir = staticmethod(lambda: "")
            get_saved_spoofed_videos_dir = staticmethod(lambda: "")
            get_saved_drive_folder_id = staticmethod(lambda: "")
            get_saved_google_service_account_json = staticmethod(lambda: "")
            get_saved_spoofer_python = staticmethod(lambda: "")
            get_saved_spoofer_root = staticmethod(lambda: "")

        results = doctor.run_checks(FakeSettings)
        names = [r.name for r in results]
        for expected in ("Airtable", "MultiLogin", "ADB", "Google Drive", "Video spoofer", "Profile locks"):
            self.assertIn(expected, names)
        # With no credentials configured, the two token checks must fail loudly.
        by_name = {r.name: r for r in results}
        self.assertEqual(by_name["Airtable"].status, FAIL)
        self.assertEqual(by_name["MultiLogin"].status, FAIL)


class ModelInventoryCheckTest(TestCase):
    """Doctor is where a half-onboarded model has to become visible.

    Kathi and Katherine were real phones, warmed up for six days, invisible to
    every posting path. Nothing was broken -- so nothing FAILed, nothing
    alerted, and no number moved. A check that compares the systems on purpose
    is the only thing that can say it out loud.
    """

    class _Airtable:
        def __init__(self, targets=None, models=None, profiles=None):
            self._targets = targets if targets is not None else {"jasmin": [{"handle": "j1"}]}
            self._models = models if models is not None else {"jasmin": "recM"}
            self._profiles = profiles or []

        def profile_targets_by_model(self):
            return self._targets

        def models_by_name(self):
            return self._models

        def posting_profiles(self):
            return list(self._profiles)

    class _Source:
        def __init__(self, folders):
            self._folders = folders

        def list_folder_names(self):
            return list(self._folders)

    class _BrokenSource:
        """A raw source that is configured but cannot answer -- Drive 403, an
        expired key, a network blip."""

        def list_folder_names(self):
            raise RuntimeError("drive says 403")

    def _patched(self, airtable, source, mlx_profiles=None, mlx_folders=None):
        """Run check_models against fakes: no network, no live settings.

        Only the things that would reach out are replaced -- the raw source
        (Drive), the Airtable client and the two MultiLogin clients. Everything
        in between, `model_inventory.collect` included, runs for real.

        That last part is the point. This helper used to swap in a
        `fake_collect(**kwargs)` that threw its kwargs away and rebuilt the
        inventory from the closure, so check_models could have passed
        `raw_source=None` -- or nothing at all -- and every test here still
        passed. The wiring is most of what this check is.
        """
        import contextlib
        from unittest.mock import patch

        from adb_bot.automation import spoof_pipeline

        mlx_token = "mlxtok" if (mlx_profiles is not None or mlx_folders is not None) else ""
        # The per-run listing memo is keyed by token; two tests using the same
        # fake token must not share a fleet.
        doctor._probe_cache.clear()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(spoof_pipeline, "build_source",
                                             lambda *a, **k: source))
            stack.enter_context(patch("adb_bot.clients.airtable.AirtableClient",
                                      lambda *a, **k: airtable))
            stack.enter_context(patch.object(
                spoof_pipeline, "raw_folder_model_aliases",
                lambda: {"corina": "Nikki", "mandy": "Luisa"}))
            if mlx_token:
                stack.enter_context(patch(
                    "adb_bot.clients.multilogin.mobile_list.MultiloginMobileListClient",
                    lambda *a, **k: self._MlxProfiles(mlx_profiles or [])))
                stack.enter_context(patch(
                    "adb_bot.clients.multilogin.folders.MultiloginFolderClient",
                    lambda *a, **k: self._MlxFolders(mlx_folders or [])))
            # raw_root is set and drive_folder is not, so the Drive shortcut is
            # not taken and build_source (patched above) decides the source.
            return doctor.check_models("tok", "appX", mlx_token, "/raw", "", "")

    class _MlxProfiles:
        def __init__(self, items):
            self._items = items

        def list_mobile_profiles(self):
            return list(self._items)

    class _MlxFolders:
        def __init__(self, items):
            self._items = items

        def list_mobile_folders(self):
            return list(self._items)

    def test_a_matching_inventory_passes(self):
        result = self._patched(self._Airtable(), self._Source(["Jasmin"]))
        self.assertEqual(result.status, PASS)

    def test_an_empty_raw_folder_with_no_profiles_warns(self):
        result = self._patched(self._Airtable(), self._Source(["Jasmin", "Katherine"]))
        self.assertEqual(result.status, WARN)
        self.assertIn("Katherine", result.detail)
        self.assertTrue(result.hint)

    def test_a_new_mlx_folder_of_blank_profiles_warns(self):
        result = self._patched(
            self._Airtable(), self._Source(["Jasmin"]),
            mlx_profiles=[{"folder_id": "f1", "serial_name": "Blank (1)"}],
            mlx_folders=[{"folder_id": "f1", "name": "Kathi"}])
        self.assertEqual(result.status, WARN)
        self.assertIn("Kathi", result.detail)

    def test_it_never_fails_the_run(self):
        # Nothing here is broken; a FAIL would block --apply runs that are fine.
        result = self._patched(self._Airtable(), self._Source(["Kathi", "Katherine", "Lou"]))
        self.assertNotEqual(result.status, FAIL)

    def test_without_an_airtable_token_it_warns_rather_than_raising(self):
        self.assertEqual(doctor.check_models("", "appX", "", "", "", "").status, WARN)

    def test_the_raw_source_actually_reaches_the_diff(self):
        # The wiring, asserted directly: the folder list handed to check_models
        # is the one the rules are run against. A check that dropped its
        # raw_source argument would pass every other test in this class.
        fleet = self._Airtable(targets={"nikki": [{"handle": "n1"}]}, models={"nikki": "recN"})
        # Corina is aliased to Nikki, whose targets exist: nothing to say.
        self.assertEqual(self._patched(fleet, self._Source(["Corina"])).status, PASS)
        # Same fleet, one more folder in the listing -- the answer must change.
        result = self._patched(fleet, self._Source(["Corina", "Katherine"]))
        self.assertEqual(result.status, WARN)
        self.assertIn("Katherine", result.detail)

    # --- the raw source degrading must not invent gaps -----------------------
    #
    # Reproduced in review: with no Drive folder id configured, the raw folder
    # set is empty, and "targets with no raw folder" then fires for EVERY model
    # with active profiles -- 5 real gaps became 9, four of them telling the
    # operator to create Drive folders that already exist. An unreadable source
    # has to mean "not compared", never "nothing there".

    _FLEET = {"jasmin": [{"handle": "j1"}], "jil": [{"handle": "i1"}],
              "katja": [{"handle": "k1"}], "nikki": [{"handle": "n1"}]}

    def test_no_raw_source_at_all_reports_no_missing_folders(self):
        # build_source returns None when neither DRIVE_RAW_FOLDER_ID + key nor
        # RAW_VIDEOS_DIR is set. That is a configuration gap, not four models
        # missing their content.
        result = self._patched(self._Airtable(targets=self._FLEET, models={}), None)
        self.assertNotIn("targets_no_raw_folder", result.detail)
        self.assertNotIn("raw_folder_no_targets", result.detail)

    def test_a_raw_source_that_raises_reports_no_missing_folders(self):
        result = self._patched(self._Airtable(targets=self._FLEET, models={}),
                               self._BrokenSource())
        self.assertNotIn("targets_no_raw_folder", result.detail)
        self.assertNotIn("raw_folder_no_targets", result.detail)
        self.assertIn("403", result.detail)      # and it says why it could not look

    def test_a_raw_source_that_lists_nothing_reports_no_missing_folders(self):
        # An empty listing is not "every model lost its folder" -- on a live box
        # it is a permission or folder-id problem, and it produces the same storm.
        result = self._patched(self._Airtable(targets=self._FLEET, models={}),
                               self._Source([]))
        self.assertNotIn("targets_no_raw_folder", result.detail)

    def test_the_partial_warning_leads_the_detail(self):
        # Trailing, it reads as a footnote on a line that already said everything
        # lines up. The operator has to know the comparison was half-done first.
        result = self._patched(self._Airtable(targets=self._FLEET, models={}), None)
        self.assertTrue(result.detail.startswith("[partial:"), result.detail)
        self.assertIn("raw source unreadable", result.detail)

    def test_a_dead_raw_source_does_not_blind_the_mlx_rule(self):
        # Rule 1 reads no raw folders, so it must survive: this is the finding
        # that fires days before any clip is uploaded.
        result = self._patched(
            self._Airtable(targets=self._FLEET, models={}), self._BrokenSource(),
            mlx_profiles=[{"folder_id": "f1", "serial_name": "Blank (1)"}],
            mlx_folders=[{"folder_id": "f1", "name": "Kathi"}])
        self.assertEqual(result.status, WARN)
        self.assertIn("mlx_folder_unnamed", result.detail)
        self.assertIn("Kathi", result.detail)

    def test_drive_is_listed_once_per_run_not_once_per_check(self):
        """check_drive and check_models want the same subfolder listing.

        Asking Drive twice every 30 minutes for an answer that cannot have
        changed between the two lines of one report is waste, and on MultiLogin
        (same memo) it is waste on a token whose expiry is a known failure mode.
        """
        from unittest.mock import patch

        calls = []

        class _Drive:
            def __init__(self, *a, **k):
                pass

            def list_subfolders(self, folder_id):
                calls.append(folder_id)
                return [{"id": "d1", "name": "Jasmin"}]

        doctor._probe_cache.clear()
        with patch("adb_bot.clients.gdrive.DriveClient", _Drive), \
             patch("adb_bot.clients.airtable.AirtableClient", lambda *a, **k: self._Airtable()):
            self.assertEqual(doctor.check_drive("key.json", "rootF").status, PASS)
            models = doctor.check_models("tok", "appX", "", "", "rootF", "key.json")
        self.assertEqual(calls, ["rootF"])          # one listing, two checks
        self.assertEqual(models.status, PASS)       # and it really was used
        doctor._probe_cache.clear()

    def test_the_memo_does_not_survive_the_next_run(self):
        # A cache that outlived a run would make `doctor` report a fleet it read
        # minutes or hours ago -- worse than the duplicate call it replaces.
        doctor._probe_cache[("mlx_mobile_profiles", "tok")] = ([], None)

        class FakeSettings:
            get_saved_airtable_token = staticmethod(lambda: "")
            get_saved_airtable_base_id = staticmethod(lambda: "appX")
            get_saved_bearer_token = staticmethod(lambda: "")
            get_saved_raw_videos_dir = staticmethod(lambda: "")
            get_saved_spoofed_videos_dir = staticmethod(lambda: "")
            get_saved_drive_folder_id = staticmethod(lambda: "")
            get_saved_google_service_account_json = staticmethod(lambda: "")
            get_saved_spoofer_python = staticmethod(lambda: "")
            get_saved_spoofer_root = staticmethod(lambda: "")

        doctor.run_checks(FakeSettings)
        self.assertNotIn(("mlx_mobile_profiles", "tok"), doctor._probe_cache)

    def test_it_is_part_of_the_standard_run(self):
        class FakeSettings:
            get_saved_airtable_token = staticmethod(lambda: "")
            get_saved_airtable_base_id = staticmethod(lambda: "appX")
            get_saved_bearer_token = staticmethod(lambda: "")
            get_saved_raw_videos_dir = staticmethod(lambda: "")
            get_saved_spoofed_videos_dir = staticmethod(lambda: "")
            get_saved_drive_folder_id = staticmethod(lambda: "")
            get_saved_google_service_account_json = staticmethod(lambda: "")
            get_saved_spoofer_python = staticmethod(lambda: "")
            get_saved_spoofer_root = staticmethod(lambda: "")

        self.assertIn("Model inventory", [r.name for r in doctor.run_checks(FakeSettings)])
