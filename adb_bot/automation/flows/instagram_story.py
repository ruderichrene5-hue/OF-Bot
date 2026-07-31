import time
from pathlib import Path

from adb_bot.core.models import Profile

from . import instagram as instagram_module

_emit = instagram_module._emit
_sleep_after_instagram_launch = instagram_module._sleep_after_instagram_launch
_adb_resolve_story_media_path = instagram_module._adb_resolve_story_media_path
_adb_push_media_to_device = instagram_module._adb_push_media_to_device
_adb_verify_remote_media_exists = instagram_module._adb_verify_remote_media_exists
_adb_verify_remote_media_matches_local = instagram_module._adb_verify_remote_media_matches_local
_adb_capture_ui_dump = instagram_module._adb_capture_ui_dump
_adb_find_instagram_story_action_center = instagram_module._adb_find_instagram_story_action_center
_adb_find_instagram_story_row_plus_center = instagram_module._adb_find_instagram_story_row_plus_center
_adb_find_instagram_media_selection_center = instagram_module._adb_find_instagram_media_selection_center
_adb_find_instagram_next_center = instagram_module._adb_find_instagram_next_center
_adb_find_instagram_share_center = instagram_module._adb_find_instagram_share_center
_adb_find_instagram_home_button_center = instagram_module._adb_find_instagram_home_button_center
_adb_find_instagram_dialog_action_center = instagram_module._adb_find_instagram_dialog_action_center
_adb_is_instagram_story_composer_visible = instagram_module._adb_is_instagram_story_composer_visible
_adb_wait_for_instagram_story_composer = instagram_module._adb_wait_for_instagram_story_composer
_adb_ensure_instagram_feed_visible = instagram_module._adb_ensure_instagram_feed_visible
_adb_get_relative_point = instagram_module._adb_get_relative_point
_adb_tap = instagram_module._adb_tap
_adb_find_ui_element_center = instagram_module._adb_find_ui_element_center
home = instagram_module.home
write_text = instagram_module.write_text
get_story_media_queue = instagram_module.get_story_media_queue
pytesseract = instagram_module.pytesseract
Image = instagram_module.Image
cv2 = instagram_module.cv2
subprocess = instagram_module.subprocess
_run_hidden = instagram_module._run_hidden
_adb_run = instagram_module._adb_run
tempfile = instagram_module.tempfile


class InstagramStoryUploadFlow:
    name = "instagram_story_upload"
    remote_directory = "/sdcard/Download"

    def get_progress_total_steps(self, target: str) -> int:
        return 7

    def _build_remote_media_path(self, local_media_path: str) -> str:
        file_name = Path(local_media_path).name
        safe_name = file_name.replace(' ', '_')
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
    ):
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

        def check_abort() -> bool:
            if callable(should_stop) and should_stop():
                emit("info", "Abort requested during Instagram story upload flow for profile %s", profile.id)
                return True
            return False

        emit("info", "Starting Instagram story upload flow for profile %s", profile.id)

        media_path = _adb_resolve_story_media_path(logger=log)
        if media_path is None:
            emit("warning", "No story upload image found for profile %s", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        media_source = Path(media_path)
        selected_media = None
        media_queue = None
        if media_source.is_dir():
            media_queue = get_story_media_queue(media_source, logger=log)
            selected_media = media_queue.get_next_media()
            if selected_media is None:
                emit("warning", "No pending story media available for profile %s from folder %s", profile.id, media_source)
                return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
            media_path = str(selected_media)
            emit("info", "Assigned story media %s to profile %s from folder %s", media_path, profile.id, media_source)
        else:
            selected_media = media_source

        remote_media_path = self._build_remote_media_path(media_path)
        emit("info", "Preparing to push story media for profile %s: %s -> %s", profile.id, media_path, remote_media_path)
        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()
        pushed = _adb_push_media_to_device(target, media_path, remote_media_path, logger=log)
        if not pushed:
            emit("warning", "adb push failed for profile %s on target %s", profile.id, target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
        emit("info", "adb push succeeded for profile %s on target %s", profile.id, target)

        max_push_attempts = 3
        attempt = 1
        while attempt <= max_push_attempts:
            remote_exists = _adb_verify_remote_media_exists(target, remote_media_path, logger=log)
            if remote_exists and _adb_verify_remote_media_matches_local(target, media_path, remote_media_path, logger=log):
                emit("info", "Verified pushed story media content on device for %s", target)
                break
            if attempt >= max_push_attempts:
                emit("warning", "Failed to verify pushed story media on device after %s attempts for %s", max_push_attempts, target)
                return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
            emit("info", "Remote media verification failed or content mismatch; retrying push (%s/%s) for %s", attempt + 1, max_push_attempts, target)
            if not _adb_push_media_to_device(target, media_path, remote_media_path, logger=log):
                emit("warning", "adb push failed on retry %s for %s", attempt + 1, target)
            attempt += 1

        # Pushing a file to the phone is not the same as posting it: the media is
        # only marked used once the story actually goes out, so a failed run
        # leaves the clip in the queue for the next attempt.
        def commit_media_used() -> None:
            if media_source.is_dir() and selected_media is not None and media_queue is not None:
                moved = media_queue.mark_used(selected_media)
                emit("info", "Marked story media %s as used for profile %s: %s", selected_media, profile.id, moved)

        def keep_media_for_retry(reason: str) -> None:
            if media_source.is_dir() and selected_media is not None:
                emit("info", "Leaving story media %s in the queue for a retry (%s)", selected_media, reason)

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            if "monkey" in command or "am start" in command:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(5)
            else:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(2)

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        _sleep_after_instagram_launch(target, logger=log, delay_seconds=10)
        self._last_next_center = None
        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=log, max_attempts=5, retry_delay_seconds=5):
            emit("warning", "Instagram feed verification did not confirm home feed for %s; continuing to story composer", target)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()
        emit("info", "Opening story composer for %s", target)
        try:
            opened = self._open_story_composer(target, adb_client, logger=log)
        except Exception as exc:
            opened = False
            emit("warning", "Exception when opening story composer for %s: %s", target, exc)
        if not opened:
            emit("warning", "Unable to reliably open the Instagram story composer for %s", target)

        if not _adb_wait_for_instagram_story_composer(target, logger=log):
            emit("warning", "Story composer did not appear after opening attempt for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        time.sleep(2)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        emit("info", "Selecting story media for %s", target)
        try:
            selected_ok = self._select_story_media(target, adb_client, logger=log)
        except Exception as exc:
            selected_ok = False
            emit("warning", "Exception when selecting story media for %s: %s", target, exc)
        if not selected_ok:
            emit("warning", "Unable to reliably select story media for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        time.sleep(5)

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        try:
            posted_via_your_story = False
            if self._tap_your_story(target, adb_client, logger=log):
                emit("info", "Tapped 'Your story' for %s; waiting for post confirmation", target)
                time.sleep(3)
                posted_via_your_story = self._verify_story_post_completed(target, adb_client, logger=log)
                if posted_via_your_story:
                    emit("info", "Confirmed story post for %s via 'Your story' path", target)
                else:
                    emit("warning", "Story post was not confirmed for %s after tapping 'Your story'", target)
                    try:
                        root = _adb_capture_ui_dump(target, logger=log)
                        if root is None:
                            emit("warning", "UI dump returned no data for %s when verification failed", target)
                        else:
                            node_count = sum(1 for _ in root.iter())
                            emit("info", "UI dump node count for %s after failed verify: %s", target, node_count)
                    except Exception as exc:
                        emit("warning", "Failed to capture UI dump for %s: %s", target, exc)
            else:
                emit("warning", "Unable to locate an explicit 'Your story' target for %s; aborting upload", target)
        except Exception:
            posted_via_your_story = False

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        if not posted_via_your_story:
            emit("warning", "Instagram story upload did not complete successfully for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        home_command = _adb_find_instagram_home_button_center(target, logger=log)
        if home_command is not None:
            _adb_tap(target, home_command[0], home_command[1], adb_client, logger=log, description="Tapping Instagram home button")
        else:
            adb_client.run_command(f"adb -s {target} shell {home()}")

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        if not posted_via_your_story:
            keep_media_for_retry("story post not confirmed")
            emit("warning", "Instagram story upload did not complete successfully for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        commit_media_used()
        emit("info", "Instagram story upload flow completed")
        return {"profile_id": profile.id, "target": target, "aborted": False, "success": True}

    def build_launch_commands(self, target: str) -> list[str]:
        return [
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    def _verify_story_post_completed(self, target: str, adb_client, logger=None, max_attempts: int = 3, delay_seconds: int = 3) -> bool:
        log = logger if logger is not None else None

        def emit(level: str, message: str, *args) -> None:
            target_logger = log if log is not None else print
            method = getattr(target_logger, level, None)
            if callable(method):
                method(message, *args)
            else:
                if args:
                    message = message % args
                print(message)

        for attempt in range(1, max_attempts + 1):
            if _adb_ensure_instagram_feed_visible(target, adb_client, logger=log):
                emit("info", "Confirmed story post for %s by detecting Instagram home feed after attempt %s/%s", target, attempt, max_attempts)
                return True
            if attempt < max_attempts:
                emit("info", "Story post confirmation not yet visible for %s; waiting %s seconds before retry %s/%s", target, delay_seconds, attempt + 1, max_attempts)
                time.sleep(delay_seconds)

        emit("warning", "Story post could not be confirmed for %s after %s checks", target, max_attempts)
        return False

    def _open_story_composer(self, target: str, adb_client, logger=None) -> bool:
        # Prefer the story-row '+' (black background) — check UI twice to be robust to transient UI state.
        for attempt in range(2):
            story_row_plus_center = _adb_find_instagram_story_row_plus_center(target, logger=logger)
            if story_row_plus_center is not None:
                _adb_tap(target, story_row_plus_center[0], story_row_plus_center[1], adb_client, logger=logger, description="Tapping story row plus")
                time.sleep(1)
                return True
            time.sleep(0.25)

        # If the '+' in the story row is not found, fall back to tapping the story action (smaller target).
        story_center = _adb_find_instagram_story_action_center(target, logger=logger)
        if story_center is not None:
            _adb_tap(target, story_center[0], story_center[1], adb_client, logger=logger, description="Tapping story action")
            time.sleep(1)
            return True

        # Do not use generic top-left/top-right fallbacks here — they interfere with reliable composer opening.
        if logger is not None:
            _emit(logger, "warning", "Cannot open story composer: story row plus or story action not detected on %s", target)
        return False

    def _select_story_media(self, target: str, adb_client, logger=None) -> bool:
        # Primary: detect the actual gallery/thumbnail element from a live UI dump.
        gallery_center = _adb_find_instagram_story_gallery_center(target, logger=logger)
        if gallery_center is not None:
            _emit(logger, "info", "Selecting story gallery target at %s,%s for %s", gallery_center[0], gallery_center[1], target)
            _adb_tap(target, gallery_center[0], gallery_center[1], adb_client, logger=logger, description="Tapping story gallery selection")
            return True

        media_thumb = _adb_find_instagram_media_selection_center(target, logger=logger)
        if media_thumb is not None:
            _emit(logger, "info", "Selecting media target at %s,%s for %s", media_thumb[0], media_thumb[1], target)
            _adb_tap(target, media_thumb[0], media_thumb[1], adb_client, logger=logger, description="Tapping story media target")
            return True

        # Fallback: if neither detector found anything but the composer is visible,
        # tap center X and ~30% Y to select the most recent/last media item.
        if _adb_is_instagram_story_composer_visible(target, logger=logger):
            fallback_media = _adb_get_relative_point(target, 0.50, 0.30, logger=logger)
            _emit(logger, "info", "Selecting media via center X,30%% Y at %s,%s for %s", fallback_media[0], fallback_media[1], target)
            _adb_tap(target, fallback_media[0], fallback_media[1], adb_client, logger=logger, description="Tapping primary story media (center X, 30% Y)")
            return True

        _emit(logger, "warning", "Unable to locate a story media thumbnail, gallery button, or use primary center30% for %s", target)
        return False

    def _tap_your_story(self, target: str, adb_client, logger=None) -> bool:
        log = logger if logger is not None else None

        def emit(level: str, message: str, *args) -> None:
            target_logger = log if log is not None else print
            method = getattr(target_logger, level, None)
            if callable(method):
                method(message, *args)
            else:
                if args:
                    message = message % args
                print(message)

        ui_center = _adb_find_ui_element_center(target, ("your story", "yourstory", "your story button"), logger=log)
        if ui_center is not None:
            x, y = ui_center
            emit("info", "Tapping 'Your story' via UI dump at %s,%s for %s", x, y, target)
            _adb_tap(target, x, y, adb_client, logger=log, description="Tapping Your story via UI dump")
            return True

        if pytesseract is not None and Image is not None and cv2 is not None:
            try:
                tmp_remote = "/sdcard/instagram_your_story.png"
                tmp_local = Path(tempfile.gettempdir()) / f"instagram_your_story_{target}.png"
                _adb_run("-s", target, "shell", f"screencap -p {tmp_remote}", check=True)
                _adb_run("-s", target, "pull", tmp_remote, tmp_local, check=True)
                img = cv2.imread(str(tmp_local))
                if img is not None:
                    h, w = img.shape[:2]
                    x0 = int(w * 0.55)
                    y0 = int(h * 0.65)
                    crop = img[y0:h, x0:w]
                    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                    _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
                    data = pytesseract.image_to_data(Image.fromarray(gray), output_type=pytesseract.Output.DICT)
                    for i, text in enumerate(data.get("text", [])):
                        if not text:
                            continue
                        if "story" in text.lower() or "your story" in text.lower():
                            x_rel = int(data["left"][i] + data["width"][i] / 2)
                            y_rel = int(data["top"][i] + data["height"][i] / 2)
                            x_abs = x0 + x_rel
                            y_abs = y0 + y_rel
                            emit("info", "Tapping 'Your story' via OCR at %s,%s for %s", x_abs, y_abs, target)
                            _adb_tap(target, x_abs, y_abs, adb_client, logger=log, description="Tapping Your story via OCR")
                            return True
            except Exception as exc:
                emit("info", "OCR attempt for 'Your story' failed for %s: %s", target, exc)

        emit("warning", "Unable to locate an explicit 'Your story' target for %s; skipping blind tap", target)
        return False

    def _tap_next(self, target: str, adb_client, logger=None) -> bool:
        next_center = _adb_find_instagram_next_center(target, logger=logger)
        previous_center = getattr(self, "_last_next_center", None)

        if next_center is not None:
            if previous_center is not None:
                delta_x = abs(next_center[0] - previous_center[0])
                delta_y = abs(next_center[1] - previous_center[1])
                if delta_x + delta_y > 300:
                    _emit(
                        logger,
                        "info",
                        "Detected a very different Next location %s from previous %s for %s; reusing previous location",
                        next_center,
                        previous_center,
                        target,
                    )
                    _adb_tap(target, previous_center[0], previous_center[1], adb_client, logger=logger, description="Re-tapping previous Next")
                    self._last_next_center = None
                    return True
            self._last_next_center = next_center
            _adb_tap(target, next_center[0], next_center[1], adb_client, logger=logger, description="Tapping Next")
            return True

        if previous_center is not None:
            _emit(logger, "info", "Reusing previous Next button location at %s for %s", previous_center, target)
            _adb_tap(target, previous_center[0], previous_center[1], adb_client, logger=logger, description="Re-tapping previous Next")
            self._last_next_center = None
            return True

        dialog_center = _adb_find_instagram_dialog_action_center(target, logger=logger)
        if dialog_center is not None:
            self._last_next_center = dialog_center
            _adb_tap(target, dialog_center[0], dialog_center[1], adb_client, logger=logger, description="Tapping dialog action")
            return True

        if (
            _adb_is_instagram_story_composer_visible(target, logger=logger)
            or instagram_module._adb_wait_for_instagram_story_share_screen(target, logger=logger)
        ):
            fallback_next = _adb_get_relative_point(target, 0.90, 0.92, logger=logger)
            if logger is not None:
                _emit(logger, "info", "Tapping fallback Next at %s,%s for %s", fallback_next[0], fallback_next[1], target)
            self._last_next_center = fallback_next
            _adb_tap(target, fallback_next[0], fallback_next[1], adb_client, logger=logger, description="Tapping fallback Next")
            return True

        if logger is not None:
            _emit(logger, "warning", "Skip tapping Next because story compose/share screen is not detected on %s", target)
        return False

    def _tap_share_story(self, target: str, adb_client, logger=None) -> bool:
        share_center = _adb_find_instagram_share_center(target, logger=logger)
        if share_center is not None:
            _adb_tap(target, share_center[0], share_center[1], adb_client, logger=logger, description="Tapping Share")
            return True

        if logger is not None:
            _emit(logger, "warning", "Share button not detected for %s; skipping tap", target)
        return False


def _adb_find_instagram_story_gallery_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    screen = instagram_module._adb_get_screen_size(target, logger=logger)
    if screen is None:
        return None

    width, height = screen
    candidates = []
    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if not any(keyword in combined for keyword in ("gallery", "recent", "photos", "camera roll")):
            continue

        bounds = attrs.get("bounds", "")
        match = __import__("re").search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        if cy < int(height * 0.40) or cy > int(height * 0.85):
            continue
        if cx < int(width * 0.10) or cx > int(width * 0.90):
            continue
        candidates.append((abs(cy - int(height * 0.60)), cx, cy))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _, cx, cy = candidates[0]
    return cx, cy
