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
