"""systemd backend: the unit text is what the server's behaviour depends on, so
it is pinned here the same way the Task Scheduler XML is.

The properties that matter are the ones that make systemd behave like the
Windows scheduler the loops were written for: no overlapping runs, missed runs
caught up after downtime, and a hard ceiling on a wedged run.
"""

import unittest
from pathlib import Path
from unittest import mock

from adb_bot.automation import schedule_spec, scheduler_admin, scheduling, systemd_admin as sd


class UnitNamingTest(unittest.TestCase):
    def test_unit_names(self):
        self.assertEqual(sd.unit_name("posting"), "adbbot-posting.service")
        self.assertEqual(sd.unit_name("mlx-sync", "timer"), "adbbot-mlx-sync.timer")

    def test_every_loop_has_a_default_interval(self):
        for loop in sd.LOOPS:
            self.assertIn(loop, schedule_spec.DEFAULT_INTERVALS)


class ServiceUnitTest(unittest.TestCase):
    def test_apply_flag_in_execstart(self):
        unit = sd.build_service_unit("posting", apply=True, python="/venv/bin/python",
                                     working_dir="/opt/adb_bot")
        self.assertIn("ExecStart=/venv/bin/python -m adb_bot.automation.run_loop posting --apply", unit)
        self.assertIn("WorkingDirectory=/opt/adb_bot", unit)

    def test_dry_run_omits_apply(self):
        unit = sd.build_service_unit("pipeline", apply=False, python="/venv/bin/python")
        # The loop's own scheduled flags stay; only --apply is dropped.
        self.assertIn("run_loop pipeline --targets profiles\n", unit)
        self.assertNotIn("--apply", unit)

    def test_oneshot_so_the_timer_can_own_the_schedule(self):
        self.assertIn("Type=oneshot", sd.build_service_unit("posting"))

    def test_runtime_ceiling_matches_windows_execution_time_limit(self):
        # Windows uses ExecutionTimeLimit=PT2H; this is the same 2 hours.
        self.assertIn(f"RuntimeMaxSec={2 * 60 * 60}", sd.build_service_unit("posting"))

    def test_env_file_is_optional(self):
        """A missing token file must not make the unit fail to start -- the loop
        should run and report the missing token instead."""
        self.assertIn("EnvironmentFile=-/etc/adbbot/env", sd.build_service_unit("posting"))

    def test_user_is_optional(self):
        self.assertNotIn("User=", sd.build_service_unit("posting"))
        self.assertIn("User=adbbot", sd.build_service_unit("posting", user="adbbot"))


class TimerUnitTest(unittest.TestCase):
    def test_sub_day_interval_repeats(self):
        unit = sd.build_timer_unit("posting", 10)
        self.assertIn("OnUnitActiveSec=10min", unit)
        self.assertIn("Unit=adbbot-posting.service", unit)

    def test_daily_interval_uses_wall_clock_start(self):
        unit = sd.build_timer_unit("mlx-sync", 1440)
        self.assertIn("OnCalendar=*-*-* 23:30:00", unit)
        self.assertNotIn("OnUnitActiveSec", unit)

    def test_daily_interval_custom_start(self):
        self.assertIn("OnCalendar=*-*-* 08:00:00", sd.build_timer_unit("warmup", 1440, "08:00"))

    def test_multi_day_interval(self):
        self.assertIn("OnUnitActiveSec=2d", sd.build_timer_unit("mlx-sync", 2880))

    def test_missed_daily_runs_are_caught_up(self):
        # Task Scheduler's StartWhenAvailable equivalent.
        self.assertIn("Persistent=true", sd.build_timer_unit("mlx-sync", 1440))

    def test_interval_timers_do_not_claim_persistence(self):
        """systemd only honours Persistent= on OnCalendar timers. Setting it on
        an interval timer does nothing and misleads whoever reads the unit."""
        self.assertNotIn("Persistent", sd.build_timer_unit("posting", 10))
        self.assertIn("OnBootSec=5min", sd.build_timer_unit("posting", 10))

    def test_interval_floored_to_one_minute(self):
        self.assertIn("OnUnitActiveSec=1min", sd.build_timer_unit("posting", 0))

    def test_installs_into_timers_target(self):
        self.assertIn("WantedBy=timers.target", sd.build_timer_unit("posting", 10))


class InstallTest(unittest.TestCase):
    """install() writes both units then hands off to systemctl."""

    def setUp(self):
        self.calls = []

        def fake_run(args):
            self.calls.append(args)
            return mock.Mock(returncode=0, stdout="", stderr="")

        patcher = mock.patch.object(sd, "_run", side_effect=fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        supported = mock.patch.object(sd, "is_supported", return_value=True)
        supported.start()
        self.addCleanup(supported.stop)

    def test_writes_units_and_enables_timer(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ok, _ = sd.install("posting", 10, apply=True, unit_dir=Path(directory))
            self.assertTrue(ok)
            self.assertTrue((Path(directory) / "adbbot-posting.service").exists())
            self.assertTrue((Path(directory) / "adbbot-posting.timer").exists())
        self.assertIn(["systemctl", "daemon-reload"], self.calls)
        self.assertIn(["systemctl", "enable", "--now", "adbbot-posting.timer"], self.calls)

    def test_remove_of_absent_timer_is_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ok, message = sd.remove("posting", unit_dir=Path(directory))
        self.assertTrue(ok)
        self.assertEqual(message, "not installed")

    def test_run_now_does_not_block_the_ui(self):
        sd.run_now("posting")
        self.assertIn(["systemctl", "start", "--no-block", "adbbot-posting.service"], self.calls)


class UnsupportedTest(unittest.TestCase):
    def test_calls_fail_cleanly_when_systemd_is_absent(self):
        with mock.patch.object(sd, "is_supported", return_value=False):
            for ok, message in (sd.install("posting", 10), sd.remove("posting"), sd.run_now("posting")):
                self.assertFalse(ok)
                self.assertIn("systemd", message)
            self.assertIsNone(sd.query_state("posting"))


class FacadeTest(unittest.TestCase):
    def test_prefers_windows_backend_on_windows(self):
        with mock.patch.object(scheduler_admin, "is_supported", return_value=True):
            self.assertIs(scheduling.backend(), scheduler_admin)
            self.assertEqual(scheduling.backend_name(), scheduling.WINDOWS)

    def test_falls_back_to_systemd(self):
        with mock.patch.object(scheduler_admin, "is_supported", return_value=False), \
             mock.patch.object(sd, "is_supported", return_value=True):
            self.assertIs(scheduling.backend(), sd)
            self.assertEqual(scheduling.backend_name(), scheduling.SYSTEMD)

    def test_no_backend_reports_rather_than_raising(self):
        with mock.patch.object(scheduler_admin, "is_supported", return_value=False), \
             mock.patch.object(sd, "is_supported", return_value=False):
            self.assertFalse(scheduling.is_supported())
            ok, message = scheduling.install("posting", 10)
            self.assertFalse(ok)
            self.assertTrue(message)
            self.assertEqual(scheduling.list_status(), {loop: None for loop in scheduling.LOOPS})

    def test_both_backends_share_one_api(self):
        for name in ("is_supported", "install", "remove", "run_now", "query_state", "list_status"):
            self.assertTrue(hasattr(scheduler_admin, name), name)
            self.assertTrue(hasattr(sd, name), name)


class PythonExeTest(unittest.TestCase):
    def test_finds_posix_venv_layout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / ".venv" / "bin" / "python").write_text("")
            with mock.patch.object(schedule_spec, "repo_root", return_value=root):
                self.assertEqual(schedule_spec.python_exe(),
                                 str(root / ".venv" / "bin" / "python"))

    def test_finds_windows_venv_layout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".venv" / "Scripts").mkdir(parents=True)
            (root / ".venv" / "Scripts" / "python.exe").write_text("")
            with mock.patch.object(schedule_spec, "repo_root", return_value=root):
                self.assertEqual(schedule_spec.python_exe(),
                                 str(root / ".venv" / "Scripts" / "python.exe"))

    def test_falls_back_to_current_interpreter(self):
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(schedule_spec, "repo_root", return_value=Path(directory)):
                self.assertEqual(schedule_spec.python_exe(), sys.executable)


if __name__ == "__main__":
    unittest.main()
