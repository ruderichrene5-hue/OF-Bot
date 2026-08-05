from unittest import TestCase

from adb_bot.automation import scheduler_admin as sa


class TaskNamingTest(TestCase):
    def test_task_name(self):
        self.assertEqual(sa.task_name("posting"), "ADBBot-posting")
        self.assertEqual(sa.task_name("mlx-sync"), "ADBBot-mlx-sync")

    def test_loops_and_defaults_aligned(self):
        # Every loop has a default interval.
        for loop in sa.LOOPS:
            self.assertIn(loop, sa.DEFAULT_INTERVALS)


class TriggerXmlTest(TestCase):
    def test_sub_day_interval_repeats_all_day(self):
        xml = sa._triggers_xml("posting", 10, None)
        self.assertIn("<Repetition><Interval>PT10M</Interval>", xml)
        self.assertIn("<Duration>P1D</Duration>", xml)
        self.assertIn("<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>", xml)

    def test_daily_interval_has_no_repetition(self):
        xml = sa._triggers_xml("mlx-sync", 1440, None)
        self.assertNotIn("<Repetition>", xml)
        self.assertIn("<DaysInterval>1</DaysInterval>", xml)
        self.assertIn("T23:30:00", xml)  # default daily start for mlx-sync

    def test_daily_interval_custom_start(self):
        xml = sa._triggers_xml("warmup", 1440, "08:00")
        self.assertIn("T08:00:00", xml)

    def test_multi_day_interval(self):
        xml = sa._triggers_xml("mlx-sync", 2880, None)  # every 2 days
        self.assertIn("<DaysInterval>2</DaysInterval>", xml)


class BuildTaskXmlTest(TestCase):
    def test_apply_flag_in_arguments(self):
        xml = sa.build_task_xml("posting", 10, apply=True, python="py.exe", working_dir="C:/repo")
        self.assertIn("-m adb_bot.automation.run_loop posting --apply", xml)
        self.assertIn("<Command>py.exe</Command>", xml)
        self.assertIn("<WorkingDirectory>C:/repo</WorkingDirectory>", xml)

    def test_dry_run_omits_apply(self):
        xml = sa.build_task_xml("pipeline", 20, apply=False, python="py.exe", working_dir="C:/repo")
        # The loop's own scheduled flags stay; only --apply is dropped.
        self.assertIn("run_loop pipeline --targets profiles</Arguments>", xml)
        self.assertNotIn("--apply", xml)

    def test_is_well_formed_xml(self):
        import xml.etree.ElementTree as ET
        doc = sa.build_task_xml("warmup", 360, apply=True, python="C:/py.exe", working_dir="C:/repo")
        # Parses without error (strip the declaration line ElementTree dislikes).
        root = ET.fromstring(doc.split("?>", 1)[1])
        self.assertTrue(root.tag.endswith("Task"))

    def test_paths_are_xml_escaped(self):
        xml = sa.build_task_xml("posting", 10, python="C:/a&b/py.exe", working_dir="C:/x<y")
        self.assertIn("C:/a&amp;b/py.exe", xml)
        self.assertIn("C:/x&lt;y", xml)


class RunWhenLoggedOffTest(TestCase):
    def test_default_runs_as_interactive_user(self):
        xml = sa.build_task_xml("posting", 10, python="py.exe", working_dir="C:/repo")
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertNotIn("S-1-5-18", xml)

    def test_logged_off_runs_as_system(self):
        xml = sa.build_task_xml("posting", 10, python="py.exe", working_dir="C:/repo",
                                run_when_logged_off=True)
        self.assertIn("<UserId>S-1-5-18</UserId>", xml)         # LocalSystem
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertNotIn("InteractiveToken", xml)

    def test_logged_off_xml_still_well_formed(self):
        import xml.etree.ElementTree as ET
        doc = sa.build_task_xml("warmup", 1440, python="py.exe", working_dir="C:/repo",
                                run_when_logged_off=True)
        root = ET.fromstring(doc.split("?>", 1)[1])
        self.assertTrue(root.tag.endswith("Task"))


class IsoMinutesTest(TestCase):
    def test_iso_minutes(self):
        self.assertEqual(sa._iso_minutes(10), "PT10M")
        self.assertEqual(sa._iso_minutes(0), "PT1M")  # floored to 1
