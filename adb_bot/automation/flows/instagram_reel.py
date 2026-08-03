import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from adb_bot.core.models import Profile
from adb_bot.automation import post_ledger

from . import instagram as instagram_module
from . import reel_verify
from . import waits

# Reuse the single optional uiautomator2 import from the instagram module so
# both flows share the same backend (None when uiautomator2 isn't installed).
u2 = getattr(instagram_module, "u2", None)

_emit = instagram_module._emit
_u2_describe = instagram_module._u2_describe
_u2_find = instagram_module._u2_find
_u2_click = instagram_module._u2_click
_sleep_after_instagram_launch = instagram_module._sleep_after_instagram_launch
_adb_resolve_story_media_path = instagram_module._adb_resolve_story_media_path
_adb_push_media_to_device = instagram_module._adb_push_media_to_device
_adb_find_instagram_reel_create_center = instagram_module._adb_find_instagram_reel_create_center
_adb_find_instagram_start_new_video_center = instagram_module._adb_find_instagram_start_new_video_center
_adb_wait_for_instagram_reel_composer = instagram_module._adb_wait_for_instagram_reel_composer
_adb_find_instagram_reel_option_center = instagram_module._adb_find_instagram_reel_option_center
_adb_find_instagram_reel_media_thumbnail_center = instagram_module._adb_find_instagram_reel_media_thumbnail_center
_adb_find_instagram_media_selection_center = instagram_module._adb_find_instagram_media_selection_center
_adb_find_instagram_share_center = instagram_module._adb_find_instagram_share_center
_adb_find_instagram_home_button_center = instagram_module._adb_find_instagram_home_button_center
_adb_get_relative_point = instagram_module._adb_get_relative_point
_adb_tap = instagram_module._adb_tap
_adb_find_instagram_dialog_action_center = instagram_module._adb_find_instagram_dialog_action_center
_adb_wait_for_instagram_story_share_screen = instagram_module._adb_wait_for_instagram_story_share_screen
_adb_ensure_instagram_feed_visible = instagram_module._adb_ensure_instagram_feed_visible
_adb_wait_for_instagram_story_composer = instagram_module._adb_wait_for_instagram_story_composer
_adb_is_instagram_story_composer_visible = instagram_module._adb_is_instagram_story_composer_visible
_adb_is_instagram_reel_composer_visible = instagram_module._adb_is_instagram_reel_composer_visible
home = instagram_module.home
write_text = instagram_module.write_text
get_story_media_queue = instagram_module.get_story_media_queue


class InstagramReelUploadFlow:
    name = "instagram_reel_upload"

    def get_progress_total_steps(self, target: str) -> int:
        return 7

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
                emit("info", "Abort requested during Instagram reel upload flow for profile %s", profile.id)
                return True
            return False

        emit("info", "Starting Instagram reel upload flow for profile %s", profile.id)

        # A per-run video (the Posting Queue's Spoof Variant) wins over the global
        # media setting/folder used for manual/warmup reel runs.
        media_path = getattr(profile, "media_path", None) or _adb_resolve_story_media_path(logger=log)
        if media_path is None:
            emit("warning", "No reel upload media found for profile %s", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        media_source = Path(media_path)
        selected_media = None
        media_queue = None
        if media_source.is_dir():
            media_queue = get_story_media_queue(media_source, logger=log)
            selected_media = media_queue.get_next_media()
            if selected_media is None:
                emit("warning", "No pending reel media available for profile %s from folder %s", profile.id, media_source)
                return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
            media_path = str(selected_media)
            emit("info", "Assigned reel media %s to profile %s from folder %s", media_path, profile.id, media_source)
        else:
            selected_media = media_source

        remote_media_path = self._build_remote_media_path(media_path)
        emit("info", "Preparing to push reel media for profile %s: %s -> %s", profile.id, media_path, remote_media_path)
        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()
        pushed = _adb_push_media_to_device(target, media_path, remote_media_path, logger=log)
        if not pushed:
            emit("warning", "adb push failed for profile %s on target %s", profile.id, target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
        emit("info", "adb push succeeded for profile %s on target %s", profile.id, target)
        # Push + media scan only: the on-device file matched the local one on
        # every run we checked, so the ls + sha256sum verification (and its
        # 3x re-push loop) was removed. The trade-off is that a push which
        # reports success over a half-dead adb tunnel is no longer caught here
        # -- it surfaces later, as the picker not finding the clip.

        # NOTE: the media is deliberately NOT marked used here. Pushing a file to
        # the phone is not the same as posting it -- if the flow fails later the
        # clip must stay in the queue so the next run can retry it, rather than
        # being silently consumed. `commit_media_used` is called only once the
        # post is confirmed.
        def commit_media_used() -> None:
            if media_source.is_dir() and selected_media is not None and media_queue is not None:
                moved = media_queue.mark_used(selected_media)
                emit("info", "Marked reel media %s as used for profile %s: %s", selected_media, profile.id, moved)

        def keep_media_for_retry(reason: str) -> None:
            if media_source.is_dir() and selected_media is not None:
                emit("info", "Leaving reel media %s in the queue for a retry (%s)", selected_media, reason)

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
        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=log, max_attempts=5, retry_delay_seconds=5):
            emit("warning", "Instagram feed verification did not confirm home feed for %s; continuing to reel composer", target)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        emit("info", "Opening reel composer for %s", target)
        try:
            opened = self._open_reel_composer(target, adb_client, logger=log)
        except Exception as exc:
            opened = False
            emit("warning", "Exception when opening reel composer for %s: %s", target, exc)
        if not opened:
            emit("warning", "Unable to reliably open the Instagram reel composer for %s (leaving Instagram open)", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False, "failed": True}

        if not _adb_wait_for_instagram_story_composer(target, logger=log):
            emit("warning", "Reel composer did not appear after opening attempt for %s (leaving Instagram open)", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False, "failed": True}

        time.sleep(2)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        emit("info", "Selecting reel media for %s", target)
        try:
            selected_ok = self._select_story_media(target, adb_client, logger=log, prefer_reel_text=True)
        except Exception as exc:
            selected_ok = False
            emit("warning", "Exception when selecting reel media for %s: %s", target, exc)
        if not selected_ok:
            emit("warning", "Unable to reliably select reel media for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        time.sleep(3)
        if self._tap_next(target, adb_client, logger=log):
            emit("info", "Tapped first Next after selecting reel media for %s", target)
            time.sleep(3)
            if profile.caption:
                entered_caption = self._enter_reel_caption(target, adb_client, profile.caption, logger=log)
                if entered_caption:
                    emit("info", "Entered reel caption for %s", target)
                    time.sleep(1)
                    adb_client.run_command(f"adb -s {target} shell input keyevent 111")
                    time.sleep(1)
            if self._tap_next(target, adb_client, logger=log):
                emit("info", "Tapped second Next after selecting reel media for %s", target)
                time.sleep(3)

        share_tapped = False
        emit("info", "Attempting final Share tap after reel second Next for %s", target)
        if self._tap_share_story(target, adb_client, logger=log):
            emit("info", "Tapped Share after reel compose for %s", target)
            share_tapped = True
        else:
            emit("warning", "Share button was not detected after reel compose for %s; skipping tap", target)

        if not share_tapped:
            emit("warning", "Instagram reel upload did not complete (Share not tapped) for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        # Keep Instagram open on the feed (tap IG's own home icon, NOT the
        # Android home key) so the post-confirmation banner stays visible while
        # we wait.
        home_command = _adb_find_instagram_home_button_center(target, logger=log)
        if home_command is not None:
            _adb_tap(target, home_command[0], home_command[1], adb_client, logger=log, description="Tapping Instagram home button")

        # Success is only declared once the phone itself proves the reel landed.
        # The banner alone is unreliable, so this also fails fast on an error /
        # draft prompt, keeps waiting while an upload notification is ongoing,
        # and never gives up before the 3-minute floor. (Post-count verification
        # is u2-only -- this legacy path has no reliable profile-tab selector.)
        verdict = reel_verify.verify_reel_posted(
            get_screen_text=_make_dump_screen_text_probe(target, logger=log),
            get_notification_state=_make_notification_probe(target, adb_client, logger=log),
            should_stop=should_stop,
            emit=emit,
        )
        post_confirmed = verdict.confirmed

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        if callable(should_stop) and should_stop():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        if not post_confirmed:
            keep_media_for_retry("post not confirmed")
            emit("warning", "Instagram reel upload for %s: %s -- leaving Instagram open "
                            "in case the upload is still finishing", target, verdict.summary())
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False,
                    "post_confirmed": False, "verify_method": verdict.method, "verify_detail": verdict.detail}

        commit_media_used()
        emit("info", "Instagram reel upload for %s: %s", target, verdict.summary())
        return {"profile_id": profile.id, "target": target, "aborted": False, "success": True,
                "post_confirmed": True, "verify_method": verdict.method, "verify_detail": verdict.detail}

    def build_launch_commands(self, target: str) -> list[str]:
        return [
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    def _build_remote_media_path(self, local_media_path: str) -> str:
        return f"/sdcard/Download/{Path(local_media_path).name.replace(' ', '_')}"

    def _open_reel_composer(self, target: str, adb_client, logger=None) -> bool:
        plus_center = _adb_find_instagram_reel_create_center(target, logger=logger)
        if plus_center is not None:
            _adb_tap(target, plus_center[0], plus_center[1], adb_client, logger=logger, description="Tapping plus center")
            time.sleep(3)
        else:
            fallback_plus = _adb_get_relative_point(target, 0.08, 0.08, logger=logger)
            _adb_tap(target, fallback_plus[0], fallback_plus[1], adb_client, logger=logger, description="Tapping fallback plus")
            time.sleep(3)

        start_new_video_center = _adb_find_instagram_start_new_video_center(target, logger=logger)
        if start_new_video_center is not None:
            screen_size = instagram_module._adb_get_screen_size(target, logger=logger) or (1080, 2340)
            width, height = screen_size
            delta_y = max(80, int(height * 0.08))
            for attempt in range(1, 4):
                x, y = start_new_video_center
                if attempt == 1:
                    tap_x = x
                    tap_y = y
                elif attempt == 2:
                    tap_x = x
                    tap_y = min(height - 1, y + delta_y)
                else:
                    tap_x = x
                    tap_y = min(height - 1, y + delta_y * 2)
                _emit(
                    logger,
                    "info",
                    "Draft dialog detected; clicking Start new video at %s for %s (attempt %s)",
                    (tap_x, tap_y),
                    target,
                    attempt,
                )
                _adb_tap(target, tap_x, tap_y, adb_client, logger=logger, description="Tapping Start new video")
                time.sleep(3)

                if _adb_wait_for_instagram_reel_composer(target, logger=logger, max_attempts=3, delay_seconds=1):
                    _emit(logger, "info", "Reel composer appears visible after draft dialog for %s", target)
                    return True

                current_start_new_video_center = _adb_find_instagram_start_new_video_center(target, logger=logger)
                if current_start_new_video_center is None:
                    _emit(
                        logger,
                        "info",
                        "Draft dialog closed after Start new video click for %s; assuming composer opened",
                        target,
                    )
                    return True
            return False

        if _adb_wait_for_instagram_reel_composer(target, logger=logger, max_attempts=3, delay_seconds=1):
            _emit(logger, "info", "Reel composer appears visible after initial plus tap for %s", target)
            return True

        return False

    def _enter_reel_caption(self, target: str, adb_client, caption: str, logger=None) -> bool:
        caption_center = _adb_find_instagram_reel_caption_center(target, logger=logger)
        if caption_center is None:
            if logger is not None:
                _emit(logger, "warning", "Reel caption input not found for %s", target)
            return False

        _emit(logger, "info", "Tapping reel caption input at %s for %s", caption_center, target)
        _adb_tap(target, caption_center[0], caption_center[1], adb_client, logger=logger, description="Tapping reel caption input")
        time.sleep(1)
        adb_client.run_command(f"adb -s {target} shell {write_text(caption)}")
        time.sleep(1)
        return True

    def _select_story_media(self, target: str, adb_client, logger=None, prefer_reel_text: bool = False) -> bool:
        if prefer_reel_text:
            reel_option = self._reveal_reel_option_center(target, adb_client, logger=logger)
            if reel_option is not None:
                _emit(logger, "info", "Selecting REEL text option at %s for %s", reel_option, target)
                _adb_tap(target, reel_option[0], reel_option[1], adb_client, logger=logger, description="Tapping REEL option text")
                time.sleep(1.5)

        media_thumb = _adb_find_instagram_reel_media_thumbnail_center(target, logger=logger)
        if media_thumb is not None:
            _emit(logger, "info", "Selecting reel media target at %s for %s", media_thumb, target)
            _adb_tap(target, media_thumb[0], media_thumb[1], adb_client, logger=logger, description="Tapping reel media target")
            return True

        if prefer_reel_text:
            _emit(logger, "warning", "REEL text was tapped for %s, but no reel media target was detected afterwards", target)
            return False

        if logger is not None:
            _emit(logger, "warning", "Unable to locate a reel media thumbnail for %s", target)
        return False

    def _reveal_reel_option_center(self, target: str, adb_client, logger=None, max_swipes: int = 4):
        """Locate the REEL mode tab in the bottom create-mode carousel. On small
        screens REEL can sit off the right edge (only POST/STORY fit), so if it
        isn't found we swipe the carousel right->left to reveal it and re-scan,
        up to `max_swipes` times. Only swipes when REEL isn't already visible."""
        center = _adb_find_instagram_reel_option_center(target, logger=logger)
        if center is not None:
            return center

        screen = instagram_module._adb_get_screen_size(target, logger=logger) or (1080, 2340)
        width, height = screen
        y = int(height * 0.88)
        x_start = int(width * 0.80)
        x_end = int(width * 0.40)
        for attempt in range(1, max_swipes + 1):
            _emit(logger, "info", "REEL option not visible for %s; swiping mode carousel right->left (%s/%s)", target, attempt, max_swipes)
            adb_client.run_command(f"adb -s {target} shell input swipe {x_start} {y} {x_end} {y} 200")
            time.sleep(1.0)
            center = _adb_find_instagram_reel_option_center(target, logger=logger)
            if center is not None:
                _emit(logger, "info", "REEL option revealed after %s swipe(s) for %s", attempt, target)
                return center
        _emit(logger, "warning", "REEL option still not found after %s swipe(s) for %s", max_swipes, target)
        return None

    def _tap_next(self, target: str, adb_client, logger=None) -> bool:
        next_center = instagram_module._adb_find_instagram_next_center(target, logger=logger)
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
            or _adb_is_instagram_reel_composer_visible(target, logger=logger)
            or _adb_wait_for_instagram_story_share_screen(target, logger=logger)
        ):
            fallback_next = _adb_get_relative_point(target, 0.90, 0.92, logger=logger)
            if logger is not None:
                _emit(logger, "info", "Tapping fallback Next at %s,%s for %s", fallback_next[0], fallback_next[1], target)
            self._last_next_center = fallback_next
            _adb_tap(target, fallback_next[0], fallback_next[1], adb_client, logger=logger, description="Tapping fallback Next")
            return True

        if logger is not None:
            _emit(logger, "warning", "Skip tapping Next because story/reel compose/share screen is not detected on %s", target)
        return False

    def _tap_share_story(self, target: str, adb_client, logger=None) -> bool:
        share_center = _adb_find_instagram_share_center(target, logger=logger)
        if share_center is not None:
            _adb_tap(target, share_center[0], share_center[1], adb_client, logger=logger, description="Tapping Share")
            return True

        if logger is not None:
            _emit(logger, "warning", "Share button not detected for %s; skipping tap", target)
        return False


def _adb_find_instagram_reel_caption_center(target: str, logger=None) -> tuple[int, int] | None:
    root = instagram_module._adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    # First priority: exact text match for "write a caption"
    exact_text_candidates = []
    # Second priority: content-desc match for "write a caption"
    content_desc_candidates = []
    # Third priority: any other caption-related match
    fallback_candidates = []

    for node in root.iter():
        attrs = node.attrib
        text = str(attrs.get("text", "")).lower()
        content_desc = str(attrs.get("content-desc", "")).lower()
        resource_id = str(attrs.get("resource-id", "")).lower()
        class_name = str(attrs.get("class", "")).lower()

        bounds = attrs.get("bounds", "")
        match = __import__("re").search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        area = (x2 - x1) * (y2 - y1)
        if area < 1500:
            continue

        # Prioritize exact text match for "write a caption"
        if "write a caption" in text:
            exact_text_candidates.append((area, cx, cy))
        # Second priority: content-desc with "write a caption"
        elif "write a caption" in content_desc:
            content_desc_candidates.append((area, cx, cy))
        # Third priority: any caption-related text or content-desc
        elif "caption" in text or "caption" in content_desc:
            fallback_candidates.append((area, cx, cy))

    # Use the highest priority non-empty list
    if exact_text_candidates:
        exact_text_candidates.sort(reverse=True)
        _, cx, cy = exact_text_candidates[0]
        return cx, cy

    if content_desc_candidates:
        content_desc_candidates.sort(reverse=True)
        _, cx, cy = content_desc_candidates[0]
        return cx, cy

    if fallback_candidates:
        fallback_candidates.sort(reverse=True)
        _, cx, cy = fallback_candidates[0]
        return cx, cy

    return None


# Text Instagram shows once a reel finishes posting (a banner / celebratory
# screen). Matched case-insensitively against the UI dump. Kept reel-specific
# and celebratory on purpose: bare words like "posted"/"shared" also appear on
# the home feed, which would false-positive a success while we sit on the feed.
_REEL_POST_CONFIRMATION_PHRASES = (
    "your reel",
    "reel shared",
    "reel was shared",
    "reel is being shared",
    "reel posted",
    "high five",
    "thumbs up",
    "nice work",
    "way to go",
    "great job",
)


def _make_notification_probe(target, adb_client, logger=None):
    """Instagram's notification state, shared by both reel flows. An *ongoing* IG
    notification means an upload is still in flight -- proof to keep waiting
    instead of declaring failure."""
    def probe():
        try:
            out = adb_client.run_command(f"adb -s {target} shell dumpsys notification --noredact")
        except Exception:
            return None
        return reel_verify.parse_notification_state(out)

    return probe


def _make_dump_screen_text_probe(target, logger=None):
    """All visible text on screen, read from the UI dump, for the screen
    classifier (banner / error dialog / draft prompt / composer)."""
    def probe():
        root = instagram_module._adb_capture_ui_dump(target, logger=logger)
        if root is None:
            return ""
        parts = []
        for node in root.iter():
            attrs = node.attrib
            for key in ("text", "content-desc"):
                value = str(attrs.get(key, "") or "").strip()
                if value:
                    parts.append(value)
        return " ".join(parts).lower()

    return probe


def _adb_wait_for_reel_post_confirmation(target, logger=None, timeout: int = 300, poll_seconds: float = 2.0, should_stop=None) -> bool:
    """Poll the UI dump for the 'reel was posted' confirmation banner, keeping
    Instagram open, for up to `timeout` seconds (default 5 min). Returns True as
    soon as a confirmation phrase is seen, False on timeout or abort. Reel-upload
    success must never be reported unless this returns True."""
    deadline = time.time() + timeout
    phrases = _REEL_POST_CONFIRMATION_PHRASES
    _emit(logger, "info", "Waiting up to %ss for a reel-posted confirmation (%s) on %s ...", timeout, ", ".join(phrases), target)
    while time.time() < deadline:
        if callable(should_stop) and should_stop():
            _emit(logger, "info", "Abort requested while waiting for reel post confirmation on %s", target)
            return False
        root = instagram_module._adb_capture_ui_dump(target, logger=logger)
        if root is not None:
            for node in root.iter():
                attrs = node.attrib
                text = str(attrs.get("text", "")).lower()
                desc = str(attrs.get("content-desc", "") or attrs.get("contentDescription", "")).lower()
                haystack = f"{text} {desc}"
                for phrase in phrases:
                    if phrase in haystack:
                        _emit(logger, "info", ">>> REEL POST CONFIRMED for %s -- matched %r in %r", target, phrase, haystack.strip()[:80])
                        return True
        time.sleep(poll_seconds)
    _emit(logger, "warning", "No reel-posted confirmation detected within %ss for %s", timeout, target)
    return False


class InstagramReelUploadU2Flow:
    """uiautomator2 version of the reel-upload flow, for side-by-side testing
    against InstagramReelUploadFlow (dump + OCR).

    The media resolve/push half is byte-for-byte the same ADB logic --
    it never touched the UI tree, so there's nothing to improve there. Only the
    on-screen half changes: each control is selected from the live view tree by
    text / content-desc / class (with implicit waits) instead of dumping XML and
    scoring coordinates. Coordinate fallbacks are kept for the spots Instagram
    renders without a stable, queryable id (the create '+', the gallery cell,
    and the Next/Share buttons), so a selector miss still degrades to the same
    behaviour the dump/OCR flow relied on.

    Requires `uiautomator2` (pip install uiautomator2). `target` is the device's
    ADB serial (ip:port), which uiautomator2 connects to directly.
    """

    name = "instagram_reel_upload_u2"
    IG_PACKAGE = "com.instagram.android"
    SELECTOR_WAIT_SECONDS = 15.0

    # Selectors for the controls each step waits on. Kept next to the tap
    # helpers that use them so a readiness check can't drift from the tap.
    # The bottom-nav create control. Instagram's nav tabs share a `*_tab`
    # resource-id family (feed_tab / profile_tab are confirmed on device), so the
    # id is far more reliable than the content-desc -- which on some builds isn't
    # "Create" at all, sending the flow into an expensive view-tree scoring pass.
    _CREATE_SELECTORS = (
        {"resourceId": "com.instagram.android:id/creation_tab"},
        {"resourceIdMatches": r"com\.instagram\.android:id/(creation_tab|create_tab|tab_create|new_post_tab)"},
        {"description": "Create"},
        {"descriptionStartsWith": "Create"},
        {"descriptionContains": "Create"},
        {"descriptionMatches": "(?i)^(new post|add post|add|create new)$"},
    )
    # The profile header's post counter. Ids move between IG builds, so try a
    # couple; the "N posts" content-desc form is handled separately.
    _POST_COUNT_SELECTORS = (
        {"resourceId": "com.instagram.android:id/row_profile_header_textview_post_count"},
        {"resourceIdMatches": r"(?i)com\.instagram\.android:id/.*post.*count.*"},
    )

    # The composer/gallery is open once any of these is on screen.
    _GALLERY_SELECTORS = (
        {"textMatches": "(?i)^reels?$"},
        {"descriptionMatches": "(?i)^reels?$"},
        {"textMatches": "(?i)^next$"},
        {"descriptionStartsWith": "Video"},
        {"descriptionStartsWith": "Photo"},
        {"textContains": "Recents"},
    )
    _NEXT_SELECTORS = ({"text": "Next"}, {"description": "Next"},
                       {"textMatches": "(?i)^next$"}, {"descriptionMatches": "(?i)^next$"})
    _SHARE_SELECTORS = ({"text": "Share"}, {"textMatches": "(?i)^share$"}, {"description": "Share"})
    # The second advance button is labelled Next on some builds and Continue on
    # others -- and on the build we tested the screen goes straight to Share.
    _ADVANCE_SELECTORS = _NEXT_SELECTORS + (
        {"text": "Continue"}, {"textMatches": "(?i)^continue$"}, {"description": "Continue"},
    )
    _CAPTION_SELECTORS = ({"resourceIdMatches": r"(?i)com\.instagram\.android:id/.*caption.*"},
                          {"textMatches": "(?i).*(write a caption|add a caption).*"})

    def get_progress_total_steps(self, target: str) -> int:
        return 7

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
                emit("info", "Abort requested during Instagram reel upload (u2) flow for profile %s", profile.id)
                return True
            return False

        def mark_step() -> None:
            if hasattr(adb_client, "mark_progress_step"):
                adb_client.mark_progress_step()

        if u2 is None:
            emit(
                "warning",
                "uiautomator2 is not importable in the interpreter running this app "
                "(python=%s, frozen_exe=%s). Install it into THAT environment or rebuild "
                "the exe with it bundled. Skipping %s",
                sys.executable,
                bool(getattr(sys, "frozen", False)),
                profile.id,
            )
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        emit("info", "Starting Instagram reel upload (u2) flow for profile %s", profile.id)

        # --- Media resolve + push (identical ADB logic to the dump/OCR flow) --
        # A per-run video (the Posting Queue's Spoof Variant) wins over the global
        # media setting/folder used for manual/warmup reel runs.
        media_path = getattr(profile, "media_path", None) or _adb_resolve_story_media_path(logger=log)
        if media_path is None:
            emit("warning", "No reel upload media found for profile %s", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        media_source = Path(media_path)
        selected_media = None
        media_queue = None
        if media_source.is_dir():
            media_queue = get_story_media_queue(media_source, logger=log)
            selected_media = media_queue.get_next_media()
            if selected_media is None:
                emit("warning", "No pending reel media available for profile %s from folder %s", profile.id, media_source)
                return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
            media_path = str(selected_media)
            emit("info", "Assigned reel media %s to profile %s from folder %s", media_path, profile.id, media_source)
        else:
            selected_media = media_source

        # Have we already sent this exact clip to this account? The ledger is
        # written the instant Share is tapped, so it answers even when the
        # previous run crashed mid-verification or never reached Airtable. This
        # is the guard that makes an unproven post safe to leave unproven --
        # without it, every "we couldn't tell" eventually becomes a second post.
        ledger = post_ledger.PostLedger()
        media_hash = post_ledger.media_fingerprint(media_path)
        prior = ledger.lookup(str(profile.id), media_hash)
        if prior is not None and prior.blocks_repost():
            emit("warning",
                 "Refusing to post %s to profile %s again -- Share was already tapped for this "
                 "clip %.0f min ago (%s). Not a failure: the earlier post is live or still "
                 "being verified.",
                 Path(media_path).name, profile.id, prior.age_seconds / 60.0, prior.status)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "uncertain": False, "already_shared": True,
                    "verify_method": "ledger", "verify_detail": f"already shared ({prior.status})"}

        remote_media_path = self._build_remote_media_path(media_path)
        emit("info", "Preparing to push reel media for profile %s: %s -> %s", profile.id, media_path, remote_media_path)
        mark_step()
        if not _adb_push_media_to_device(target, media_path, remote_media_path, logger=log):
            emit("warning", "adb push failed for profile %s on target %s", profile.id, target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
        emit("info", "adb push succeeded for profile %s on target %s", profile.id, target)
        # Push + media scan only: the on-device file matched the local one on
        # every run we checked, so the ls + sha256sum verification (and its
        # 3x re-push loop) was removed. The trade-off is that a push which
        # reports success over a half-dead adb tunnel is no longer caught here
        # -- it surfaces later, as the picker not finding the clip.

        # NOTE: the media is deliberately NOT marked used here. Pushing a file to
        # the phone is not the same as posting it -- if the flow fails later the
        # clip must stay in the queue so the next run can retry it, rather than
        # being silently consumed. Committed only once the post is confirmed.
        def commit_media_used() -> None:
            if media_source.is_dir() and selected_media is not None and media_queue is not None:
                moved = media_queue.mark_used(selected_media)
                emit("info", "Marked reel media %s as used for profile %s: %s", selected_media, profile.id, moved)

        def keep_media_for_retry(reason: str) -> None:
            if media_source.is_dir() and selected_media is not None:
                emit("info", "Leaving reel media %s in the queue for a retry (%s)", selected_media, reason)

        mark_step()
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # Pick up the configured speed profile for this run's waits.
        waits.set_speed(None)
        emit("info", "Flow speed factor for %s: %.2fx", target, waits.speed_factor())

        # --- Launch Instagram (same commands as the dump/OCR flow) -----------
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            is_start = "monkey" in command or "am start" in command
            # Wait for Instagram to actually reach the foreground instead of
            # blindly sleeping; falls back to the old delay if we can't tell.
            waits.settle(
                5 if is_start else 2,
                ready=(lambda: self._ig_is_foreground(target, adb_client)) if is_start else None,
                logger=log, what="Instagram in foreground",
            )

        mark_step()

        emit("info", "Connecting uiautomator2 to %s", target)
        try:
            d = u2.connect(target)
            d.implicitly_wait(self.SELECTOR_WAIT_SECONDS)
        except Exception as exc:
            emit("warning", "uiautomator2 could not connect to %s: %s", target, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        # Instagram is up, but its first frame may still be loading. Wait for a
        # real piece of UI (bottom nav) rather than a flat 10-second sleep.
        waits.settle(
            10,
            ready=waits.u2_ready(
                d,
                {"resourceId": "com.instagram.android:id/feed_tab"},
                {"resourceIdMatches": r"com\.instagram\.android:id/.*(tab_bar|profile_tab).*"},
            ),
            logger=log, what="Instagram UI loaded",
        )

        # Only sweep for pop-ups if the create control isn't already reachable --
        # on a clean feed that check is one cheap RPC instead of a full sweep.
        # "Screen is usable" = the bottom nav is up. The create control itself is
        # unlabeled on this build, so we can't ask for it by name; the nav tabs
        # are the reliable proof that the feed rendered and nothing is covering it.
        self._dismiss_popups_u2(
            d, logger=log,
            skip_if=lambda: waits.any_exists(
                d,
                {"resourceId": "com.instagram.android:id/feed_tab"},
                {"resourceId": "com.instagram.android:id/profile_tab"},
            ),
        )
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # Baseline for post-verification: read the account's post count BEFORE
        # uploading, so afterwards a +1 proves the reel landed even if Instagram
        # never shows its confirmation banner. Best-effort -- if it can't be
        # read, verification falls back to the banner/notification signals.
        baseline_count = None
        if self._open_profile_tab_u2(d, target, logger=log):
            waits.settle(2, ready=waits.u2_ready(d, *self._POST_COUNT_SELECTORS),
                         logger=log, what="profile header")
            baseline_count = self._read_post_count_u2(d, target, logger=log)
        emit("info", "Baseline post count for %s: %s", target,
             baseline_count.value if baseline_count else "unavailable")

        # If Instagram opened on the Reels tab (or elsewhere), switch to the
        # main feed first so the composer/nav is where the flow expects.
        instagram_module._ensure_instagram_home_feed_u2(d, target, logger=log)

        # --- Step 1: open the reel composer ----------------------------------
        emit("info", "Opening reel composer for %s", target)
        if not self._open_reel_composer_u2(d, target, emit, log):
            flagged = self._account_flag_result_u2(d, profile, target, emit, "opening the composer")
            if flagged:
                return flagged
            emit("warning", "Unable to open the Instagram reel composer for %s (leaving Instagram open)", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False, "failed": True}
        mark_step()
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # --- Step 2: select REEL mode + first media --------------------------
        emit("info", "Selecting reel media for %s", target)
        if not self._select_media_u2(d, target, emit, log):
            flagged = self._account_flag_result_u2(d, profile, target, emit, "selecting media")
            if flagged:
                return flagged
            emit("warning", "Unable to select reel media for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
        mark_step()
        # The editor screen is up once its Next/advance control appears.
        waits.settle(3, ready=waits.u2_ready(d, *self._NEXT_SELECTORS),
                     logger=log, what="reel editor")

        # Instagram now sometimes shows a promo for its "Edits" video-editing app
        # right after the reel media is selected, which covers the composer's
        # Next button. Detect and dismiss it before trying to tap Next.
        self._dismiss_edit_app_popup_u2(d, target, emit, log)

        # --- Step 3: Next -> (caption) -> Next --------------------------------
        if self._tap_next_u2(d, target, emit, log):
            emit("info", "Tapped first Next for %s", target)
            # Next landed when the caption field (or the Share control) shows up.
            waits.settle(3, ready=waits.u2_ready(d, *self._CAPTION_SELECTORS, *self._SHARE_SELECTORS),
                         logger=log, what="caption/share screen")
            if profile.caption:
                if self._enter_caption_u2(d, target, adb_client, profile.caption, emit, log):
                    emit("info", "Entered reel caption for %s", target)
                    waits.settle(1)
                    try:
                        d.press("back")  # hide the keyboard so it can't cover Next/Share
                    except Exception:
                        adb_client.run_command(f"adb -s {target} shell input keyevent 111")
                    # Keyboard gone once Share/Next is reachable again.
                    waits.settle(1, ready=waits.u2_ready(d, *self._SHARE_SELECTORS, *self._NEXT_SELECTORS),
                                 logger=log, what="keyboard dismissed")
            # The screen after the caption is the share screen on some builds and
            # one more Next/Continue on others. Check for Share FIRST: hunting for
            # a Next that isn't there cost ~9s of selector misses and then fired a
            # blind ratio tap at a coordinate that hit nothing.
            if waits.any_exists(d, *self._SHARE_SELECTORS):
                emit("info", "Already on the share screen for %s; no second Next needed", target)
            elif self._tap_advance_u2(d, target, emit, log):
                emit("info", "Tapped second advance (Next/Continue) for %s", target)
                waits.settle(3, ready=waits.u2_ready(d, *self._SHARE_SELECTORS),
                             logger=log, what="share screen")
        mark_step()

        # --- Step 4: Share ---------------------------------------------------
        reel_posted = False
        if self._tap_share_u2(d, target, emit, log):
            emit("info", "Tapped Share for %s", target)
            # Write the ledger entry NOW -- before the settle, before
            # verification, before anything that can crash or hang. From this
            # instant a reel may exist on the account, and that fact has to
            # outlive this process. Everything after here only refines the
            # record; nothing after here is allowed to be the thing that
            # creates it.
            ledger.record_share(str(profile.id), media_path,
                                caption=getattr(profile, "caption", "") or "",
                                queue_id=getattr(profile, "queue_id", "") or "",
                                media_hash=media_hash,
                                baseline_count=baseline_count)
            # Share registered once the composer is gone (we're back on a feed
            # tab). Verification below does the real confirmation work.
            waits.settle(8, ready=waits.u2_ready(d, {"resourceId": "com.instagram.android:id/feed_tab"}),
                         logger=log, what="composer closed")
            reel_posted = True
        else:
            emit("warning", "Share button was not detected after reel compose for %s", target)

        if not reel_posted:
            # Share was never tapped, so nothing can have gone out. Safe to
            # retry -- unless what blocked Share was an account flag, which no
            # number of retries will clear.
            flagged = self._account_flag_result_u2(d, profile, target, emit, "the Share step")
            if flagged:
                return flagged
            emit("warning", "Instagram reel upload (u2) did not complete successfully for %s "
                            "(the Share button was never tapped -- nothing was posted)", target)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "uncertain": False}

        # --- Step 5: confirm the post ----------------------------------------
        # Look at the screen before touching it. Share has just landed and the
        # composer has closed, which is the exact moment Instagram shows its
        # confirmation -- and it is the last moment we can be sure of seeing it,
        # because both of the next two steps destroy it: dismissing popups can
        # tap it away, and the home tap re-renders the feed under it. If the
        # phone is already saying the reel went out, that is the answer; there is
        # nothing a post counter could add.
        verdict = None
        try:
            early_text = self._screen_text_probe_u2(d, target, logger=log)()
            if reel_verify.classify_post_screen(early_text) == reel_verify.STATE_CONFIRMED:
                verdict = reel_verify.VerifyResult(
                    True, reel_verify.VIA_BANNER,
                    "confirmation seen on the feed immediately after Share",
                )
        except Exception as exc:
            _emit(log, "info", "u2: early confirmation read failed for %s (%s); "
                               "falling through to full verification", target, exc)

        if verdict is None:
            # Tap the IG Home (house) icon -- NOT the Android home key -- so we
            # stay inside Instagram. We do NOT close Instagram afterwards.
            # Posting is a common moment for Instagram to throw up a prompt (the
            # "Rate Instagram" dialog turns up here). Clear it before
            # verification so it can't block the feed/profile navigation the
            # post-count check needs.
            self._dismiss_popups_u2(d, logger=log, max_rounds=2)
            self._tap_ig_home_icon_u2(d, target, emit, log)
            # Success is only declared once the phone proves the post landed.
            # The banner is checked first each pass because it is perishable;
            # the post count is what catches a silent success. Never gives up
            # before the 3-minute floor.
            # Fast budget: ~45s, no 3-minute failure floor. An unproven post is
            # no longer reported as Failed -- it goes to the recheck queue -- so
            # there is nothing to protect against by waiting longer here, and a
            # profile must not sit on a phone for five minutes per post.
            verdict = reel_verify.verify_reel_posted(
                baseline_count=baseline_count,
                get_post_count=self._profile_post_count_probe_u2(d, target, emit, log),
                get_screen_text=self._screen_text_probe_u2(d, target, logger=log),
                get_notification_state=self._notification_probe(target, adb_client, logger=log),
                min_wait=reel_verify.FAST_MIN_WAIT_SECONDS,
                timeout=reel_verify.FAST_TIMEOUT_SECONDS,
                should_stop=should_stop,
                emit=emit,
            )
        post_confirmed = verdict.confirmed
        mark_step()

        # Fold the verdict back into the ledger. Only a *conclusive* negative
        # clears the clip for another send; a timeout leaves it blocked, which
        # is the safe direction to be wrong in.
        if post_confirmed:
            ledger.resolve(str(profile.id), media_hash,
                           post_ledger.STATUS_CONFIRMED, verdict.method)
        elif not verdict.uncertain:
            ledger.resolve(str(profile.id), media_hash,
                           post_ledger.STATUS_DISPROVED, verdict.method)

        if callable(should_stop) and should_stop():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # Share WAS tapped, so the reel may well be live. Whether we could prove
        # it decides the outcome:
        #   confirmed                  -> success
        #   conclusive negative        -> failed (error dialog / draft / composer)
        #   nothing conclusive         -> UNCERTAIN, never "failed"
        # Retrying an uncertain post is how an account posts the same reel twice.
        uncertain = (not post_confirmed) and verdict.uncertain

        if post_confirmed:
            commit_media_used()
            emit("info", "Instagram reel upload (u2) for %s: %s", target, verdict.summary())
        elif uncertain:
            # Not a failure and not a success -- an open question, handed to the
            # deferred recheck rather than guessed at now. The clip stays in the
            # queue but the ledger blocks a blind re-send until the recheck says
            # otherwise.
            keep_media_for_retry("outcome uncertain -- queued for recheck")
            emit("warning", "Instagram reel upload (u2) for %s: %s -- Share was tapped, so the "
                            "reel may well be live. Handing off to the deferred recheck; the "
                            "ledger blocks a re-post until it resolves. Leaving Instagram open.",
                 target, verdict.summary())
        else:
            keep_media_for_retry("post not confirmed")
            emit("warning", "Instagram reel upload (u2) for %s: %s -- leaving Instagram open "
                            "in case the upload is still finishing", target, verdict.summary())
        return {
            "profile_id": profile.id,
            "target": target,
            "aborted": False,
            "success": post_confirmed,
            "uncertain": uncertain,
            "post_confirmed": post_confirmed,
            "verify_method": verdict.method,
            "verify_strength": verdict.strength,
            "verify_detail": verdict.detail,
            # Carried out so the recheck pass can close the ledger entry for
            # this exact clip without re-hashing the file.
            "media_hash": media_hash,
            "media_path": media_path,
        }

    def build_launch_commands(self, target: str) -> list[str]:
        return [
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    def _build_remote_media_path(self, local_media_path: str) -> str:
        return f"/sdcard/Download/{Path(local_media_path).name.replace(' ', '_')}"

    # -- uiautomator2 helpers -------------------------------------------------

    def _find_dismiss_in_dump(self, d, logger=None):
        """Locate a safe dismiss control from ONE hierarchy dump.

        The previous version asked the device for each label separately
        (10 labels x 2 regex selectors x 3 rounds = 60 full-tree scans, ~74 s on
        a busy feed). One dump plus a local scan is a single RPC.

        Returns (label, center, bounds) or None. Only visible, clickable nodes
        whose text/content-desc is EXACTLY a safe label qualify, so feed content
        can't be mistaken for a pop-up control.
        """
        try:
            xml = d.dump_hierarchy()
        except Exception as exc:
            _emit(logger, "warning", "u2: could not dump hierarchy for pop-up scan: %s", exc)
            return None
        try:
            root = ET.fromstring(xml)
        except Exception:
            return None

        safe = {label.lower() for label in instagram_module._SAFE_DISMISS_LABELS}
        # Dialog buttons are usually a TextView *inside* a clickable row (IG's
        # "Rate Instagram" prompt is built this way, and the Next/Share controls
        # log clickable=False too), so the label itself often isn't clickable.
        # Walk up to the nearest clickable ancestor and tap that; if there isn't
        # one, tapping the label's own centre still works.
        parents = {child: parent for parent in root.iter() for child in parent}
        fallback = None

        for node in root.iter():
            attrs = node.attrib
            if attrs.get("visible-to-user") == "false":
                continue
            label = None
            for key in ("text", "content-desc"):
                value = str(attrs.get(key, "") or "").strip().lower()
                if value and value in safe:
                    label = value
                    break
            if label is None:
                continue

            target_attrs = attrs
            if attrs.get("clickable") != "true":
                walker = node
                for _ in range(4):     # dialog rows nest only a level or two
                    walker = parents.get(walker)
                    if walker is None:
                        break
                    if walker.attrib.get("clickable") == "true":
                        target_attrs = walker.attrib
                        break

            center = instagram_module._node_center(target_attrs)
            if center is None:
                continue
            bounds = target_attrs.get("bounds", "")
            if target_attrs.get("clickable") == "true":
                return (label, center, bounds)
            # No clickable ancestor: usable, but prefer a genuinely clickable
            # control if one turns up later in the tree.
            if fallback is None:
                fallback = (label, center, bounds)

        return fallback

    def _dismiss_popups_u2(self, d, logger=None, max_rounds: int = 3, skip_if=None) -> None:
        """Tap only safe dismiss controls to clear interstitial pop-ups.

        `skip_if` is a predicate meaning "the screen is already usable" -- when it
        holds we don't scan at all. On the home feed the Create button is right
        there, so the common case costs one cheap check instead of a full sweep.

        Stops as soon as a tap doesn't change anything: Instagram's feed contains
        permanently-present 'Dismiss' affordances that are clickable but never go
        away, and re-tapping them just burned rounds.
        """
        if callable(skip_if):
            try:
                if skip_if():
                    _emit(logger, "info", "u2: screen already usable; skipping pop-up scan")
                    return
            except Exception:
                pass

        last_bounds = None
        for _ in range(max_rounds):
            found = self._find_dismiss_in_dump(d, logger=logger)
            if found is None:
                break
            label, center, bounds = found
            if bounds and bounds == last_bounds:
                _emit(logger, "info",
                      "u2: pop-up '%s' at %s did not go away after tapping; leaving it alone", label, bounds)
                break
            _emit(logger, "info", "u2: dismissing pop-up '%s' at %s", label, bounds)
            try:
                d.click(center[0], center[1])
            except Exception as exc:
                _emit(logger, "warning", "u2: failed to dismiss pop-up '%s': %s", label, exc)
                break
            last_bounds = bounds
            waits.settle(1.0)

    def _find_create_via_dump_u2(self, d, target, logger=None):
        """Last resort before a blind tap: dump the uiautomator view tree and
        locate a create ('+') node when IG doesn't expose it as content-desc
        'Create'. Scores nodes by resource-id / content-desc / class, plus a
        top-left position hint (where this build shows the +). Logs the top
        candidates so a failing run reveals the real element. Returns the (x, y)
        center to tap, or None."""
        try:
            xml = d.dump_hierarchy()
            root = ET.fromstring(xml)
        except Exception as exc:
            _emit(logger, "warning", "u2: view-tree dump failed for %s: %s", target, exc)
            return None

        try:
            width, height = d.window_size()
        except Exception:
            width, height = (0, 0)

        best = None
        best_score = 0
        best_attrs = ("", "", "", "")
        logged = 0
        for node in root.iter("node"):
            attrs = node.attrib
            match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
            if not match:
                continue
            x1, y1, x2, y2 = map(int, match.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

            rid = (attrs.get("resource-id") or "").lower()
            desc = (attrs.get("content-desc") or "").lower()
            cls = (attrs.get("class") or "").lower()
            clickable = attrs.get("clickable") == "true"

            # Never score non-Instagram chrome as the create button. If a stray
            # nav dropped us onto the Android launcher/search, its buttons
            # (com.android.launcher3:id/home, com.android.quicksearchbox search,
            # systemui nav bar) otherwise win the top-left position hint below
            # and get tapped -- exactly the 199.190.44.226:27044/:23320 misfires
            # where the create tap landed on the launcher search icon.
            if rid.startswith("com.android.") or rid.startswith("android:"):
                continue
            # Story rings and feed content live in the same top-left zone as the
            # + on some builds. Their content-desc identifies them ("<user>'s
            # story, N of M, unseen." / profile pics / posts). Excluding them
            # stops the position hint from tapping a story ring (which opens the
            # story viewer, not the composer) -- the 199.190.44.226:26309 misfire
            # that then failed with "REEL tab never became visible".
            if (
                "'s story" in desc
                or "story," in desc
                or "unseen" in desc
                or "profile picture" in desc
                or "'s post" in desc
                or "'s reel" in desc
            ):
                continue
            # Other corner controls that must never win the position hint: on a
            # build that moves the +, tapping these would open Direct/search/
            # notifications instead of the composer -- a silent wrong turn.
            if any(
                token in desc
                for token in ("direct", "messenger", "message", "inbox", "notification",
                              "activity feed", "search", "settings", "back")
            ):
                continue

            score = 0
            if "creation" in rid or "new_post" in rid or "create" in rid:
                score += 10
            if "create" in desc or "new post" in desc or "compose" in desc or desc == "add":
                score += 8
            if "camera" in rid:
                score += 3
            if clickable and (cls.endswith("imageview") or cls.endswith("button")):
                score += 1
                # Position hint: a clickable image button in the top-left corner
                # (where the + sits on this build).
                if width and height and cx < width * 0.25 and cy < height * 0.18:
                    score += 4
            if score <= 0:
                continue

            if logged < 8:
                _emit(logger, "info", "u2: create-candidate score=%s id=%r desc=%r class=%r center=(%s,%s)", score, rid, desc, cls, cx, cy)
                logged += 1
            if score > best_score:
                best_score = score
                best = (cx, cy)
                best_attrs = (rid, desc, cls, attrs.get("bounds", ""))

        if best is None:
            _emit(logger, "warning", "u2: no create-looking node in the view tree for %s (will fall back to a coordinate tap)", target)
        else:
            # Always log the WINNER's identity, not just its position -- the
            # capped candidate list above can scroll it off, which is how we
            # spent two runs not knowing what was actually being tapped. On this
            # build it turns out to be an unlabeled node (no resource-id, no
            # content-desc), which is why no selector can ever find it.
            rid, desc, cls, bounds = best_attrs
            _emit(logger, "info",
                  "u2: best create candidate for %s -> score=%s id=%r desc=%r class=%r bounds=%s center=%s",
                  target, best_score, rid, desc, cls, bounds, best)
        return best

    def _account_flag_result_u2(self, d, profile, target, emit, what: str):
        """If an IG block screen is what stopped the flow, return the result dict
        that says so; None if the screen isn't one.

        Called at every point the flow gives up *before* Share. Without it a
        checkpoint reads as a plain failure, and a plain failure is retryable --
        so a challenged account gets relaunched forever and nobody is told a
        person has to tap it through. Proven 2026-08-03: a "Confirm you're
        human" screen produced `Failed - Needs Retry` with a retry bump.

        Only pre-Share paths use it. After Share the post may exist, and the
        uncertain/verify path owns that decision -- flagging the account there
        could discard a live post.
        """
        flag = instagram_module.account_flag_u2(d)
        if not flag:
            return None
        emit("warning", "Instagram flagged %s during %s: %s -- not a retryable failure",
             target, what, flag)
        return {"profile_id": profile.id, "target": target, "aborted": False,
                "success": False, "account_flag": flag}

    def _open_reel_composer_u2(self, d, target, emit, logger=None) -> bool:
        # Make sure Instagram is the foreground app before hunting for the + --
        # a stray nav could have dropped us to the launcher, and we must not dump
        # / tap launcher controls.
        self._recover_to_instagram_u2(d, target, emit, logger)
        # Find it from ONE view-tree dump rather than asking the device about
        # each candidate name in turn. On this build the + is an *unlabeled*
        # node -- no resource-id, no content-desc -- so the six name lookups
        # could never match and cost ~10s of pure latency every run. The dump
        # scan below matches ids and content-descs too (scoring them higher than
        # position), so a build that does label the button is still handled --
        # just locally, for the price of a single RPC.
        clicked = False
        center = self._find_create_via_dump_u2(d, target, logger)
        if center is not None:
            _emit(logger, "info", "u2: tapping Create (+) located via view-tree dump at %s for %s", center, target)
            try:
                d.click(center[0], center[1])
                clicked = True
            except Exception as exc:
                _emit(logger, "warning", "u2: dump-located Create tap failed for %s: %s", target, exc)

        if not clicked:
            try:
                w, h = d.window_size()
            except Exception:
                w, h = (0, 0)
            _emit(logger, "info", "u2: Create (+) not found in the view tree; FALLBACK tap top-left (0.08,0.08) ~ px (%s,%s) for %s", int(w * 0.08) if w else "?", int(h * 0.08) if h else "?", target)
            try:
                d.click(0.08, 0.08)
            except Exception as exc:
                _emit(logger, "warning", "u2: fallback Create tap failed for %s: %s", target, exc)

        # The composer usually opens straight into the gallery; only wait for it
        # if it isn't up yet.
        waits.settle(3, ready=waits.u2_ready(d, *self._GALLERY_SELECTORS),
                     logger=logger, what="reel gallery")

        # Some accounts land on a draft dialog offering to resume a previous
        # video. Only a *clickable* control counts: the gallery's own title is
        # the text "New reel" (id gallery_title_text, clickable=false), which
        # used to match here and cost a wasted click on a plain label.
        draft = _u2_find(
            d,
            [{"textContains": "Start new video", "clickable": True},
             {"descriptionContains": "Start new video", "clickable": True},
             {"textMatches": "(?i)^(start new video|new video)$", "clickable": True}],
            logger=logger,
            purpose="'Start new video' draft dialog",
        )
        if draft is not None:
            _emit(logger, "info", "u2: clicking draft dialog -> %s", _u2_describe(draft))
            try:
                draft.click()
                waits.settle(3, ready=waits.u2_ready(d, *self._GALLERY_SELECTORS),
                             logger=logger, what="gallery after draft dialog")
            except Exception as exc:
                _emit(logger, "warning", "u2: draft dialog click failed: %s", exc)
        else:
            _emit(logger, "info", "u2: no draft dialog present for %s; continuing", target)

        # Composer/gallery is open once any of these are present.
        gallery_ready = self._first_present(
            d,
            list(self._GALLERY_SELECTORS),
            timeout=self.SELECTOR_WAIT_SECONDS,
            logger=logger,
            purpose="reel composer / gallery",
        )
        if gallery_ready is None:
            _emit(logger, "warning", "Reel composer/gallery did not appear for %s", target)
            return False
        return True

    def _reel_tab_selectors(self):
        return [
            {"text": "REEL"}, {"text": "Reel"}, {"text": "Reels"},
            {"textMatches": "(?i)^reels?$"}, {"descriptionMatches": "(?i)^reels?$"},
        ]

    def _reel_tab_visible_u2(self, d) -> bool:
        for kwargs in self._reel_tab_selectors():
            try:
                if d(**kwargs).exists:
                    return True
            except Exception:
                continue
        return False

    def _mode_carousel_y_ratio_u2(self, d) -> float:
        """Vertical position (as a 0-1 screen ratio) of the create-mode carousel,
        anchored on a currently-visible mode label so the reveal-swipe lands on
        the carousel row and not the gallery grid above it. Falls back to 0.88."""
        try:
            _, height = d.window_size()
        except Exception:
            height = 0
        for kwargs in ({"text": "POST"}, {"text": "Post"}, {"text": "STORY"}, {"text": "Story"}, {"text": "LIVE"}, {"text": "Live"}):
            try:
                sel = d(**kwargs)
                if sel.exists:
                    bounds = (sel.info or {}).get("bounds") or {}
                    cy = (bounds.get("top", 0) + bounds.get("bottom", 0)) / 2 if bounds else 0
                    if cy and height:
                        return cy / height
            except Exception:
                continue
        return 0.88

    def _other_mode_selectors(self):
        return [
            {"text": "POST"}, {"text": "Post"},
            {"text": "STORY"}, {"text": "Story"},
            {"text": "LIVE"}, {"text": "Live"},
        ]

    def _tab_is_selected_u2(self, d, selectors) -> bool:
        """True if any of `selectors` matches an on-screen mode tab whose
        `selected` state is True. Instagram marks the active carousel mode with
        selected=true; a missing/False flag means 'not the active mode' (or the
        build doesn't expose it)."""
        for kwargs in selectors:
            try:
                sel = d(**kwargs)
                if sel.exists and (sel.info or {}).get("selected") is True:
                    return True
            except Exception:
                continue
        return False

    def _select_reel_mode_u2(self, d, target, emit, logger=None, max_swipes: int = 4, max_confirm: int = 3) -> bool:
        """Reveal (swiping the carousel right->left if REEL is off-screen), tap
        REEL, then CONFIRM the carousel actually switched to REEL before
        returning -- so we never pick media in POST/STORY mode and silently post
        the wrong type.

        Returns True when REEL is the active mode (or its selected-state isn't
        readable and no other mode is active -> best-effort, no regression on IG
        builds without the flag). Returns False only when REEL can't be found or
        a different mode stays selected -> the caller then refuses to pick media."""
        selectors = self._reel_tab_selectors()
        other_mode = self._other_mode_selectors()

        # 1) Make REEL visible (swipe the carousel right->left if off-screen).
        if not self._reel_tab_visible_u2(d):
            y = self._mode_carousel_y_ratio_u2(d)
            _emit(logger, "info", "u2: REEL tab not visible for %s; swiping mode carousel right->left at y=%.2f to reveal it", target, y)
            for attempt in range(1, max_swipes + 1):
                try:
                    d.swipe(0.80, y, 0.40, y, 0.2)
                except Exception as exc:
                    _emit(logger, "warning", "u2: mode-carousel swipe failed for %s: %s", target, exc)
                time.sleep(1.0)
                if self._reel_tab_visible_u2(d):
                    _emit(logger, "info", "u2: REEL tab revealed after %s swipe(s) for %s", attempt, target)
                    break
            if not self._reel_tab_visible_u2(d):
                _emit(logger, "warning", "u2: REEL tab never became visible after %s swipe(s) for %s", max_swipes, target)
                return False

        # 2) Tap REEL, then confirm the carousel switched. Re-tap if it didn't.
        for attempt in range(1, max_confirm + 1):
            _u2_click(d, selectors, logger=logger, purpose="REEL mode tab")
            time.sleep(1.0)
            if self._tab_is_selected_u2(d, selectors):
                _emit(logger, "info", "u2: REEL mode confirmed selected for %s (attempt %s)", target, attempt)
                return True
            if self._tab_is_selected_u2(d, other_mode):
                _emit(logger, "warning", "u2: a non-REEL mode is still selected after tapping REEL for %s; re-tapping (attempt %s/%s)", target, attempt, max_confirm)
                continue
            _emit(logger, "info", "u2: REEL selected-state not readable for %s (attempt %s/%s)", target, attempt, max_confirm)

        # Couldn't confirm REEL selected after retries. Block only if we can see
        # we're positively in another mode; otherwise proceed best-effort.
        if self._tab_is_selected_u2(d, other_mode):
            _emit(logger, "warning", "u2: still in a non-REEL mode after %s attempts for %s; refusing to pick media to avoid posting a non-reel", max_confirm, target)
            return False
        _emit(logger, "info", "u2: proceeding best-effort for %s (REEL selected-state unconfirmed, but no wrong mode detected)", target)
        return True

    def _select_media_u2(self, d, target, emit, logger=None) -> bool:
        # Ensure we're actually in REEL mode (reveal + confirm) BEFORE picking
        # media, so a mis-registered REEL tap can't lead to selecting a thumbnail
        # in POST/STORY mode and posting the wrong type.
        if not self._select_reel_mode_u2(d, target, emit, logger=logger):
            _emit(logger, "warning", "u2: could not confirm REEL mode for %s; not selecting media to avoid posting a non-reel", target)
            return False
        # Wait for a gallery cell rather than a flat 1.5s -- the very selectors
        # the click below uses are what "the gallery has repainted" means, so a
        # responsive phone proceeds as soon as a thumbnail is there.
        waits.settle(1.5, ready=waits.u2_ready(
            d,
            {"descriptionStartsWith": "Video"},
            {"descriptionStartsWith": "Photo"},
            {"descriptionContains": "Video"},
            {"descriptionContains": "Photo"},
        ), logger=logger, what="reel gallery thumbnails")

        # Pick the first gallery thumbnail. Reels are video, so prefer a video
        # cell; gallery cells carry a "Video, ..." / "Photo, ..." content-desc.
        # Fall back to the first grid cell position (top-left of the gallery).
        return _u2_click(
            d,
            [
                {"descriptionStartsWith": "Video"},
                {"descriptionStartsWith": "Photo"},
                {"descriptionContains": "Video"},
                {"descriptionContains": "Photo"},
            ],
            logger=logger,
            purpose="first reel media thumbnail",
            fallback_ratio=(0.17, 0.30),
        )

    def _next_visible_u2(self, d) -> bool:
        try:
            return bool(
                d(text="Next").exists
                or d(description="Next").exists
                or d(textMatches="(?i)^next$").exists
            )
        except Exception:
            return False

    def _recover_to_instagram_u2(self, d, target, emit, logger=None, max_back: int = 3) -> bool:
        """If Instagram's 'Edits' promo deep-linked us out to the Google Play
        Store install page (or any other app is in the foreground), get back to
        Instagram: press Back (which closes the Play Store page and returns to
        the composer) and, as a last resort, bring IG to the front without
        restarting it. Never force-stop IG for this. Returns True if Instagram
        is in the foreground afterwards."""
        def _current_pkg() -> str:
            try:
                return (d.app_current() or {}).get("package", "") or ""
            except Exception:
                return ""

        if _current_pkg() == self.IG_PACKAGE:
            return True
        _emit(logger, "warning", "u2: foreground is %r (not Instagram) for %s -- recovering with Back", _current_pkg() or "unknown", target)
        for attempt in range(1, max_back + 1):
            try:
                d.press("back")
            except Exception as exc:
                _emit(logger, "warning", "u2: Back press failed during recovery for %s: %s", target, exc)
            # The condition was already checked on the next line -- poll it
            # instead of paying the whole sleep first. Ceiling unchanged.
            waits.settle(1.2, ready=lambda: _current_pkg() == self.IG_PACKAGE,
                         logger=logger, what="Instagram back in the foreground")
            if _current_pkg() == self.IG_PACKAGE:
                _emit(logger, "info", "u2: back in Instagram after %s Back press(es) for %s", attempt, target)
                return True
        try:
            _emit(logger, "info", "u2: bringing Instagram to the foreground (no restart) for %s", target)
            d.app_start(self.IG_PACKAGE, stop=False)
            waits.settle(2, ready=lambda: _current_pkg() == self.IG_PACKAGE,
                         logger=logger, what="Instagram foregrounded")
        except Exception as exc:
            _emit(logger, "warning", "u2: app_start recovery failed for %s: %s", target, exc)
        ok = _current_pkg() == self.IG_PACKAGE
        if not ok:
            _emit(logger, "warning", "u2: could not return to Instagram for %s (foreground=%r)", target, _current_pkg() or "unknown")
        return ok

    def _dismiss_edit_app_popup_u2(self, d, target, emit, logger=None) -> None:
        """After the reel media is selected, Instagram may push its 'Edits'
        video-editing app -- either as an in-app promo that covers the Next
        button, or by deep-linking to the Google Play Store install page (which
        takes IG out of the foreground entirely). Recover from the Play Store
        first, then clear any in-app promo (dismiss control -> Back -> tap
        outside), re-checking Next after each attempt.
        """
        _emit(logger, "info", "u2: checking for the 'Edits' editing-app pop-up for %s", target)

        # If the promo bounced us out to the Play Store (or any other app), get
        # back into Instagram before doing anything else.
        self._recover_to_instagram_u2(d, target, emit, logger)

        # Give the normal composer time to render Next. The promo appears
        # immediately, so if Next shows within this window there's no pop-up.
        if self._first_present(
            d,
            [{"text": "Next"}, {"description": "Next"}, {"textMatches": "(?i)^next$"}],
            timeout=6,
            logger=logger,
            purpose="Next (post-media check)",
        ) is not None:
            _emit(logger, "info", "u2: Next is visible; no editing-app pop-up to dismiss for %s", target)
            return

        # Next isn't reachable -> something is covering it. Log any promo marker.
        promo = _u2_find(
            d,
            [
                {"textContains": "Edits"},
                {"descriptionContains": "Edits"},
                {"textContains": "edit your"},
                {"textContains": "editing app"},
                {"textContains": "new way to edit"},
            ],
            logger=logger,
            purpose="'Edits' editing-app promo marker",
        )
        if promo is None:
            _emit(logger, "info", "u2: no explicit promo marker, but Next is hidden; attempting to dismiss an overlay for %s", target)

        # 1) Explicit dismiss/close control, if the promo offers one.
        if _u2_click(
            d,
            [
                {"textMatches": "(?i)^(not now|maybe later|dismiss|skip|no thanks|no, thanks)$"},
                {"descriptionMatches": "(?i)^(close|dismiss)$"},
            ],
            logger=logger,
            purpose="editing-app pop-up dismiss control",
        ):
            waits.settle(1.2, ready=lambda: self._next_visible_u2(d),
                         logger=logger, what="Next button back after dismissing the pop-up")
            if self._next_visible_u2(d):
                _emit(logger, "info", "u2: Next reappeared after tapping a dismiss control for %s", target)
                return

        # 2) Hardware Back -- closes a bottom sheet / interstitial without
        #    leaving the composer.
        _emit(logger, "info", "u2: pressing Back to close the editing-app pop-up for %s", target)
        try:
            d.press("back")
        except Exception as exc:
            _emit(logger, "warning", "u2: Back press failed for %s: %s", target, exc)
        waits.settle(1.2, ready=lambda: self._next_visible_u2(d),
                     logger=logger, what="Next button back after dismissing the pop-up")
        if self._next_visible_u2(d):
            _emit(logger, "info", "u2: Next reappeared after Back for %s", target)
            return

        # 3) Tap outside the sheet (top-centre, clear of any buttons).
        _emit(logger, "info", "u2: tapping outside (top of screen) to dismiss the pop-up for %s", target)
        try:
            d.click(0.5, 0.06)
        except Exception as exc:
            _emit(logger, "warning", "u2: tap-outside failed for %s: %s", target, exc)
        waits.settle(1.2, ready=lambda: self._next_visible_u2(d),
                     logger=logger, what="Next button back after tapping outside")

        if self._next_visible_u2(d):
            _emit(logger, "info", "u2: Next is visible after dismissing the pop-up for %s", target)
        else:
            _emit(logger, "warning", "u2: Next still not visible after pop-up dismissal attempts for %s", target)

    def _tap_next_u2(self, d, target, emit, logger=None) -> bool:
        # The Next control sits bottom-right in both composer screens.
        return self._tap_advance_u2(d, target, emit, logger, selectors=self._NEXT_SELECTORS,
                                    purpose="Next button")

    def _tap_advance_u2(self, d, target, emit, logger=None, selectors=None, purpose="advance button") -> bool:
        """Tap the button that moves the composer forward. Defaults to the wider
        Next/Continue set, since the second step isn't always called 'Next'."""
        return _u2_click(
            d,
            list(selectors if selectors is not None else self._ADVANCE_SELECTORS),
            logger=logger,
            purpose=purpose,
            fallback_ratio=(0.90, 0.92),
        )

    def _enter_caption_u2(self, d, target, adb_client, caption, emit, logger=None) -> bool:
        # On the reel share screen "Write a caption..." is usually a placeholder
        # that must be tapped to focus/open the real editable field. Tapping it
        # first is more reliable than writing to whatever EditText happens to be
        # on screen (which is why set_text alone could "click but type nothing").
        _u2_click(
            d,
            [
                {"textContains": "Write a caption"},
                {"descriptionContains": "Write a caption"},
                {"textContains": "Add a caption"},
                {"descriptionContains": "Add a caption"},
            ],
            logger=logger,
            purpose="caption field placeholder",
        )
        time.sleep(1.0)

        # Locate the editable field, preferring the focused one.
        field = None
        for kwargs in (
            {"focused": True, "className": "android.widget.EditText"},
            {"className": "android.widget.EditText"},
            {"focused": True},
        ):
            sel = d(**kwargs)
            try:
                present = sel.exists
            except Exception:
                present = False
            if present:
                field = sel
                _emit(logger, "info", "u2: caption editable field via %s -> %s", kwargs, _u2_describe(sel))
                break
        if field is None:
            _emit(logger, "warning", "u2: caption input field not found for %s", target)
            return False

        def _current_text() -> str:
            try:
                return (field.get_text() or "").strip()
            except Exception:
                return ""

        def _stuck() -> bool:
            got = _current_text()
            needle = caption.strip()[:15].lower()
            return bool(needle and needle in got.lower())

        # Attempt 1: uiautomator2 set_text (handles unicode/emoji).
        _emit(logger, "info", "u2: setting reel caption via set_text: %r", caption)
        try:
            field.set_text(caption)
        except Exception as exc:
            _emit(logger, "warning", "u2: set_text failed for caption: %s", exc)
        time.sleep(0.8)
        if _stuck():
            _emit(logger, "info", "u2: caption confirmed via set_text -> %r", _current_text())
            return True
        _emit(logger, "info", "u2: caption not stuck after set_text (field reads %r); falling back to adb input text", _current_text())

        # Attempt 2: adb `input text` into the focused field -- the method the
        # dump/OCR flow used successfully. Re-tap the field to ensure focus.
        try:
            field.click()
            time.sleep(0.4)
        except Exception:
            pass
        try:
            adb_client.run_command(f"adb -s {target} shell {write_text(caption)}")
        except Exception as exc:
            _emit(logger, "warning", "u2: adb 'input text' fallback failed for caption: %s", exc)
        time.sleep(0.8)
        if _stuck():
            _emit(logger, "info", "u2: caption confirmed via adb input text -> %r", _current_text())
            return True

        # Attempt 3: uiautomator2 IME send_keys.
        try:
            _emit(logger, "info", "u2: trying send_keys (u2 IME) for caption")
            d.send_keys(caption, clear=False)
            time.sleep(0.8)
            if _stuck():
                _emit(logger, "info", "u2: caption confirmed via send_keys -> %r", _current_text())
                return True
        except Exception as exc:
            _emit(logger, "warning", "u2: send_keys fallback failed for caption: %s", exc)

        _emit(logger, "warning", "u2: could not confirm caption text was entered for %s (field reads %r)", target, _current_text())
        return False

    def _tap_share_u2(self, d, target, emit, logger=None) -> bool:
        # Make sure the Edits promo didn't bounce us to the Play Store before the
        # final post.
        self._recover_to_instagram_u2(d, target, emit, logger)
        # The real posting button carries a visible "Share" text label; prefer
        # that over a bare share glyph (content-desc only).
        return _u2_click(
            d,
            list(self._SHARE_SELECTORS),
            logger=logger,
            purpose="Share button",
            fallback_ratio=(0.90, 0.94),
        )

    def _tap_home_u2(self, d, logger=None) -> bool:
        return _u2_click(
            d,
            [{"description": "Home"}, {"descriptionStartsWith": "Home"}],
            logger=logger,
            purpose="Home tab",
        )

    def _tap_ig_home_icon_u2(self, d, target, emit, logger=None) -> bool:
        """Tap Instagram's bottom-nav Home (house) icon. Falls back to the
        bottom-LEFT position (the house is the leftmost tab) -- deliberately NOT
        the Android home key, so we stay inside Instagram to see the post
        confirmation."""
        _emit(logger, "info", "u2: tapping the IG Home (house) icon for %s", target)
        # Scope to IG's own feed_tab resource-id: the Android system Home button
        # (com.android.launcher3:id/home) ALSO carries content-desc "Home", and
        # tapping it backgrounds Instagram.
        return _u2_click(
            d,
            [
                {"resourceId": "com.instagram.android:id/feed_tab"},
                {"resourceIdMatches": r"com\.instagram\.android:id/(feed_tab|main_home_tab)"},
                {"description": "Home", "resourceIdMatches": r"com\.instagram\.android:id/.*"},
            ],
            logger=logger,
            purpose="IG Home (house) icon",
            fallback_ratio=(0.08, 0.95),
        )

    # Text that Instagram shows once a reel finishes posting -- a small banner /
    # toast with a celebratory message. Matched case-insensitively.
    # Reel-specific / celebratory only -- bare "posted"/"shared" also appear on
    # the home feed and would false-positive a success while we sit on the feed.
    _POST_CONFIRMATION_PHRASES = (
        "your reel",
        "reel shared",
        "reel was shared",
        "reel is being shared",
        "reel posted",
        "high five",
        "thumbs up",
        "nice work",
        "way to go",
        "great job",
    )

    # Ways to ask Android what's in front. `dumpsys window displays` doesn't
    # carry the focus lines on every build (it reported nothing on the MLX
    # phones), so try the activity manager first and fall back.
    _FOREGROUND_COMMANDS = (
        "dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity'",
        "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'",
        "dumpsys window displays | grep -E 'mCurrentFocus|mFocusedApp'",
    )

    def _ig_is_foreground(self, target, adb_client) -> bool:
        """True when Instagram is the app in front. Used before uiautomator2 is
        connected; once u2 is up we check for real UI instead, which is both
        faster and more meaningful."""
        for shell in self._FOREGROUND_COMMANDS:
            try:
                out = adb_client.run_command(f"adb -s {target} shell {shell}")
            except Exception:
                continue
            if out and self.IG_PACKAGE in out:
                return True
        return False

    # --- post verification probes ----------------------------------------
    # Fed to reel_verify.verify_reel_posted. Each degrades to None/"" rather
    # than raising, so a probe that can't read its signal simply doesn't vote.

    def _read_post_count_u2(self, d, target, logger=None):
        """The account's own post count, read from the profile header.

        Instagram's resource ids for the counters move between builds, so try a
        few, then fall back to any node whose text/content-desc looks like
        "<n> posts". Returns a reel_verify.Count or None.
        """
        for kwargs in self._POST_COUNT_SELECTORS:
            try:
                node = d(**kwargs)
                if node.exists:
                    parsed = reel_verify.parse_count(node.info.get("text"))
                    if parsed is not None:
                        return parsed
            except Exception:
                continue
        # Content-desc form: "12 posts".
        try:
            node = d(descriptionMatches=r"(?i)^\s*[\d.,\s]+\s*(posts?|beitr).*")
            if node.exists:
                parsed = reel_verify.parse_count(node.info.get("contentDescription"))
                if parsed is not None:
                    return parsed
        except Exception:
            pass
        _emit(logger, "info", "u2: could not read the post count for %s", target)
        return None

    def _browse_and_refresh_profile_u2(self, d, target, logger=None) -> bool:
        """Move through the feed and back to the profile so its post count is
        actually re-fetched.

        Sitting on the profile screen doesn't refresh it -- and re-tapping the
        Profile tab while already there just scrolls to top, so a new post can
        stay invisible for a minute or more. Leaving to the feed and coming back
        forces a re-render, and a pull-to-refresh on the grid forces a re-fetch.
        The little scroll on the way is also just what a person would do.
        """
        # Instagram likes to interrupt right after a post ("Rate Instagram",
        # "Turn on notifications", ...). Such a dialog sits over the nav bar and
        # would swallow every tap below, so the count could never update and the
        # post would be reported unconfirmed. Clear it first.
        self._dismiss_popups_u2(d, logger=logger, max_rounds=2)

        try:
            width, height = d.window_size()
        except Exception:
            width, height = (1080, 2340)
        mid_x = int(width * 0.5)

        # Feed: one pull-down. This used to scroll away and then scroll back,
        # but the second gesture only undid the first -- what actually forces the
        # count to update is leaving for the feed and returning to the profile
        # below. One pull at the top of the feed reloads it and reads as ordinary
        # browsing, at half the gesture cost. This probe runs every ~25s for up
        # to three minutes, so the saving repeats.
        self._tap_ig_home_icon_u2(d, target, lambda *a, **k: None, logger)
        waits.settle(1.0)
        try:
            d.swipe(mid_x, int(height * 0.35), mid_x, int(height * 0.72), 0.35)
            waits.settle(0.6)
        except Exception:
            pass

        # Back to the profile -- this is the re-render that updates the count.
        if not self._open_profile_tab_u2(d, target, logger=logger):
            return False
        waits.settle(1.5, ready=waits.u2_ready(d, *self._POST_COUNT_SELECTORS),
                     logger=logger, what="profile header")

        # Pull-to-refresh so the grid re-fetches rather than showing a cached page.
        try:
            d.swipe(mid_x, int(height * 0.35), mid_x, int(height * 0.72), 0.4)
            waits.settle(1.5)
        except Exception:
            pass
        return True

    def _profile_post_count_probe_u2(self, d, target, emit, logger=None, every_seconds: float = 25.0,
                                     initial_delay: float | None = None):
        """A throttled post-count probe. Each check browses feed -> profile and
        pulls to refresh (see :meth:`_browse_and_refresh_profile_u2`), which is
        too expensive to do on every poll -- so it only really looks every
        `every_seconds` and returns None in between (None = 'no opinion').

        It also holds off for `initial_delay` (default: one full interval) before
        the *first* check. That delay is the point, not a detail: the seconds
        right after Share are when the confirmation banner is on screen, and
        this probe navigates away from the feed to read the profile. It used to
        start from `last = 0.0`, so the very first poll cleared the throttle
        instantly and walked off the feed before the banner had ever been read.
        Letting the cheap screen probe own that window costs nothing -- the
        counter is cached on Instagram's side and rarely moves that fast anyway.
        """
        wait_first = every_seconds if initial_delay is None else initial_delay
        state = {"next": time.time() + wait_first}

        def probe():
            now = time.time()
            if now < state["next"]:
                return None
            state["next"] = now + every_seconds
            if not self._browse_and_refresh_profile_u2(d, target, logger=logger):
                return None
            return self._read_post_count_u2(d, target, logger=logger)

        return probe

    def _open_profile_tab_u2(self, d, target, logger=None) -> bool:
        """Tap the bottom-nav Profile tab (IG's own, not the launcher)."""
        selectors = (
            {"resourceId": "com.instagram.android:id/profile_tab"},
            {"resourceIdMatches": r"com\.instagram\.android:id/(profile_tab|main_profile_tab)"},
        )
        for kwargs in selectors:
            try:
                node = d(**kwargs)
                if node.exists:
                    node.click()
                    return True
            except Exception:
                continue
        _emit(logger, "info", "u2: profile tab not found for %s", target)
        return False

    def _screen_text_probe_u2(self, d, target, logger=None):
        """All visible text + any transient toast, lowercased, for the screen
        classifier (banner / error dialog / draft prompt / composer)."""
        def probe():
            parts = []
            try:
                parts.append(d.dump_hierarchy() or "")
            except Exception:
                pass
            try:
                toast = d.toast.get_message(0.2, 0.2, "")
                if toast:
                    parts.append(str(toast))
            except Exception:
                pass
            return " ".join(parts).lower()

        return probe

    def _notification_probe(self, target, adb_client, logger=None):
        return _make_notification_probe(target, adb_client, logger=logger)

    def _wait_for_post_confirmation_u2(self, d, target, emit, logger=None, timeout: int = 45, should_stop=None) -> bool:
        """Poll for the 'reel was posted' confirmation banner/toast and alert on
        the logs when it appears. Keeps Instagram open the whole time. Returns
        True if detected within `timeout`, False on timeout or abort."""
        import re as _re

        phrases = self._POST_CONFIRMATION_PHRASES
        _emit(logger, "info", "u2: waiting up to %ss for a reel-posted confirmation (%s) for %s ...", timeout, ", ".join(phrases), target)
        deadline = time.time() + timeout
        consecutive_errors = 0
        while time.time() < deadline:
            if callable(should_stop) and should_stop():
                _emit(logger, "info", "u2: abort requested while waiting for reel post confirmation for %s", target)
                return False
            try:
                for phrase in phrases:
                    pattern = f"(?i).*{_re.escape(phrase)}.*"
                    for kwargs in ({"textMatches": pattern}, {"descriptionMatches": pattern}):
                        if d(**kwargs).exists:
                            _emit(logger, "info", "u2: >>> REEL POST CONFIRMED for %s -- detected %r via %s", target, phrase, kwargs)
                            return True
                # Also check for a transient system toast carrying the same words.
                toast_msg = d.toast.get_message(0.5, 0.5, "")
                if toast_msg:
                    low = str(toast_msg).lower()
                    for phrase in phrases:
                        if phrase in low:
                            _emit(logger, "info", "u2: >>> REEL POST CONFIRMED for %s -- toast %r matched %r", target, toast_msg, phrase)
                            return True
                consecutive_errors = 0
            except Exception as exc:
                # The Multilogin cloud phone / ADB tunnel can drop during the long
                # wait. Don't crash the flow -- back off, and bail after a few
                # consecutive drops (the reel may have posted; we just can't see).
                consecutive_errors += 1
                _emit(logger, "warning", "u2: error polling for reel confirmation for %s (%s/3): %s", target, consecutive_errors, exc)
                if consecutive_errors >= 3:
                    _emit(logger, "warning", "u2: device/uiautomator connection lost while waiting for confirmation for %s; the reel may have posted but can't be confirmed", target)
                    return False

            time.sleep(1.5)

        _emit(logger, "warning", "u2: no reel-posted confirmation text detected within %ss for %s", timeout, target)
        return False

    def _first_present(self, d, selectors, timeout=None, logger=None, purpose="element"):
        wait_seconds = self.SELECTOR_WAIT_SECONDS if timeout is None else timeout
        _emit(logger, "info", "u2: waiting up to %.0fs for %s ...", wait_seconds, purpose)
        deadline = time.time() + wait_seconds
        while True:
            for kwargs in selectors:
                sel = d(**kwargs)
                try:
                    present = sel.exists
                except Exception as exc:
                    _emit(logger, "warning", "u2:   selector %s errored while waiting for %s: %s", kwargs, purpose, exc)
                    continue
                if present:
                    _emit(logger, "info", "u2: %s appeared via %s -> %s", purpose, kwargs, _u2_describe(sel))
                    return sel
            if time.time() >= deadline:
                _emit(logger, "info", "u2: %s did not appear within %.0fs", purpose, wait_seconds)
                return None
            time.sleep(0.4)


class ReelPostCountProbeFlow(InstagramReelUploadU2Flow):
    """Read an account's post count. Posts nothing, uploads nothing, taps no
    Share button.

    This is the device half of the deferred recheck: fifteen minutes after a
    post we could not confirm, something has to go and look. It exists as a flow
    rather than as ad-hoc ADB calls so it goes through the same launch ->
    connect -> shut down lifecycle as everything else (profile locking, MLX
    launch, readiness retries) instead of reimplementing that badly.

    Subclasses the upload flow purely to reuse its profile-navigation helpers --
    `_browse_and_refresh_profile_u2` in particular, which knows that sitting on
    the profile does not refresh the counter. `run` is fully overridden; nothing
    of the upload path executes.
    """

    name = "reel_post_count_probe"

    def get_progress_total_steps(self, target: str) -> int:
        return 2

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

        log = logger
        target = profile.target

        def emit(level: str, message: str, *args) -> None:
            _emit(log, level, message, *args)

        def mark_step() -> None:
            if hasattr(adb_client, "mark_progress_step"):
                adb_client.mark_progress_step()

        if u2 is None:
            emit("warning", "uiautomator2 is not importable; cannot probe the post count for %s", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "post_count": None}

        if callable(should_stop) and should_stop():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        try:
            d = u2.connect(target)
            d.implicitly_wait(self.SELECTOR_WAIT_SECONDS)
        except Exception as exc:
            emit("warning", "uiautomator2 could not connect to %s: %s", target, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False,
                    "success": False, "post_count": None}

        waits.settle(
            10,
            ready=waits.u2_ready(
                d,
                {"resourceId": "com.instagram.android:id/feed_tab"},
                {"resourceIdMatches": r"com\.instagram\.android:id/.*(tab_bar|profile_tab).*"},
            ),
            logger=log, what="Instagram UI loaded",
        )
        mark_step()

        # Same browse-and-refresh as the in-run probe: leaving for the feed and
        # coming back is what actually re-fetches the counter. Cheap here, since
        # unlike the in-run path nothing is racing us.
        self._browse_and_refresh_profile_u2(d, target, logger=log)
        count = self._read_post_count_u2(d, target, logger=log)
        mark_step()

        emit("info", "Post-count probe for %s: %s", target,
             f"{count.value} (exact={count.exact})" if count else "unreadable")
        return {
            "profile_id": profile.id,
            "target": target,
            "aborted": False,
            "success": count is not None,
            "post_count": count.value if count else None,
            "post_count_exact": bool(count.exact) if count else False,
        }
