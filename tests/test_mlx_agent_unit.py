"""The MultiLogin agent's systemd unit.

Unlike the loop units this one is a static file, not generated -- it describes
the machine's MLX install rather than anything in this repo. So the file itself
is what these tests pin, and they pin the properties that were read off the live
hand-started process it replaces (see the header comment in the unit).

The failure this unit exists to prevent (TODO_2026-08-05 3.1) is a *silent* one:
the agent died, nothing restarted it, nothing said so, and every phone launch
failed for an hour. Each assertion below is one of the ways that could happen
again.
"""

import re
import unittest
from pathlib import Path

from adb_bot.automation import schedule_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO_ROOT / "deploy" / "systemd" / "adbbot-mlx-agent.service"
INSTALLER = REPO_ROOT / "deploy" / "systemd" / "install_mlx_agent.sh"


def sections(text: str) -> dict:
    """{section: [(key, value), ...]}. Duplicate keys are kept -- After= and
    Environment= are legitimately repeated, so a dict-of-dicts would lie."""
    out, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out.setdefault(current, [])
        elif "=" in line and current:
            key, _, value = line.partition("=")
            out[current].append((key.strip(), value.strip()))
    return out


class UnitFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = UNIT_PATH.read_text(encoding="utf-8")
        cls.sections = sections(cls.text)

    def values(self, section, key):
        return [v for k, v in self.sections.get(section, []) if k == key]

    def one(self, section, key):
        found = self.values(section, key)
        self.assertEqual(len(found), 1, f"expected exactly one {key}= in [{section}], got {found}")
        return found[0]

    # --- what the live process told us -------------------------------------

    def test_execstart_is_the_agent_not_the_launcher(self):
        """The process listening on :45001 is a *child* of agent.bin, living at
        deps/launcher/<version>/. Pointing the unit at that child would break at
        MLX's next self-update, when the version in the path changes."""
        self.assertEqual(self.one("Service", "ExecStart"), "/opt/mlx/agent.bin")
        self.assertNotIn("launcher-linux", self.text.split("[Service]")[1])

    def test_no_arguments(self):
        # /proc/<pid>/cmdline was a single NUL-terminated entry.
        self.assertNotIn(" ", self.one("Service", "ExecStart").strip())

    def test_working_directory_matches_the_live_cwd(self):
        self.assertEqual(self.one("Service", "WorkingDirectory"), "/opt/mlx")

    def test_display_environment(self):
        """Without these the agent starts and then cannot drive a phone window.
        Values are the live process's, not the older xrdp session's :10.0."""
        env = self.values("Service", "Environment")
        self.assertIn("DISPLAY=:0", env)
        self.assertIn("XAUTHORITY=/var/run/lightdm/root/:0", env)

    def test_runs_as_root(self):
        """No User= override: the X cookie at /var/run/lightdm/root/:0 is mode
        0600 root, so any other user cannot open the display."""
        self.assertEqual(self.values("Service", "User"), [])

    # --- the supervision the TODO asked for --------------------------------

    def test_restarts_always(self):
        self.assertEqual(self.one("Service", "Restart"), "always")

    def test_restart_is_not_instant(self):
        """A 100ms default would spin the crash loop straight into the start
        rate limit."""
        self.assertTrue(self.values("Service", "RestartSec"))

    def test_always_is_not_capped_by_the_start_rate_limit(self):
        """StartLimitIntervalSec belongs in [Unit]; put in [Service] it is
        silently ignored and the unit still latches into `failed` after 5 fast
        restarts -- another silent hour."""
        self.assertEqual(self.one("Unit", "StartLimitIntervalSec"), "0")

    def test_ordered_after_the_display(self):
        after = " ".join(self.values("Unit", "After"))
        self.assertIn("graphical.target", after)

    def test_survives_reboot(self):
        self.assertEqual(self.one("Install", "WantedBy"), "multi-user.target")

    def test_long_running_service_not_oneshot(self):
        """oneshot would make systemd consider the agent 'done' the moment it
        forks its launcher, and Restart= would fight it."""
        self.assertEqual(self.one("Service", "Type"), "simple")

    def test_a_child_oom_does_not_take_the_agent_down(self):
        """The cgroup holds one phone_launcher per live profile (~170 MB each);
        those are what the OOM killer picks. The default OOMPolicy=stop would
        tear down the agent and every other live phone along with the one that
        was killed -- the 2026-08-04 incident."""
        self.assertEqual(self.one("Service", "OOMPolicy"), "continue")

    def test_stop_reaps_the_launcher_child(self):
        """An orphaned launcher keeps :45001 bound, so the restarted agent
        cannot come back."""
        self.assertEqual(self.one("Service", "KillMode"), "control-group")

    def test_logs_to_the_journal_like_the_loop_units(self):
        self.assertEqual(self.one("Service", "StandardOutput"), "journal")
        self.assertEqual(self.one("Service", "StandardError"), "journal")

    def test_env_file_is_optional(self):
        """Same convention as the loop units: a missing token file must not stop
        the agent from starting (it does not read our tokens at all)."""
        self.assertEqual(self.one("Service", "EnvironmentFile"), "-/etc/adbbot/env")

    # --- traps specific to Exec lines --------------------------------------

    def test_exec_lines_use_no_shell_variables(self):
        """systemd expands $VAR in Exec= itself before /bin/sh ever sees it, so
        a shell loop counter would silently become the empty string."""
        for key, value in self.sections["Service"]:
            if key.startswith("Exec"):
                self.assertNotIn("$", value, f"{key}= must not rely on shell $ expansion")
                self.assertNotIn("%", value, f"{key}= must not contain a systemd %specifier")

    # --- naming / wiring ----------------------------------------------------

    def test_named_like_the_other_adbbot_units(self):
        self.assertTrue(UNIT_PATH.name.startswith(schedule_spec.UNIT_PREFIX))
        self.assertTrue(UNIT_PATH.name.endswith(".service"))

    def test_not_a_loop_name(self):
        """adbbot-mlx-agent must not collide with the adbbot-mlx-sync loop, nor
        be mistaken for one: install_units.sh would try to build it from the
        loop builders."""
        stem = UNIT_PATH.stem[len(schedule_spec.UNIT_PREFIX):]
        self.assertNotIn(stem, schedule_spec.LOOPS)
        self.assertNotIn(stem, schedule_spec.RECOMMENDED_LOOPS)
        self.assertNotIn(stem, schedule_spec.PLANNED_LOOPS)


class InstallerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALLER.read_text(encoding="utf-8")

    def test_installer_is_executable(self):
        self.assertTrue(INSTALLER.stat().st_mode & 0o111)

    def test_installer_references_the_unit_that_exists(self):
        self.assertIn(UNIT_PATH.name, self.text)

    def test_installer_does_not_start_by_default(self):
        """Starting a second agent while the hand-started one still holds :45001
        gives a crash loop, so enabling is opt-in. Every `systemctl enable` must
        sit inside the --enable branch."""
        self.assertIn("--enable", self.text)
        lines = self.text.splitlines()
        enables = [i for i, line in enumerate(lines) if line.strip().startswith("systemctl enable")]
        self.assertTrue(enables, "installer never enables the unit at all")
        for i in enables:
            # Must be nested in a block ...
            self.assertTrue(lines[i].startswith((" ", "\t")),
                            f"line {i + 1} enables the unit at top level, unconditionally")
            # ... and the block it is nested in must be the --enable one.
            opener = next((line for line in reversed(lines[:i]) if line.lstrip().startswith("if [[")), "")
            self.assertIn('"$ACTION" == "enable"', opener,
                          f"line {i + 1} enables the unit outside the --enable branch")

    def test_installer_is_separate_from_the_timer_installer(self):
        """install_units.sh installs loop timers and its --apply starts live
        posting; the agent must not ride along on that flag."""
        units_sh = (REPO_ROOT / "deploy" / "systemd" / "install_units.sh").read_text(encoding="utf-8")
        self.assertNotIn("mlx-agent", units_sh)

    def test_takeover_instructions_stop_the_loose_process_first(self):
        self.assertTrue(re.search(r"pgrep.*agent\\?\.bin", self.text))
        self.assertIn("45001", self.text)


if __name__ == "__main__":
    unittest.main()
