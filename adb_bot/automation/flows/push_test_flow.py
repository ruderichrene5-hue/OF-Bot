from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from adb_bot.core.models import Profile
from adb_bot.automation.flows.instagram import (
    _adb_resolve_story_media_path,
    _adb_push_media_to_device,
    _adb_verify_remote_media_exists,
)


class PushMediaTestFlow:
    """Simple flow to push a configured local file to the device for manual inspection.

    - resolves the local file via existing settings/candidate lookup
    - pushes to /sdcard/Download/<filename>
    - verifies the file exists on device
    - leaves the profile/device on screen for manual verification
    """

    name = "push_media_test"
    remote_directory = "/sdcard/Download"

    def _build_remote_media_path(self, local_media_path: str) -> str:
        file_name = Path(local_media_path).name
        safe_name = file_name.replace(" ", "_")
        return f"{self.remote_directory}/{safe_name}"

    def run(
        self,
        profile: Profile,
        adb_client=None,
        logger=None,
        should_stop=None,
        status_callback=None,
        manual_continue_event=None,
        manual_continue_callback=None,
    ) -> dict[str, Any]:
        if not adb_client:
            raise ValueError("adb_client is required")
        if not profile.target:
            raise ValueError("Profile target is missing")

        log = logger if logger is not None else None
        target = profile.target

        def emit(level: str, message: str, *args) -> None:
            target_logger = log if log is not None else print
            method = getattr(target_logger, level, None)
            if callable(method):
                method(message, *args)
            else:
                if args:
                    message = message % args
                print(message)

        emit("info", "Starting push-media-test flow for profile %s", profile.id)

        # Resolve the local media path using the same helpers as story upload
        media_path = _adb_resolve_story_media_path(logger=log)
        if media_path is None:
            emit("warning", "No local media file found to push for profile %s", profile.id)
            return {"profile_id": profile.id, "target": target, "pushed": False}

        remote_media_path = self._build_remote_media_path(media_path)
        emit("info", "Preparing to push media for profile %s: %s -> %s", profile.id, media_path, remote_media_path)

        pushed = _adb_push_media_to_device(target, media_path, remote_media_path, logger=log)
        if not pushed:
            emit("warning", "adb push failed for profile %s on target %s", profile.id, target)
            return {"profile_id": profile.id, "target": target, "pushed": False}

        # Verify the pushed media exists; allow a single retry
        max_attempts = 2
        attempt = 1
        while attempt <= max_attempts:
            if _adb_verify_remote_media_exists(target, remote_media_path, logger=log):
                emit("info", "adb push succeeded and verified for profile %s on target %s", profile.id, target)
                break
            if attempt >= max_attempts:
                emit("warning", "Failed to verify pushed media on device after %s attempts for %s", max_attempts, target)
                return {"profile_id": profile.id, "target": target, "pushed": False}
            emit("info", "Remote media not found; retrying push (%s/%s) for %s", attempt + 1, max_attempts, target)
            _adb_push_media_to_device(target, media_path, remote_media_path, logger=log)
            attempt += 1

        # Leave the device/profile open for manual inspection
        emit("info", "Push-media-test completed; leaving profile %s open for manual check", profile.id)
        return {"profile_id": profile.id, "target": target, "pushed": True}
