import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from adb_bot.automation.flows.instagram import _adb_verify_remote_media_matches_local


class StoryUploadVerificationTest(TestCase):
    def test_returns_true_when_remote_hash_matches_local_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "story.jpg"
            local_path.write_bytes(b"sample-media-bytes")
            expected_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()

            with patch("adb_bot.automation.flows.instagram.subprocess.run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=0, stdout=f"{expected_hash}\n", stderr="")

                result = _adb_verify_remote_media_matches_local("device-1", str(local_path), "/sdcard/Download/story.jpg")

                self.assertTrue(result)
                mock_run.assert_called_once()

    def test_returns_false_when_remote_hash_differs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = Path(tmpdir) / "story.jpg"
            local_path.write_bytes(b"sample-media-bytes")

            with patch("adb_bot.automation.flows.instagram.subprocess.run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")

                result = _adb_verify_remote_media_matches_local("device-1", str(local_path), "/sdcard/Download/story.jpg")

                self.assertFalse(result)
                mock_run.assert_called_once()
