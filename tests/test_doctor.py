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
        def __init__(self, targets=None, models=None):
            self._targets = targets if targets is not None else {"jasmin": [{"handle": "j1"}]}
            self._models = models if models is not None else {"jasmin": "recM"}

        def profile_targets_by_model(self):
            return self._targets

        def models_by_name(self):
            return self._models

    class _Source:
        def __init__(self, folders):
            self._folders = folders

        def list_folder_names(self):
            return list(self._folders)

    def _patched(self, airtable, source, mlx_profiles=None, mlx_folders=None):
        """Run check_models against fakes: no network, no live settings.

        Only the two things that would reach out are replaced -- the raw source
        (Drive) and the Airtable client. Everything else, including the diff
        rules the check is judged on, runs for real.
        """
        from adb_bot.automation import model_inventory, spoof_pipeline

        original_collect = model_inventory.collect
        original_build = spoof_pipeline.build_source

        def fake_collect(**kwargs):
            return original_collect(airtable=airtable, raw_source=source,
                                    mlx_profiles=mlx_profiles, mlx_folders=mlx_folders,
                                    aliases={"corina": "Nikki", "mandy": "Luisa"})

        model_inventory.collect = fake_collect
        spoof_pipeline.build_source = lambda *a, **k: source
        try:
            # No MLX token, so the check never touches MultiLogin either.
            return doctor.check_models("tok", "appX", "", "/raw", "", "")
        finally:
            model_inventory.collect = original_collect
            spoof_pipeline.build_source = original_build

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
