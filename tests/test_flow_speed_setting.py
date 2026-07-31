"""Flow speed has to survive Apply *and* every later run.

Two things used to break it: starting a run rewrote the settings file from a
dict literal (dropping `flow_speed` entirely), and the dropdown itself sat in a
grid column past the right edge of the fixed-size Dev controls window, so it
could not be changed in the first place. These cover the persistence half; the
layout half is verified by rendering the window.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_bot.config import settings as settings_module
from adb_bot.automation.flows import waits
from adb_bot.ui.ui import WorkflowUI


class _Var:
    def __init__(self, value="") -> None:
        self._value = value

    def get(self):
        return self._value

    def set(self, value) -> None:
        self._value = value


class FlowSpeedPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.settings_file = Path(tempfile.mkdtemp()) / "dev_settings.json"
        self.settings_file.write_text(json.dumps({"flow_speed": "normal"}))
        patcher = patch.object(settings_module, "SETTINGS_FILE", self.settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The saved setting only shows through when no explicit override is set.
        waits.set_speed(None)
        self.addCleanup(waits.set_speed, None)

    def _ui(self) -> WorkflowUI:
        ui = WorkflowUI.__new__(WorkflowUI)
        ui.logger = MagicMock()
        ui._batch_launch_delay_seconds = 1
        ui._readiness_wait_seconds = 10
        ui._readiness_max_attempts = 2
        ui._shutdown_on_success = False
        ui.bearer_token_var = _Var("tok")
        ui.batch_launch_delay_var = _Var("1")
        ui.readiness_wait_var = _Var("10")
        ui.readiness_attempts_var = _Var("2")
        ui.story_media_path_var = _Var("")
        ui.airtable_token_var = _Var("")
        ui.airtable_base_id_var = _Var("")
        ui.airtable_table_name_var = _Var("Profiles")
        return ui

    def _apply(self, ui, speed: str) -> None:
        ui._dev_flow_speed_var = _Var(speed)
        ui._apply_dev_controls("tok", "1", "10", "2", "", MagicMock(), "", "", "Profiles")

    def test_apply_saves_the_selected_speed(self):
        ui = self._ui()
        self._apply(ui, "fast")
        self.assertEqual(json.loads(self.settings_file.read_text())["flow_speed"], "fast")
        self.assertEqual(settings_module.get_saved_flow_speed(), "fast")

    def test_the_saved_speed_reaches_the_flows(self):
        ui = self._ui()
        self._apply(ui, "fast")
        self.assertEqual(waits.speed_factor(), 0.5)
        self._apply(ui, "slow")
        self.assertEqual(waits.speed_factor(), 1.5)

    def test_starting_a_run_does_not_reset_the_speed(self):
        ui = self._ui()
        self._apply(ui, "fast")

        ui._run_in_progress = False
        ui._run_token = 0
        ui.abort_requested = False
        ui.profile_vars = {"p-1": _Var(True)}
        ui.profile_to_folder = {}
        ui.folder_media_paths = {}
        ui.folder_names = {}
        ui.shutdown_on_success_var = _Var(False)
        ui.run_button = MagicMock()
        ui.abort_button = MagicMock()
        ui._get_selected_flow_value = lambda: "warm_up_process"
        ui._get_launch_delay_seconds = lambda: 1
        ui._get_readiness_wait_seconds = lambda: 10
        ui._get_readiness_max_attempts = lambda: 2
        ui._get_active_bearer_token = lambda: "tok"
        ui._reset_run_statuses = MagicMock()
        ui._set_profile_status = MagicMock()
        with patch("adb_bot.ui.ui.threading.Thread"):
            ui.run_selected()

        self.assertEqual(settings_module.get_saved_flow_speed(), "fast")
        self.assertEqual(waits.speed_factor(), 0.5)

    def test_an_unknown_speed_falls_back_to_normal_speed(self):
        ui = self._ui()
        self._apply(ui, "turbo")
        self.assertEqual(waits.speed_factor(), 1.0)


if __name__ == "__main__":
    unittest.main()
