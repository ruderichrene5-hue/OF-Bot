"""Today's-date-folder content picker for GeeLark's Active_Posting cycle.

Confirmed structure 2026-08-30: GeeLark_Raw_Videos/<Model>/<YYYY-MM-DD>/*.mp4,
independent of MLX's 01_Raw_Videos. Only today's date folder is ever read --
no fallback to older content, by design (forgetting to upload must mean no
post, not a silent repeat).
"""

import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from adb_bot.automation import geelark_content as content


class FakeDriveClient:
    def __init__(self, tree, files_by_folder=None, downloads=None):
        # tree: {folder_id: [{'id':..,'name':..}, ...]} for list_subfolders
        self.tree = tree
        self.files_by_folder = files_by_folder or {}
        self.download_calls = []
        self._downloads = downloads or {}

    def list_subfolders(self, parent_id):
        return list(self.tree.get(parent_id, []))

    def list_files(self, parent_id):
        return list(self.files_by_folder.get(parent_id, []))

    def download(self, file_id, dest_path):
        self.download_calls.append((file_id, dest_path))
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(dest_path).write_bytes(b"fake video bytes")
        return dest_path


ROOT = "root-id"


def _tree_for(model_name, date_str, files):
    model_id = "model-id"
    date_id = "date-id"
    return (
        {ROOT: [{"id": model_id, "name": model_name}],
         model_id: [{"id": date_id, "name": date_str}]},
        {date_id: files},
    )


class TodayRawVideosTest(unittest.TestCase):
    def test_finds_videos_in_todays_date_folder(self):
        tree, files = _tree_for("Luisa", "2026-08-30",
                                [{"id": "v1", "name": "clip.mp4", "link": None}])
        client = FakeDriveClient(tree, files)
        result = content.today_raw_videos("Luisa", client=client,
                                          root_folder_id=ROOT,
                                          today=date(2026, 8, 30))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "clip.mp4")
        self.assertEqual(result[0].model, "Luisa")

    def test_missing_model_folder_is_empty_not_an_error(self):
        client = FakeDriveClient({ROOT: []})
        result = content.today_raw_videos("Nonexistent", client=client,
                                          root_folder_id=ROOT)
        self.assertEqual(result, [])

    def test_missing_todays_date_folder_is_empty(self):
        """The core safety rule: no upload today -> nothing, not yesterday's
        leftovers."""
        model_id = "model-id"
        client = FakeDriveClient({
            ROOT: [{"id": model_id, "name": "Luisa"}],
            model_id: [{"id": "old-date", "name": "2026-08-25"}],
        })
        result = content.today_raw_videos("Luisa", client=client,
                                          root_folder_id=ROOT,
                                          today=date(2026, 8, 30))
        self.assertEqual(result, [])

    def test_non_video_files_are_ignored(self):
        tree, files = _tree_for("Luisa", "2026-08-30", [
            {"id": "v1", "name": "clip.mp4", "link": None},
            {"id": "v2", "name": "notes.txt", "link": None},
        ])
        client = FakeDriveClient(tree, files)
        result = content.today_raw_videos("Luisa", client=client,
                                          root_folder_id=ROOT,
                                          today=date(2026, 8, 30))
        self.assertEqual([v.name for v in result], ["clip.mp4"])

    def test_model_name_matching_is_case_insensitive(self):
        tree, files = _tree_for("Luisa", "2026-08-30",
                                [{"id": "v1", "name": "clip.mp4", "link": None}])
        client = FakeDriveClient(tree, files)
        result = content.today_raw_videos("luisa", client=client,
                                          root_folder_id=ROOT,
                                          today=date(2026, 8, 30))
        self.assertEqual(len(result), 1)

    def test_geelark_new_suffix_matches_the_plain_drive_folder(self):
        """Confirmed live 2026-08-30: the Geelark group is 'Nikki Geelark
        NEW' (35 phones) but the raw-content folder is just 'Nikki' like
        every other model -- must match without renaming either side."""
        tree, files = _tree_for("Nikki", "2026-08-30",
                                [{"id": "v1", "name": "clip.mp4", "link": None}])
        client = FakeDriveClient(tree, files)
        result = content.today_raw_videos("Nikki Geelark NEW", client=client,
                                          root_folder_id=ROOT,
                                          today=date(2026, 8, 30))
        self.assertEqual(len(result), 1)

    def test_a_genuinely_different_model_still_does_not_match(self):
        """The suffix-stripping must not turn into a fuzzy match that
        confuses two different models' content."""
        tree, files = _tree_for("Nikki", "2026-08-30",
                                [{"id": "v1", "name": "clip.mp4", "link": None}])
        client = FakeDriveClient(tree, files)
        result = content.today_raw_videos("Nicole Geelark NEW", client=client,
                                          root_folder_id=ROOT,
                                          today=date(2026, 8, 30))
        self.assertEqual(result, [])


class SpoofForHandleTest(unittest.TestCase):
    def _video(self):
        return content.RawVideo(model="Luisa", name="clip.mp4", path="",
                                source_id="v1")

    def test_returns_none_on_download_failure(self):
        client = FakeDriveClient({})
        client.download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        result = content.spoof_for_handle(self._video(), "handle1", client,
                                          "python3", "/spoofer")
        self.assertIsNone(result)

    def test_returns_none_when_spoofer_produces_nothing(self):
        client = FakeDriveClient({})
        with patch.object(content, "build_cli_spoofer",
                         return_value=lambda *a, **k: None):
            result = content.spoof_for_handle(self._video(), "handle1", client,
                                              "python3", "/spoofer")
        self.assertIsNone(result)

    def test_downloaded_raw_file_is_cleaned_up_either_way(self):
        client = FakeDriveClient({})
        seen_paths = []

        def fake_spoof_fn(raw_path, out_dir, seed, logger=None):
            seen_paths.append(raw_path)
            return None

        with patch.object(content, "build_cli_spoofer", return_value=fake_spoof_fn):
            content.spoof_for_handle(self._video(), "handle1", client,
                                     "python3", "/spoofer")
        self.assertTrue(seen_paths)
        self.assertFalse(Path(seen_paths[0]).exists())

    def test_success_returns_the_finalized_variant_path(self):
        client = FakeDriveClient({})

        def fake_spoof_fn(raw_path, out_dir, seed, logger=None):
            produced = Path(out_dir) / "clip.mp4"
            produced.parent.mkdir(parents=True, exist_ok=True)
            produced.write_bytes(b"spoofed")
            return produced

        with patch.object(content, "build_cli_spoofer", return_value=fake_spoof_fn):
            result = content.spoof_for_handle(self._video(), "handle1", client,
                                              "python3", "/spoofer")
        self.assertIsNotNone(result)
        self.assertIn("handle1", result.path)
        self.assertEqual(result.handle, "handle1")


class GetPostMediaTest(unittest.TestCase):
    def test_no_content_today_returns_none(self):
        client = FakeDriveClient({ROOT: []})
        with patch.object(content, "_env", side_effect=lambda n: "x"):
            result = content.get_post_media("Luisa", "handle1", client=client,
                                            root_folder_id=ROOT)
        self.assertIsNone(result)

    def test_picks_a_video_and_spoofs_for_the_handle(self):
        tree, files = _tree_for("Luisa", "2026-08-30",
                                [{"id": "v1", "name": "clip.mp4", "link": None}])
        client = FakeDriveClient(tree, files)

        def fake_spoof_fn(raw_path, out_dir, seed, logger=None):
            produced = Path(out_dir) / "clip.mp4"
            produced.parent.mkdir(parents=True, exist_ok=True)
            produced.write_bytes(b"spoofed")
            return produced

        with patch.object(content, "build_cli_spoofer", return_value=fake_spoof_fn), \
             patch.object(content, "datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2026, 8, 30)
            result = content.get_post_media(
                "Luisa", "handle1", client=client, root_folder_id=ROOT,
                spoofer_python="python3", spoofer_root="/spoofer")
        self.assertIsNotNone(result)
        self.assertEqual(result.raw_video.name, "clip.mp4")


if __name__ == "__main__":
    unittest.main()
