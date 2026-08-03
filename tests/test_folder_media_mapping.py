"""Per-Multilogin-folder reels media folders.

A folder is mapped once in the Profiles list and reused by every later run. The
contract these pin down:

* a profile only ever draws media from the folder mapped to *its own* Multilogin
  folder -- folder X never reaches into folder Y's clips;
* an unmapped folder falls back to the previous global behaviour, so a setup
  with no mappings at all runs exactly as it did before;
* the mapping survives a run and a profile refresh (it is merged into the
  settings file, not overwritten).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adb_bot.config import settings as settings_module
from adb_bot.ui.helpers import (
    REEL_FLOWS,
    folders_missing_media,
    resolve_folder_media_path,
)
from adb_bot.ui.ui import WorkflowUI


class _Var:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


class ResolveFolderMediaTests(unittest.TestCase):
    def setUp(self):
        self.profile_to_folder = {"p-a1": "folder-A", "p-a2": "folder-A", "p-b1": "folder-B"}
        self.mapping = {"folder-A": "/media/model-a", "folder-B": "/media/model-b"}

    def test_profile_gets_its_own_folder_media(self):
        self.assertEqual(
            resolve_folder_media_path("p-a1", self.profile_to_folder, self.mapping),
            str(Path("/media/model-a")),
        )
        self.assertEqual(
            resolve_folder_media_path("p-b1", self.profile_to_folder, self.mapping),
            str(Path("/media/model-b")),
        )

    def test_profiles_in_one_folder_share_that_folders_media(self):
        first = resolve_folder_media_path("p-a1", self.profile_to_folder, self.mapping)
        second = resolve_folder_media_path("p-a2", self.profile_to_folder, self.mapping)
        self.assertEqual(first, second)

    def test_unmapped_folder_never_borrows_a_mapped_one(self):
        # folder-B mapped, folder-C not: folder-C's profile must fall back to the
        # global setting (None), not silently pick up folder-B's clips.
        profile_to_folder = {"p-b1": "folder-B", "p-c1": "folder-C"}
        mapping = {"folder-B": "/media/model-b"}
        self.assertIsNone(resolve_folder_media_path("p-c1", profile_to_folder, mapping))

    def test_no_mappings_at_all_keeps_the_old_behaviour(self):
        self.assertIsNone(resolve_folder_media_path("p-a1", self.profile_to_folder, {}))

    def test_unknown_profile_and_blank_values_fall_back(self):
        self.assertIsNone(resolve_folder_media_path("ghost", self.profile_to_folder, self.mapping))
        self.assertIsNone(resolve_folder_media_path("p-a1", {"p-a1": ""}, {"": "/media/x"}))
        self.assertIsNone(resolve_folder_media_path("p-a1", self.profile_to_folder, {"folder-A": "   "}))


class FoldersMissingMediaTests(unittest.TestCase):
    def test_lists_only_selected_unmapped_folders_once(self):
        missing = folders_missing_media(
            ["p-a1", "p-a2", "p-c1"],
            {"p-a1": "folder-A", "p-a2": "folder-A", "p-c1": "folder-C"},
            {"folder-A": "/media/model-a"},
            {"folder-A": "Model A", "folder-C": "Model C"},
        )
        self.assertEqual(missing, ["Model C"])

    def test_all_mapped_reports_nothing(self):
        missing = folders_missing_media(
            ["p-a1"], {"p-a1": "folder-A"}, {"folder-A": "/media/model-a"}, {"folder-A": "Model A"}
        )
        self.assertEqual(missing, [])


class FolderMediaSettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings_file = Path(tempfile.mkdtemp()) / "dev_settings.json"
        self.settings_file.write_text(json.dumps({"bearer_token": "keep-me", "scheduler": {"a": 1}}))
        patcher = patch.object(settings_module, "SETTINGS_FILE", self.settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_merges_and_reads_back(self):
        self.assertTrue(settings_module.save_folder_media_paths({"folder-A": "/media/model-a"}))
        saved = json.loads(self.settings_file.read_text())
        # The unrelated config must survive the write.
        self.assertEqual(saved["bearer_token"], "keep-me")
        self.assertEqual(saved["scheduler"], {"a": 1})
        self.assertEqual(settings_module.get_folder_media_path("folder-A"), str(Path("/media/model-a")))

    def test_blank_entries_are_dropped_and_unmapped_reads_empty(self):
        settings_module.save_folder_media_paths({"folder-A": "/media/model-a", "folder-B": "  "})
        self.assertEqual(settings_module.get_folder_media_paths(), {"folder-A": "/media/model-a"})
        self.assertEqual(settings_module.get_folder_media_path("folder-B"), "")
        self.assertEqual(settings_module.get_folder_media_path(""), "")

    def test_corrupt_mapping_is_ignored(self):
        self.settings_file.write_text(json.dumps({"folder_media_paths": "not-a-dict"}))
        self.assertEqual(settings_module.get_folder_media_paths(), {})


class RunWiringTests(unittest.TestCase):
    """The run path hands each profile its own folder's media -- and only for the
    reel flows, so every other flow keeps resolving media the way it always did."""

    def _ui(self, flow: str, mapping: dict[str, str]) -> WorkflowUI:
        ui = WorkflowUI.__new__(WorkflowUI)
        ui.abort_requested = False
        ui._run_token = 1
        ui.logger = MagicMock()
        ui.profile_to_folder = {"p-a1": "folder-A", "p-b1": "folder-B", "p-c1": "folder-C"}
        ui.folder_media_paths = dict(mapping)
        ui._run_folder_media = dict(mapping)
        ui.folder_names = {"folder-A": "Model A", "folder-B": "Model B", "folder-C": "Model C"}
        ui.reel_caption_var = _Var("caption")
        ui.bio_var = _Var("")
        ui.picture_var = _Var("")
        ui._get_selected_flow_value = lambda: flow
        ui._set_profile_status = MagicMock()
        ui._disable_manual_continue_button = MagicMock()
        return ui

    def _media_path_passed(self, ui: WorkflowUI, profile_id: str):
        with patch("adb_bot.ui.ui.run_profile_workflow") as run_workflow:
            ui._run_single_profile(
                profile_id, "token", MagicMock(), MagicMock(), MagicMock(), MagicMock(), 1
            )
        return run_workflow.call_args.kwargs["media_path"]

    def test_reel_run_uses_the_profiles_own_folder(self):
        ui = self._ui("instagram_reel_upload", {"folder-A": "/media/a", "folder-B": "/media/b"})
        self.assertEqual(self._media_path_passed(ui, "p-a1"), str(Path("/media/a")))
        self.assertEqual(self._media_path_passed(ui, "p-b1"), str(Path("/media/b")))

    def test_unmapped_folder_falls_back_instead_of_borrowing(self):
        ui = self._ui("instagram_reel_upload", {"folder-A": "/media/a"})
        self.assertIsNone(self._media_path_passed(ui, "p-c1"))

    def test_u2_reel_flow_is_wired_too(self):
        ui = self._ui("instagram_reel_upload_u2", {"folder-A": "/media/a"})
        self.assertEqual(self._media_path_passed(ui, "p-a1"), str(Path("/media/a")))

    def test_non_reel_flows_are_untouched(self):
        for flow in ("warm_up_process", "instagram_story_upload", "update_bio", "update_profile_picture"):
            ui = self._ui(flow, {"folder-A": "/media/a"})
            self.assertIsNone(self._media_path_passed(ui, "p-a1"), flow)

    def test_run_uses_the_snapshot_not_a_later_edit(self):
        # Remapping a folder mid-run must not redirect the profiles already running.
        ui = self._ui("instagram_reel_upload", {"folder-A": "/media/a"})
        ui.folder_media_paths["folder-A"] = "/media/changed-mid-run"
        self.assertEqual(self._media_path_passed(ui, "p-a1"), str(Path("/media/a")))

    def test_confirm_is_silent_when_no_folder_is_mapped(self):
        ui = self._ui("instagram_reel_upload", {})
        with patch("adb_bot.ui.ui.messagebox") as box:
            self.assertTrue(ui._confirm_folder_media(["p-a1", "p-c1"]))
        box.askokcancel.assert_not_called()

    def test_confirm_warns_once_a_mapping_exists_and_a_folder_lacks_one(self):
        ui = self._ui("instagram_reel_upload", {"folder-A": "/media/a"})
        with patch("adb_bot.ui.ui.messagebox") as box:
            box.askokcancel.return_value = False
            self.assertFalse(ui._confirm_folder_media(["p-a1", "p-c1"]))
            self.assertIn("Model C", box.askokcancel.call_args.args[1])

    def test_confirm_is_silent_when_every_selected_folder_is_mapped(self):
        ui = self._ui("instagram_reel_upload", {"folder-A": "/media/a", "folder-B": "/media/b"})
        with patch("adb_bot.ui.ui.messagebox") as box:
            self.assertTrue(ui._confirm_folder_media(["p-a1", "p-b1"]))
        box.askokcancel.assert_not_called()


class AirtableRunnerWiringTests(unittest.TestCase):
    """The Airtable path resolves the same way, so a lifecycle-driven reels run
    cannot pull another folder's clips either."""

    def _run_flows(self, flow: str, resolver):
        from adb_bot.automation import airtable_runner

        account_plan = MagicMock(launch_id="p-a1", account_id="acc1", account_name="Model A")
        flow_run = MagicMock(flow=flow, caption="c", bio=None, picture=None)
        account_plan.runs = [flow_run]
        plan = MagicMock(plans=[account_plan], skipped=[])

        airtable = MagicMock()
        airtable.create_run_log.return_value = "log1"
        # No `time.sleep` patch needed any more: the batch-wide readiness sleep
        # is gone, and each profile now waits for its own readiness inside
        # run_profile_workflow (mocked here).
        with patch.object(airtable_runner, "run_profile_workflow") as run_workflow, \
                patch.object(airtable_runner, "_missing_input_reason", return_value=None):
            airtable_runner._launch_and_run_flows(
                plan, ["p-a1"], airtable, MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                MagicMock(), MagicMock(), 0, 1, 0, None, None, None, False,
                media_path_resolver=resolver,
            )
        return run_workflow.call_args.kwargs

    def test_reel_flow_uses_the_resolver(self):
        kwargs = self._run_flows("instagram_reel_upload", lambda pid: f"/media/{pid}")
        self.assertEqual(kwargs["media_path"], "/media/p-a1")

    def test_non_reel_flow_ignores_the_resolver(self):
        kwargs = self._run_flows("update_bio", lambda pid: f"/media/{pid}")
        self.assertIsNone(kwargs["media_path"])

    def test_no_resolver_keeps_the_previous_behaviour(self):
        kwargs = self._run_flows("instagram_reel_upload", None)
        self.assertIsNone(kwargs["media_path"])

    def test_a_failing_resolver_does_not_break_the_run(self):
        def boom(_pid):
            raise RuntimeError("nope")

        kwargs = self._run_flows("instagram_reel_upload", boom)
        self.assertIsNone(kwargs["media_path"])


class RunKeepsSettingsTests(unittest.TestCase):
    """Starting a run used to rewrite the whole settings file from a literal,
    which would drop the folder mappings (and the Airtable/Scheduler config)
    every single time. It has to merge."""

    def setUp(self):
        self.settings_file = Path(tempfile.mkdtemp()) / "dev_settings.json"
        self.settings_file.write_text(json.dumps({
            "folder_media_paths": {"folder-A": "/media/a"},
            "airtable_token": "keep-me",
            "scheduler": {"posting": {"enabled": True}},
            "flow_speed": "fast",
        }))
        patcher = patch.object(settings_module, "SETTINGS_FILE", self.settings_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_starting_a_run_preserves_the_rest_of_the_file(self):
        ui = WorkflowUI.__new__(WorkflowUI)
        ui._run_in_progress = False
        ui._run_token = 0
        ui.abort_requested = False
        ui.logger = MagicMock()
        ui.profile_vars = {"p-a1": _Var(True)}
        ui.profile_to_folder = {"p-a1": "folder-A"}
        ui.folder_media_paths = {"folder-A": "/media/a"}
        ui.folder_names = {"folder-A": "Model A"}
        ui.story_media_path_var = _Var("")
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

        with patch("adb_bot.ui.ui.threading.Thread") as thread:
            ui.run_selected()
        thread.assert_called_once()

        saved = json.loads(self.settings_file.read_text())
        self.assertEqual(saved["folder_media_paths"], {"folder-A": "/media/a"})
        self.assertEqual(saved["airtable_token"], "keep-me")
        self.assertEqual(saved["scheduler"], {"posting": {"enabled": True}})
        self.assertEqual(saved["flow_speed"], "fast")
        self.assertEqual(saved["bearer_token"], "tok")


class RefreshKeepsMappingTests(unittest.TestCase):
    def test_clearing_the_profile_list_keeps_the_mappings(self):
        ui = WorkflowUI.__new__(WorkflowUI)
        for name in (
            "profile_vars", "profile_labels", "profile_status_vars", "profile_last_status",
            "folder_sections", "folder_toggles", "folder_expanded", "folder_names",
            "folder_media_vars", "profile_to_folder", "profile_rows",
        ):
            setattr(ui, name, {})
        ui.folder_media_paths = {"folder-A": "/media/a"}
        ui.folder_media_vars["folder-A"] = _Var("x")
        ui.profile_search_var = None
        ui.profile_container = MagicMock(winfo_children=lambda: [])
        ui.profile_canvas = MagicMock()

        ui.clear_profile_options()

        self.assertEqual(ui.folder_media_paths, {"folder-A": "/media/a"})
        self.assertEqual(ui.folder_media_vars, {})


class ReelFlowHonoursTheFolderTests(unittest.TestCase):
    """End of the chain: the reel flow pushes a clip from the folder handed to it
    on the Profile, never from the global setting or another folder."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.folder_a = root / "model-a"
        self.folder_b = root / "model-b"
        self.global_folder = root / "global"
        for folder, name in (
            (self.folder_a, "a-clip.mp4"),
            (self.folder_b, "b-clip.mp4"),
            (self.global_folder, "global-clip.mp4"),
        ):
            folder.mkdir(parents=True)
            (folder / name).write_bytes(b"video")

    def _pushed_local_path(self, media_path):
        """Run the flow far enough to see which local file it tries to push, then
        let it bail out on the (mocked) failed push."""
        from adb_bot.automation.flows import instagram_reel
        from adb_bot.core.models import Profile

        profile = Profile(id="p-1", status="active", ip="127.0.0.1", port="5555", media_path=media_path)
        pushed: list[str] = []

        def fake_push(target, local, remote, logger=None):
            pushed.append(local)
            return False  # ends the run right after the media choice

        with patch.object(instagram_reel, "_adb_push_media_to_device", side_effect=fake_push), \
                patch.object(
                    instagram_reel, "_adb_resolve_story_media_path",
                    return_value=str(self.global_folder),
                ):
            instagram_reel.InstagramReelUploadFlow().run(profile, adb_client=MagicMock(), logger=MagicMock())
        return Path(pushed[0]) if pushed else None

    def test_flow_pushes_a_clip_from_the_mapped_folder(self):
        chosen = self._pushed_local_path(str(self.folder_a))
        self.assertEqual(chosen.parent, self.folder_a)
        self.assertEqual(chosen.name, "a-clip.mp4")

    def test_a_different_folder_gets_its_own_clip(self):
        chosen = self._pushed_local_path(str(self.folder_b))
        self.assertEqual(chosen.parent, self.folder_b)

    def test_without_a_mapping_the_global_setting_still_wins(self):
        chosen = self._pushed_local_path(None)
        self.assertEqual(chosen.parent, self.global_folder)


class TopUpTheMappedFolderTests(unittest.TestCase):
    """A folder is mapped once and topped up between runs, with the app left
    open -- the queue has to notice clips added after it was first built."""

    def setUp(self):
        from adb_bot.automation.flows.story_media import StoryMediaQueueManager

        self.root = Path(tempfile.mkdtemp())
        (self.root / "clip1.mp4").write_bytes(b"video")
        self.queue = StoryMediaQueueManager(self.root)

    def test_clips_added_later_are_picked_up(self):
        self.assertEqual(self.queue.get_next_media().name, "clip1.mp4")
        self.assertIsNone(self.queue.get_next_media())

        (self.root / "clip2.mp4").write_bytes(b"video")
        self.assertEqual(self.queue.get_next_media().name, "clip2.mp4")

    def test_a_rescan_never_hands_out_the_same_clip_twice(self):
        first = self.queue.get_next_media()
        # clip1 is still on disk (a failed run does not consume media), but it
        # must not come back a second time in this session.
        self.assertTrue(first.exists())
        self.assertIsNone(self.queue.get_next_media())

    def test_consumed_clips_do_not_come_back(self):
        self.queue.mark_used(self.queue.get_next_media())
        (self.root / "clip2.mp4").write_bytes(b"video")
        self.assertEqual(self.queue.get_next_media().name, "clip2.mp4")
        self.assertIsNone(self.queue.get_next_media())

    def test_a_deleted_folder_reports_empty_instead_of_raising(self):
        import shutil

        self.queue.get_next_media()
        shutil.rmtree(self.root)
        self.assertIsNone(self.queue.get_next_media())


class ReelFlowConstantTests(unittest.TestCase):
    def test_every_reel_option_in_the_ui_gets_the_folder_media(self):
        # The other direction (REEL_FLOWS ⊆ UI values) is deliberately not
        # asserted: REEL_FLOWS still lists the retired screen-dump flow so an
        # Airtable row naming it keeps getting its folder's media.
        from adb_bot.ui.helpers import get_available_flows

        offered_reel_flows = [
            flow["value"] for flow in get_available_flows() if "reel" in flow["value"]
        ]
        self.assertTrue(offered_reel_flows)
        for value in offered_reel_flows:
            self.assertIn(value, REEL_FLOWS)

    def test_the_lifecycle_planner_schedules_the_same_flows_the_ui_offers(self):
        from adb_bot.automation import lifecycle
        from adb_bot.ui.helpers import get_available_flows

        values = {flow["value"] for flow in get_available_flows()}
        for scheduled in (lifecycle.FLOW_WARMUP, lifecycle.FLOW_REEL,
                          lifecycle.FLOW_UPDATE_BIO, lifecycle.FLOW_UPDATE_PICTURE):
            self.assertIn(scheduled, values)


if __name__ == "__main__":
    unittest.main()
