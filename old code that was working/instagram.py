import hashlib
import random
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import os
import shutil
import xml.etree.ElementTree as ET

if __package__ in {None, ""}:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - environment fallback
    cv2 = None
    np = None

try:
    import pytesseract
except ImportError:  # pragma: no cover - optional OCR dependency
    pytesseract = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional image dependency
    Image = None

from adb_bot.core.adb_commands import back, home, swipe, tap, write_text
from adb_bot.core.models import Profile
from adb_bot.automation.flows.story_media import StoryMediaQueueManager, discover_story_media_files, get_story_media_queue


def _emit(logger, level, message, *args) -> None:
    if logger is None:
        if args:
            message = message % args
        print(message)
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message, *args)
    else:
        if args:
            message = message % args
        print(message)


def _adb_capture_ui_dump(target: str, logger=None):
    dump_remote = "/sdcard/instagram_ui_dump.xml"
    dump_local = Path(tempfile.gettempdir()) / f"instagram_ui_dump_{target}.xml"

    try:
        _emit(logger, "info", "Dumping UI hierarchy for %s", target)
        result = subprocess.run(
            f"adb -s {target} shell uiautomator dump {dump_remote}",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            _emit(logger, "info", "uiautomator dump stdout for %s: %s", target, result.stdout.strip())
        if result.stderr.strip():
            _emit(logger, "info", "uiautomator dump stderr for %s: %s", target, result.stderr.strip())

        result = subprocess.run(
            f"adb -s {target} pull {dump_remote} {dump_local}",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            _emit(logger, "info", "adb pull stdout for %s: %s", target, result.stdout.strip())
        if result.stderr.strip():
            _emit(logger, "info", "adb pull stderr for %s: %s", target, result.stderr.strip())
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else exc.stdout.strip() if exc.stdout else str(exc)
        _emit(logger, "warning", "Unable to capture UI dump for %s: %s", target, stderr)
        return None

    try:
        tree = ET.parse(dump_local)
        return tree.getroot()
    except Exception as exc:
        _emit(logger, "warning", "Failed to parse UI dump for %s: %s", target, exc)
        return None
    finally:
        try:
            dump_local.unlink(missing_ok=True)
        except OSError as exc:
            _emit(logger, "warning", "Failed to remove local UI dump file for %s: %s", target, exc)


def _adb_is_instagram_home_feed_visible(target: str, logger=None) -> bool:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return False

    has_home_tab_selected = False
    has_home_nav_present = False
    has_profile_tab_selected = False
    has_reels_tab_selected = False
    has_stories_row = False
    has_feed_resource = False
    has_feed_content = False

    def _is_bottom_nav_element(resource_id: str, node_class: str) -> bool:
        lower_id = resource_id.lower()
        lower_class = node_class.lower()
        bottom_nav_tokens = (
            "nav_home",
            "bottomnavigation",
            "bottom_navigation",
            "navigationbar",
            "tab",
        )
        return any(token in lower_id for token in bottom_nav_tokens) or any(token in lower_class for token in bottom_nav_tokens)

    def _is_feed_post_candidate(combined: str, node_class: str) -> bool:
        if "android.widget" not in node_class and "androidx." not in node_class:
            return False
        return any(token in combined for token in ("like", "comment", "share", "save", "photo", "image", "reel", "post"))

    for node in root.iter():
        resource_id = node.attrib.get("resource-id", "").lower()
        node_class = node.attrib.get("class", "").lower()
        combined = " ".join(
            str(node.attrib.get(key, "")) for key in ("content-desc", "text", "resource-id", "class")
        ).lower()
        if not any(key in combined for key in ("home", "feed", "stories", "post", "photo", "image", "reel", "like", "comment", "share", "save")):
            continue

        selected = node.attrib.get("selected", "false").lower()
        checked = node.attrib.get("checked", "false").lower()
        is_bottom_nav = _is_bottom_nav_element(resource_id, node_class)

        if is_bottom_nav and "home" in combined:
            has_home_nav_present = True

        if selected in {"true", "1"} or checked in {"true", "1"}:
            if is_bottom_nav and "home" in combined:
                has_home_tab_selected = True
            elif is_bottom_nav and any(token in combined for token in ("profile", "person", "account")):
                has_profile_tab_selected = True
            elif is_bottom_nav and "reel" in combined:
                has_reels_tab_selected = True

        if "stories" in combined and not is_bottom_nav:
            has_stories_row = True

        if ("feed" in resource_id or "recyclerview" in node_class) and not is_bottom_nav and "profile" not in resource_id:
            has_feed_resource = True

        if _is_feed_post_candidate(combined, node_class) and not is_bottom_nav:
            has_feed_content = True

    if has_profile_tab_selected or has_reels_tab_selected:
        _emit(logger, "info", "Instagram home feed not detected for %s because a non-home tab is selected", target)
        return False

    if not has_home_tab_selected and not has_home_nav_present:
        _emit(logger, "info", "Instagram home feed not detected for %s because the home tab is not present", target)
        return False

    if has_home_tab_selected and has_stories_row:
        _emit(logger, "info", "Instagram home feed appears visible for %s by selected home tab and stories row", target)
        return True

    if has_home_tab_selected and has_feed_content:
        _emit(logger, "info", "Instagram home feed appears visible for %s by selected home tab and feed content", target)
        return True

    if has_home_tab_selected and has_feed_resource:
        _emit(logger, "info", "Instagram home feed appears visible for %s by selected home tab and feed resource-id", target)
        return True

    if has_home_nav_present and (has_stories_row or has_feed_resource or has_feed_content):
        _emit(logger, "info", "Instagram home feed appears visible for %s by home nav presence and feed markers", target)
        return True

    _emit(logger, "info", "Instagram home feed not detected for %s", target)
    return False


def _adb_is_device_screen_on(target: str, adb_client, logger=None) -> bool:
    if adb_client is None:
        return False

    _emit(logger, "info", "Checking device screen state for %s", target)
    output = adb_client.run_command(f"adb -s {target} shell dumpsys power")
    if not output:
        _emit(logger, "warning", "Unable to read power state for %s", target)
        return False

    normalized = output.lower()
    if "mwakefulness=awake" in normalized or "wakefulness=awake" in normalized:
        return True
    if "state=on" in normalized or "state on" in normalized:
        return True
    if "mScreenOn=true" in normalized or "screen on" in normalized or "screenstate=on" in normalized:
        return True
    if "mWakefulness=dozing" in normalized or "wakefulness=dozing" in normalized:
        return False
    if "mScreenOn=false" in normalized or "screen off" in normalized or "screenstate=off" in normalized:
        return False

    return False


def _adb_ensure_instagram_feed_visible(target: str, adb_client, logger=None, max_attempts: int = 2) -> bool:
    _emit(logger, "info", "Ensuring Instagram feed is visible for %s", target)
    if _adb_is_instagram_home_feed_visible(target, logger=logger):
        _emit(logger, "info", "Instagram feed verification succeeded immediately for %s", target)
        return True

    _emit(logger, "warning", "Instagram feed verification failed for %s; manual confirmation needed", target)
    return False


def _adb_get_screen_size(target: str, logger=None) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            f"adb -s {target} shell wm size",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        output = (result.stdout or result.stderr or "").strip()
        match = re.search(r"(\d+)[xX](\d+)", output)
        if match:
            width = int(match.group(1))
            height = int(match.group(2))
            return width, height
        _emit(logger, "warning", "Unable to parse screen size output for %s: %s", target, output)
        return None
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else exc.stdout.strip() if exc.stdout else str(exc)
        _emit(logger, "warning", "Failed to read screen size for %s: %s", target, stderr)
        return None


def _adb_get_relative_point(target: str, x_fraction: float, y_fraction: float, logger=None) -> tuple[int, int]:
    screen_size = _adb_get_screen_size(target, logger=logger)
    if screen_size is None:
        width, height = 1080, 2340
    else:
        width, height = screen_size

    x = int(round(width * x_fraction))
    y = int(round(height * y_fraction))
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    return x, y


def _adb_find_ui_element_center(target: str, keywords: tuple[str, ...], logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    normalized_keywords = tuple(keyword.lower() for keyword in keywords if keyword)
    if not normalized_keywords:
        return None

    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if not any(keyword in combined for keyword in normalized_keywords):
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1 = int(match.group(1))
        y1 = int(match.group(2))
        x2 = int(match.group(3))
        y2 = int(match.group(4))
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    return None


def _adb_find_instagram_home_button_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    bottom_nav_tokens = ("nav_home", "bottomnavigation", "bottom_navigation", "tab", "home")
    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if "home" not in combined:
            continue

        resource_id = attrs.get("resource-id", "").lower()
        node_class = attrs.get("class", "").lower()
        if not any(token in resource_id for token in bottom_nav_tokens) and not any(token in node_class for token in bottom_nav_tokens):
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1 = int(match.group(1))
        y1 = int(match.group(2))
        x2 = int(match.group(3))
        y2 = int(match.group(4))
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    return None


def _adb_find_instagram_notifications_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen
    top_right_min_x = int(width * 0.55)
    top_right_max_y = int(height * 0.30)

    notification_tokens = (
        "notification",
        "notif",
        "bell",
        "inbox",
        "activity",
        "notifications",
        "heart",
        "love",
        "♥",
        "♡",
    )

    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if not any(token in combined for token in notification_tokens):
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1 = int(match.group(1))
        y1 = int(match.group(2))
        x2 = int(match.group(3))
        y2 = int(match.group(4))
        if x2 < top_right_min_x or y2 > top_right_max_y:
            continue
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    for node in root.iter():
        attrs = node.attrib
        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1 = int(match.group(1))
        y1 = int(match.group(2))
        x2 = int(match.group(3))
        y2 = int(match.group(4))
        if x1 < top_right_min_x or y2 > top_right_max_y:
            continue

        node_class = attrs.get("class", "").lower()
        resource_id = attrs.get("resource-id", "").lower()
        if "imageview" in node_class or "icon" in node_class or "notification" in resource_id:
            clickable = attrs.get("clickable", "false").lower() == "true"
            if clickable:
                return ((x1 + x2) // 2, (y1 + y2) // 2)

    return None


def _adb_resolve_story_media_path(logger=None) -> str | None:
    # Prefer an explicitly saved path from settings
    try:
        from adb_bot.config.settings import load_settings
        settings = load_settings()
        saved = (settings.get("story_media_path") or "").strip()
        if saved:
            # Normalize saved path (expand ~) for cross-platform compatibility
            saved_path = Path(saved).expanduser()
            if saved_path.exists() and saved_path.is_file():
                _emit(logger, "info", "Using story upload image from settings: %s", saved_path)
                return str(saved_path)
            if saved_path.exists() and saved_path.is_dir():
                discovered = discover_story_media_files(saved_path, logger=logger)
                if discovered:
                    _emit(logger, "info", "Using story upload media folder from settings: %s", saved_path)
                    return str(saved_path)
                _emit(logger, "warning", "Saved story media folder has no supported files: %s", saved)
            else:
                _emit(logger, "warning", "Saved story media path is invalid: %s", saved)
    except Exception:
        pass

    candidate_names = [
        # image candidates
        "instagram_story_upload.jpg",
        "instagram_story_upload.png",
        "story_upload.jpg",
        "story_upload.png",
        "instagram_story.jpg",
        "story.jpg",
        # video candidates
        "instagram_story_upload.mp4",
        "instagram_story_upload.mov",
        "story_upload.mp4",
        "story_upload.mov",
        "instagram_story.mp4",
        "story.mp4",
    ]
    search_paths = [
        Path.cwd(),
        Path(__file__).resolve().parents[3],
        Path(__file__).resolve().parents[3] / "assets",
        Path(__file__).resolve().parents[3] / "images",
    ]

    for base in search_paths:
        for name in candidate_names:
            path = base / name
            if path.exists() and path.is_file():
                _emit(logger, "info", "Using story upload image from %s", path)
                return str(path)

    _emit(logger, "warning", "No story upload image found for story upload flow in search paths")
    return None


def _adb_push_media_to_device(target: str, local_media_path: str, remote_media_path: str, logger=None) -> bool:
    local_path = Path(local_media_path)
    if not local_path.exists() or not local_path.is_file():
        _emit(logger, "warning", "Local story media file does not exist: %s", local_media_path)
        return False

    try:
        remote_dir = os.path.dirname(remote_media_path)
        _emit(logger, "info", "Ensuring remote directory %s exists on %s", remote_dir, target)
        mkdir_result = subprocess.run(
            [
                "adb",
                "-s",
                target,
                "shell",
                "mkdir",
                "-p",
                remote_dir,
            ],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        if mkdir_result.returncode != 0:
            _emit(logger, "warning", "Failed to create remote directory %s on %s: %s", remote_dir, target, (mkdir_result.stderr or mkdir_result.stdout or "<no output>").strip())
            return False

        _emit(logger, "info", "Pushing local story media %s to %s on %s", local_media_path, remote_media_path, target)
        result = subprocess.run(
            [
                "adb",
                "-s",
                target,
                "push",
                local_media_path,
                remote_media_path,
            ],
            shell=False,
            check=True,
            capture_output=True,
            text=True,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if stdout:
            _emit(logger, "info", "adb push stdout for %s: %s", target, stdout)
        if stderr:
            _emit(logger, "info", "adb push stderr for %s: %s", target, stderr)
        _emit(logger, "info", "Refreshing media scanner for %s", remote_media_path)
        quoted_uri = shlex.quote(f"file://{remote_media_path}")
        scan_command = [
            "adb",
            "-s",
            target,
            "shell",
            "am",
            "broadcast",
            "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d",
            quoted_uri,
        ]
        _emit(logger, "info", "Executing media scanner command: %s", " ".join(scan_command))
        scan_result = subprocess.run(
            scan_command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        if scan_result.stdout.strip():
            _emit(logger, "info", "Media scanner stdout for %s: %s", target, scan_result.stdout.strip())
        if scan_result.stderr.strip():
            _emit(logger, "info", "Media scanner stderr for %s: %s", target, scan_result.stderr.strip())
        _emit(logger, "info", "adb push completed for %s", target)
        return True
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else exc.stdout.strip() if exc.stdout else str(exc)
        _emit(logger, "warning", "Failed to push story media to %s: %s", target, stderr)
        return False
    except Exception as exc:
        _emit(logger, "warning", "Unexpected error during adb push to %s: %s", target, exc)
        return False


def _adb_verify_remote_media_matches_local(target: str, local_media_path: str, remote_media_path: str, logger=None) -> bool:
    """Verify the remote media has the same content hash as the local file."""
    local_path = Path(local_media_path)
    if not local_path.exists() or not local_path.is_file():
        _emit(logger, "warning", "Local file is missing, cannot verify remote media hash for %s", local_media_path)
        return False

    digest = hashlib.sha256()
    try:
        with local_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        local_hash = digest.hexdigest()
    except Exception as exc:
        _emit(logger, "warning", "Unable to compute local hash for %s: %s", local_media_path, exc)
        return False

    try:
        quoted_remote_path = shlex.quote(remote_media_path)
        hash_command = [
            "adb",
            "-s",
            target,
            "shell",
            "sha256sum",
            quoted_remote_path,
        ]
        _emit(logger, "info", "Executing remote hash command: %s", " ".join(hash_command))
        result = subprocess.run(
            hash_command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        output = (result.stdout or result.stderr or "").strip()
        _emit(logger, "info", "Remote hash returncode=%s stdout=%s stderr=%s", result.returncode, result.stdout.strip(), result.stderr.strip())
        if not output:
            _emit(logger, "warning", "Remote hash verification returned no output for %s", remote_media_path)
            return False

        remote_hash = output.split()[0].strip()
        if remote_hash.lower() == local_hash.lower():
            _emit(logger, "info", "Remote media hash matches local file for %s", remote_media_path)
            return True

        _emit(logger, "warning", "Remote media hash mismatch for %s (expected %s, got %s)", remote_media_path, local_hash, remote_hash)
        return False
    except Exception as exc:
        _emit(logger, "warning", "Error verifying remote media hash on %s: %s", target, exc)
        return False


def _adb_verify_remote_media_exists(target: str, remote_media_path: str, logger=None) -> bool:
    """Verify the remote media file exists and has non-zero size on the device."""
    try:
        quoted_remote_path = shlex.quote(remote_media_path)
        ls_command = [
            "adb",
            "-s",
            target,
            "shell",
            "ls",
            "-l",
            quoted_remote_path,
        ]
        _emit(logger, "info", "Executing remote ls command: %s", " ".join(ls_command))
        result = subprocess.run(
            ls_command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        output = (result.stdout or result.stderr or "").strip()
        _emit(logger, "info", "Remote ls returncode=%s stdout=%s stderr=%s", result.returncode, result.stdout.strip(), result.stderr.strip())
        if not output:
            _emit(logger, "info", "Remote media check returned no output for %s", remote_media_path)
        else:
            _emit(logger, "info", "Remote media ls output for %s: %s", remote_media_path, output)
            # Typical ls -l line: -rw-r--r-- 1 shell shell 12345 2026-07-06 12:34 /sdcard/...
            size_match = re.search(r"\s(\d+)\s+/.+", output) or re.search(r"\s(\d+)\s+\d{4}-\d{2}-\d{2}", output)
            if size_match:
                try:
                    size = int(size_match.group(1))
                    if size > 0:
                        _emit(logger, "info", "Remote media exists on %s with size %s", target, size)
                        return True
                    _emit(logger, "warning", "Remote media exists but has zero size on %s", target)
                    return False
                except Exception:
                    pass

        # As a fallback, try test -e and then stat -c%s
        fallback_command = [
            "adb",
            "-s",
            target,
            "shell",
            "test",
            "-e",
            quoted_remote_path,
            "&&",
            "stat",
            "-c",
            "%s",
            quoted_remote_path,
        ]
        _emit(logger, "info", "Executing remote fallback command: %s", " ".join(fallback_command))
        result2 = subprocess.run(
            fallback_command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
        _emit(logger, "info", "Remote fallback returncode=%s stdout=%s stderr=%s", result2.returncode, result2.stdout.strip(), result2.stderr.strip())
        out2 = (result2.stdout or result2.stderr or "").strip()
        if out2:
            try:
                size2 = int(out2.split()[0])
                if size2 > 0:
                    _emit(logger, "info", "Remote media exists on %s with size %s (stat)", target, size2)
                    return True
            except Exception:
                pass

        # Log the actual directory contents for debugging if the file is missing.
        try:
            directory = os.path.dirname(remote_media_path)
            directory_listing_command = [
                "adb",
                "-s",
                target,
                "shell",
                "ls",
                "-la",
                directory,
            ]
            _emit(logger, "info", "Inspecting remote directory contents: %s", " ".join(directory_listing_command))
            dir_result = subprocess.run(
                directory_listing_command,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
            )
            if dir_result.stdout.strip():
                _emit(logger, "info", "Remote directory listing for %s: %s", directory, dir_result.stdout.strip())
            if dir_result.stderr.strip():
                _emit(logger, "info", "Remote directory listing stderr for %s: %s", directory, dir_result.stderr.strip())
        except Exception as exc:
            _emit(logger, "warning", "Unable to inspect remote directory for %s: %s", target, exc)

        _emit(logger, "warning", "Remote media not found or empty at %s on %s", remote_media_path, target)
        return False
    except Exception as exc:
        _emit(logger, "warning", "Error verifying remote media on %s: %s", target, exc)
        return False


def _adb_find_instagram_story_action_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    screen = _adb_get_screen_size(target, logger=logger)
    if root is None:
        return None

    width, height = (1080, 2340) if screen is None else screen
    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if not any(keyword in combined for keyword in ("your story", "create story", "camera", "add")):
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        if cy <= int(height * 0.10) or cy >= int(height * 0.30):
            continue
        return cx, cy

    return None


def _adb_find_instagram_story_row_plus_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    screen = _adb_get_screen_size(target, logger=logger)
    if root is None:
        return None

    width, height = (1080, 2340) if screen is None else screen
    for node in root.iter():
        attrs = node.attrib
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or attrs.get("contentDescription") or "").strip()
        resource = (attrs.get("resource-id") or "").lower()
        combined = " ".join((text, desc, resource)).strip().lower()

        if text != "+" and desc != "+" and "add" not in combined and "create" not in combined:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        if cy <= int(height * 0.10) or cy >= int(height * 0.30):
            continue

        # Prefer a story-row plus target that is wider than a tiny badge and not the top-left toolbar icon.
        width_px = x2 - x1
        height_px = y2 - y1
        if min(width_px, height_px) < 20:
            continue

        if cx > int(width * 0.45):
            continue

        return cx, cy

    return None


def _adb_find_instagram_gallery_center(target: str, logger=None) -> tuple[int, int] | None:
    return _adb_find_ui_element_center(target, ("gallery", "recent", "photos", "camera roll"), logger=logger)


def _adb_find_instagram_next_center(target: str, logger=None) -> tuple[int, int] | None:
    return _adb_find_ui_element_center(target, ("next", "forward", "arrow"), logger=logger)


def _adb_find_instagram_share_center(target: str, logger=None) -> tuple[int, int] | None:
    return _adb_find_ui_element_center(target, ("share", "your story", "send", "post"), logger=logger)


def _adb_is_instagram_story_composer_visible(target: str, logger=None) -> bool:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return False

    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if any(keyword in combined for keyword in ("your story", "create story", "gallery", "recent", "camera roll", "story", "share", "next", "send")):
            return True
    return False


def _adb_is_instagram_story_share_screen_visible(target: str, logger=None) -> bool:
    return _adb_find_instagram_share_center(target, logger=logger) is not None or _adb_find_instagram_next_center(target, logger=logger) is not None


def _adb_wait_for_instagram_story_composer(target: str, logger=None, max_attempts: int = 4, delay_seconds: int = 2) -> bool:
    for attempt in range(1, max_attempts + 1):
        if _adb_is_instagram_story_composer_visible(target, logger=logger):
            return True
        if attempt < max_attempts:
            time.sleep(delay_seconds)
    return False


def _adb_wait_for_instagram_story_share_screen(target: str, logger=None, max_attempts: int = 4, delay_seconds: int = 2) -> bool:
    for attempt in range(1, max_attempts + 1):
        if _adb_is_instagram_story_share_screen_visible(target, logger=logger):
            return True
        if attempt < max_attempts:
            time.sleep(delay_seconds)
    return False


def _adb_find_instagram_plus_center(target: str, logger=None) -> tuple[int, int] | None:
    """Locate the '+' (create) action commonly in the top-left of Instagram home.

    Strategy:
    - Look for UI nodes with text/content-desc exactly '+' or containing 'new'/'create'/'add' and 'story'.
    - If found, ensure the bounds are in the top-left region (roughly left 30% and top 25%).
    - Fallback to a conservative relative coordinate near the top-left.
    """
    root = _adb_capture_ui_dump(target, logger=logger)
    screen = _adb_get_screen_size(target, logger=logger)
    if root is None:
        return None

    width, height = (1080, 2340) if screen is None else screen
    for node in root.iter():
        attrs = node.attrib
        text = (attrs.get("text") or "")
        desc = (attrs.get("content-desc") or attrs.get("contentDescription") or "")
        resource = (attrs.get("resource-id") or "")
        combined = " ".join((text, desc, resource)).strip().lower()

        # Exact plus character match
        if text.strip() == "+" or desc.strip() == "+":
            bounds = attrs.get("bounds", "")
            match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                if cx <= int(width * 0.35) and cy <= int(height * 0.30):
                    return cx, cy

        # Look for words indicating create/new/story and ensure top-left placement
        if ("new" in combined or "create" in combined or "add" in combined) and "story" in combined:
            bounds = attrs.get("bounds", "")
            match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2
                if cx <= int(width * 0.40) and cy <= int(height * 0.35):
                    return cx, cy

    # Conservative fallback near top-left
    return _adb_get_relative_point(target, 0.08, 0.08, logger=logger)


def get_follow_button_tap_position(candidate: tuple[int, int, int, int], image_shape: tuple[int, int, int] | tuple[int, int]) -> tuple[int, int]:
    x, y, width, height = candidate
    screen_width = max(1, image_shape[1] if len(image_shape) >= 2 else image_shape[0])
    screen_height = max(1, image_shape[0])

    center_x = x + width // 2
    center_y = y + height // 2
    tap_x = int(min(max(center_x, int(screen_width * 0.70)), int(screen_width * 0.96)))
    tap_y = int(min(max(center_y, int(screen_height * 0.08)), int(screen_height * 0.95)))
    tap_x += random.randint(-6, 6)
    tap_y += random.randint(-4, 4)
    tap_x = int(min(max(tap_x, int(screen_width * 0.70)), int(screen_width * 0.96)))
    tap_y = int(min(max(tap_y, int(screen_height * 0.08)), int(screen_height * 0.95)))
    return tap_x, tap_y


class InstagramLikeFeedFlow:
    name = "instagram_like_feed"

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

        log = logger or print
        target = profile.target

        def check_abort() -> bool:
            if callable(should_stop) and should_stop():
                log.info("Abort requested during Instagram like-reels flow for profile %s", profile.id)
                return True
            return False

        log.info("Starting Instagram like-reels flow for profile %s", profile.id)
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            if "monkey" in command or "am start" in command:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(3)
            else:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(1)

        log.info("Waiting for Instagram to load")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(5)

        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=logger):
            log.warning("Instagram feed verification failed for profile %s; aborting like-reels flow", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False}

        log.info("Navigating to Reels tab for profile %s", profile.id)
        navigate_reels = self._build_navigate_to_reels_command(target)
        log.info("Reels navigation command: %s", navigate_reels)
        adb_client.run_command(navigate_reels)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(2)

        like_count = 0
        max_likes = 6
        while like_count < max_likes:
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}

            like_command = self._build_like_reel_command(target)
            log.info("Like command: %s", like_command)
            adb_client.run_command(like_command)
            log.info("Liked reel %d/%d", like_count + 1, max_likes)
            like_count += 1
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            time.sleep(random.uniform(1.5, 2.5))

            if like_count < max_likes:
                swipe_command = self._build_next_reel_swipe_command(target)
                adb_client.run_command(swipe_command)
                log.info("Swiped to next reel")
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(random.uniform(1.0, 1.8))

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        return_home_command = self._build_return_home_command(target)
        log.info("Returning to home page before closing Instagram: %s", return_home_command)
        adb_client.run_command(return_home_command)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(1)
        adb_client.run_command(f"adb -s {target} shell {home()}")
        log.info("Waiting 3 seconds after home press before closing the connection")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(3)
        log.info("Instagram like-reels flow completed")
        return {"profile_id": profile.id, "target": target}

    def build_launch_commands(self, target: str) -> list[str]:
        return [
            f"adb -s {target} shell {tap(109, 670)}",
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    def _build_navigate_to_reels_command(self, target: str) -> str:
        start_x = random.randint(800, 900)
        start_y = random.randint(900, 1200)
        end_x = random.randint(100, 200)
        end_y = random.randint(900, 1200)
        duration = random.randint(200, 350)
        return f"adb -s {target} shell {swipe(start_x, start_y, end_x, end_y, duration)}"

    def _build_like_reel_command(self, target: str) -> str:
        x = 980
        y = 990
        return f"adb -s {target} shell {tap(x, y)}"

    def _build_next_reel_swipe_command(self, target: str) -> str:
        start_x = random.randint(520, 580)
        start_y = random.randint(1000, 1200)
        end_x = random.randint(520, 580)
        end_y = random.randint(300, 500)
        duration = random.randint(300, 600)
        return f"adb -s {target} shell {swipe(start_x, start_y, end_x, end_y, duration)}"

    def _build_return_home_command(self, target: str) -> str:
        home_center = _adb_find_instagram_home_button_center(target)
        if home_center is not None:
            x, y = home_center
        else:
            x, y = _adb_get_relative_point(target, 0.11, 0.95)
        return f"adb -s {target} shell {tap(x, y)}"

    def _build_fallback_like_position(self) -> tuple[int, int]:
        return 980, 990


class InstagramNotificationsFlow:
    name = "instagram_notifications"

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
                emit("info", "Abort requested during Instagram notifications flow for profile %s", profile.id)
                return True
            return False

        emit("info", "Starting Instagram notifications flow for profile %s", profile.id)
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            if "monkey" in command or "am start" in command:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(3)
            else:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(1)

        emit("info", "Waiting for Instagram to load before opening notifications")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(5)

        open_notifications_command = self._build_open_notifications_command(target)
        emit("info", "Opening Instagram notifications tab: %s", open_notifications_command)
        adb_client.run_command(open_notifications_command)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        emit("info", "Waiting 3 seconds for the notifications tab to fully load")
        time.sleep(3)

        for scroll_index in range(5):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            scroll_command = self._build_scroll_down_command(target)
            adb_client.run_command(scroll_command)
            emit("info", "Scrolled notifications list (step %d/5)", scroll_index + 1)
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            time.sleep(2)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            screenshot_path = Path(handle.name)

        emit("info", "Preparing screenshot analysis for notifications flow; temporary file: %s", screenshot_path)
        try:
            emit("info", "Capturing screenshot from device %s", target)
            capture_result = self._capture_screenshot(target, str(screenshot_path), logger=log)
            if not capture_result:
                emit("warning", "Screenshot capture returned no output path for profile %s", profile.id)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            emit("info", "Screenshot capture completed; captured path: %s", capture_result)
            if not screenshot_path.exists():
                emit("warning", "Screenshot file was not created at %s after capture", screenshot_path)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            image = self._load_screenshot_image(str(screenshot_path), logger=log)
            if image is None:
                emit("warning", "Unable to read screenshot for Instagram notifications flow from %s", screenshot_path)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            emit("info", "Loaded screenshot image with shape %s and dtype %s", image.shape, image.dtype)
            button_candidates = self._find_follow_button_candidates(image, logger=log)
            emit("info", "Detected %d follow-button candidate(s)", len(button_candidates))
            for index, candidate in enumerate(button_candidates[:4], start=1):
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                x, y, width, height = candidate
                tap_x, tap_y = get_follow_button_tap_position((x, y, width, height), image.shape)
                command = f"adb -s {target} shell {tap(tap_x, tap_y)}"
                emit("info", "Tapping follow-button candidate %d at box=%s,%s size=%sx%s -> tap=%s,%s", index, x, y, width, height, tap_x, tap_y)
                adb_client.run_command(command)
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(0.8)
        except Exception as exc:  # pragma: no cover - diagnostic path
            emit("exception", "Unexpected error during screenshot/OpenCV processing for profile %s: %s", profile.id, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False}
        finally:
            try:
                screenshot_path.unlink(missing_ok=True)
            except OSError as exc:
                emit("warning", "Failed to remove temporary screenshot file %s: %s", screenshot_path, exc)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        adb_client.run_command(f"adb -s {target} shell {home()}")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(2)
        emit("info", "Instagram notifications flow completed")
        return {"profile_id": profile.id, "target": target}

    def build_launch_commands(self, target: str) -> list[str]:
        return [
            f"adb -s {target} shell {tap(109, 670)}",
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    def _build_open_notifications_command(self, target: str) -> str:
        ui_center = _adb_find_instagram_notifications_center(target)
        if ui_center is not None:
            x, y = ui_center
            return f"adb -s {target} shell {tap(x, y)}"
        x, y = _adb_get_relative_point(target, 0.92, 0.08)
        return f"adb -s {target} shell {tap(x, y)}"

    def _build_scroll_down_command(self, target: str) -> str:
        start_x = random.randint(520, 580)
        start_y = random.randint(1500, 1800)
        end_x = random.randint(520, 580)
        end_y = random.randint(900, 1200)
        duration = random.randint(300, 650)
        return f"adb -s {target} shell {swipe(start_x, start_y, end_x, end_y, duration)}"

    def _capture_screenshot(self, target: str, output_path: str, logger=None) -> str | None:
        shell_command = f"adb -s {target} shell screencap -p /sdcard/instagram_notifications.png"
        pull_command = f"adb -s {target} pull /sdcard/instagram_notifications.png {output_path}"
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

        try:
            shell_result = subprocess.run(shell_command, shell=True, check=True, capture_output=True, text=True)
            emit("info", "ADB screencap command succeeded for %s: %s", target, shell_result.stdout.strip() or "<no stdout>")
        except subprocess.CalledProcessError as exc:
            emit("warning", "ADB screencap command failed for %s: %s", target, exc.stderr.strip() or str(exc))
            return None

        try:
            pull_result = subprocess.run(pull_command, shell=True, check=True, capture_output=True, text=True)
            emit("info", "ADB pull command succeeded for %s: %s", target, pull_result.stdout.strip() or "<no stdout>")
        except subprocess.CalledProcessError as exc:
            emit("warning", "ADB pull command failed for %s: %s", target, exc.stderr.strip() or str(exc))
            return None

        return output_path

    def _load_screenshot_image(self, screenshot_path: str, logger=None):
        if cv2 is None or np is None:
            log = logger if logger is not None else None
            if log is None:
                print(f"OpenCV is not available; unable to inspect screenshot {screenshot_path}")
            else:
                log.warning("OpenCV is not available; unable to inspect screenshot %s", screenshot_path)
            return None
        return cv2.imread(screenshot_path)

    def _find_follow_button_candidates(self, image, logger=None):
        log = logger if logger is not None else None
        if cv2 is None or np is None:
            if log is None:
                print("OpenCV is not available; follow-button detection skipped")
            else:
                log.warning("OpenCV is not available; follow-button detection skipped")
            return []

        if log is None:
            print(f"Running OpenCV follow-button detection on image shape {image.shape}")
        else:
            log.info("Running OpenCV follow-button detection on image shape %s", image.shape)

        ocr_candidates = self._find_follow_button_candidates_via_ocr(image, logger=log)
        if ocr_candidates:
            if log is None:
                print(f"OCR detected {len(ocr_candidates)} follow-button region(s)")
            else:
                log.info("OCR detected %d follow-button region(s)", len(ocr_candidates))
            return ocr_candidates

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        _, thresholded = cv2.threshold(blurred, 180, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresholded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if log is None:
            print(f"OpenCV contour detection found {len(contours)} contour(s)")
        else:
            log.info("OpenCV contour detection found %d contour(s)", len(contours))

        candidates = []
        rejected = 0
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if width < 20 or height < 10:
                rejected += 1
                continue
            if width > 800 or height > 320:
                rejected += 1
                continue
            if area < 120:
                rejected += 1
                continue
            ratio = width / max(height, 1)
            if ratio < 0.5 or ratio > 12.0:
                rejected += 1
                continue

            region = image[y:y + height, x:x + width]
            if region.size == 0:
                rejected += 1
                continue

            region_hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
            lower_blue = np.array([100, 70, 70], dtype=np.uint8)
            upper_blue = np.array([140, 255, 255], dtype=np.uint8)
            blue_mask = cv2.inRange(region_hsv, lower_blue, upper_blue)
            blue_pixels = int(cv2.countNonZero(blue_mask))
            blue_ratio = blue_pixels / max(1, region.size // 3)
            if blue_ratio < 0.02:
                rejected += 1
                continue

            candidates.append((x, y, width, height))

        if log is None:
            print(f"OpenCV follow-button filtering kept {len(candidates)} candidate(s); rejected {rejected} contour(s)")
        else:
            log.info("OpenCV follow-button filtering kept %d candidate(s); rejected %d contour(s)", len(candidates), rejected)
        for index, candidate in enumerate(candidates, start=1):
            x, y, width, height = candidate
            if log is None:
                print(f"Candidate {index}: box=({x},{y}) size={width}x{height} ratio={width / max(height, 1):.2f}")
            else:
                log.info("Candidate %d: box=(%s,%s) size=%sx%s ratio=%.2f", index, x, y, width, height, width / max(height, 1))

        if candidates:
            candidates.sort(key=lambda item: (item[1], item[0]))
            return candidates

        fallback_candidates = self._build_fallback_follow_regions(image, logger=log)
        if log is None:
            print(f"Using {len(fallback_candidates)} fallback follow regions")
        else:
            log.info("Using %d fallback follow regions", len(fallback_candidates))
        return fallback_candidates

    def _find_follow_button_candidates_via_ocr(self, image, logger=None):
        log = logger if logger is not None else None
        if pytesseract is None or Image is None:
            if log is None:
                print("pytesseract/Pillow is not available; skipping OCR")
            else:
                log.info("pytesseract/Pillow is not available; skipping OCR")
            return []

        # Ensure Tesseract binary is configured for pytesseract
        tesseract_exe = shutil.which("tesseract")
        if not tesseract_exe:
            default_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            if os.path.exists(default_path):
                try:
                    pytesseract.pytesseract.tesseract_cmd = default_path
                    if log is None:
                        print(f"Configured pytesseract to use {default_path}")
                    else:
                        log.info("Configured pytesseract to use %s", default_path)
                except Exception:
                    pass
            else:
                if log is None:
                    print("Tesseract executable not found; OCR will be skipped")
                else:
                    log.warning("Tesseract executable not found; OCR will be skipped")
                return []

        try:
            pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)
        except Exception as exc:  # pragma: no cover - optional OCR path
            if log is None:
                print(f"OCR failed: {exc}")
            else:
                log.warning("OCR failed: %s", exc)
            return []

        candidates = []
        for index, text in enumerate(data.get("text", []) or []):
            cleaned = (text or "").strip().lower()
            if not cleaned or cleaned not in {"follow", "follow back", "followed"}:
                continue
            x = int(data["left"][index])
            y = int(data["top"][index])
            width = int(data["width"][index])
            height = int(data["height"][index])
            if width < 20 or height < 10:
                continue
            candidates.append((x, y, width, height))

        candidates.sort(key=lambda item: (item[1], item[0]))
        return candidates

    def _build_fallback_follow_regions(self, image, logger=None):
        height = image.shape[0]
        width = image.shape[1]
        regions = []
        right_x = min(width - 100, max(900, int(width * 0.86)))
        y_positions = [int(height * 0.20), int(height * 0.40), int(height * 0.60), int(height * 0.80)]
        for y in y_positions:
            regions.append((right_x, y, min(120, width - right_x - 20), 70))
            regions.append((max(880, right_x - 30), y + 90, min(120, width - right_x + 20), 70))

        if logger is not None:
            logger.info("Fallback follow regions anchored near x=%s on %sx%s image", right_x, width, height)

        return regions


class InstagramScrollFlow:
    name = "instagram_scroll"

    def get_progress_total_steps(self, target: str) -> int:
        launch_commands = self.build_launch_commands(target)
        sequence = self._build_sequence(target)
        return len(launch_commands) + len(sequence) + 1

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

        log = logger or print
        target = profile.target

        def check_abort() -> bool:
            if callable(should_stop) and should_stop():
                log.info("Abort requested during Instagram flow for profile %s", profile.id)
                return True
            return False

        log.info("Starting Instagram scroll flow for profile %s", profile.id)
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            if "monkey" in command or "am start" in command:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(3)
            else:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(1)

        log.info("Waiting for Instagram to load")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(5)

        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=logger):
            log.warning("Instagram feed verification failed for profile %s; requesting manual continue", profile.id)
            if callable(status_callback):
                status_callback(profile.id, "manual_continue")
            if manual_continue_callback is not None:
                manual_continue_callback(profile.id)

            if manual_continue_event is not None:
                timeout_seconds = 300
                wait_start = time.time()
                while not manual_continue_event.is_set():
                    if check_abort():
                        return {"profile_id": profile.id, "target": target, "aborted": True}
                    if not _adb_is_device_screen_on(target, adb_client, logger=logger):
                        log.warning("Device screen inactive while waiting for manual continue for profile %s; aborting scroll flow", profile.id)
                        return {"profile_id": profile.id, "target": target, "aborted": False}
                    if time.time() - wait_start > timeout_seconds:
                        log.warning("Manual continue timeout after %s seconds for profile %s", timeout_seconds, profile.id)
                        break
                    time.sleep(0.5)

                if manual_continue_event.is_set():
                    manual_continue_event.clear()
                    log.info("Manual continue requested for profile %s; re-verifying feed UI", profile.id)
                    if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=logger):
                        log.warning("Instagram feed still not verified after manual continue for profile %s; aborting scroll flow", profile.id)
                        return {"profile_id": profile.id, "target": target, "aborted": False}
                else:
                    log.warning("Manual continue was not provided for profile %s; aborting scroll flow", profile.id)
                    return {"profile_id": profile.id, "target": target, "aborted": False}
            else:
                return {"profile_id": profile.id, "target": target, "aborted": False}

        sequence = self._build_sequence(target)
        for step in sequence:
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(step["command"])
            log.info("Executed swipe: %s", step["command"])
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            time.sleep(step["delay"])

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        adb_client.run_command(f"adb -s {target} shell {home()}")
        log.info("Waiting 3 seconds after home press before closing the connection")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(3)
        log.info("Instagram scroll flow completed")
        return {"profile_id": profile.id, "target": target}

    def build_launch_commands(self, target: str) -> list[str]:
        return [
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    def _build_sequence(self, target: str, target_total_delay: float = 600.0):
        sequence = []
        current_total_delay = 0.0
        down_swipes = 0
        up_swipes = 0

        while current_total_delay < target_total_delay:
            if down_swipes >= up_swipes * 2 and random.random() < 0.25:
                direction = "up"
            else:
                direction = "down" if random.random() < 0.95 else "up"

            if direction == "down":
                start_y = random.randint(1750, 1850)
                end_y = random.randint(550, 800)
                start_x = random.randint(480, 560)
                end_x = random.randint(470, 560)
                duration = random.randint(220, 650)
                delay = round(random.uniform(2.5, 6.5), 2)
                down_swipes += 1
            else:
                start_y = random.randint(850, 1100)
                end_y = random.randint(1250, 1550)
                start_x = random.randint(500, 550)
                end_x = random.randint(500, 550)
                duration = random.randint(220, 600)
                delay = round(random.uniform(2.0, 3.8), 2)
                up_swipes += 1

            offset_x = random.randint(-25, 25)
            offset_start = random.randint(-20, 20)
            offset_end = random.randint(-20, 20)
            command = f"adb -s {target} shell {swipe(start_x + offset_x, start_y + offset_start, end_x + offset_x, end_y + offset_end, duration)}"
            sequence.append({
                "command": command,
                "delay": delay,
                "direction": direction,
            })
            current_total_delay += delay

        if len(sequence) < 12:
            sequence = self._build_sequence(target, target_total_delay=target_total_delay)

        return sequence


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

        if media_source.is_dir() and selected_media is not None:
            moved = media_queue.mark_used(selected_media)
            emit("info", "Marked story media %s as used for profile %s: %s", selected_media, profile.id, moved)

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

        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=log):
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

        # Confirm that a story composer action is visible before trying to post.
        if (
            _adb_find_instagram_story_action_center(target, logger=log) is None
            and _adb_find_instagram_next_center(target, logger=log) is None
            and _adb_find_instagram_share_center(target, logger=log) is None
        ):
            emit("warning", "Story composer did not show a valid share action for %s; aborting upload", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        # After selecting the media, prefer to directly post via the 'Your story' target if available.
        # We only treat the action as successful after the app moves out of the composer state.
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
                    emit("warning", "Story post was not confirmed for %s after tapping 'Your story'; trying fallback path", target)
                    # Capture a debug UI dump to help diagnose why confirmation failed
                    try:
                        root = _adb_capture_ui_dump(target, logger=log)
                        if root is None:
                            emit("warning", "UI dump returned no data for %s when verification failed", target)
                        else:
                            node_count = sum(1 for _ in root.iter())
                            emit("info", "UI dump node count for %s after failed verify: %s", target, node_count)
                    except Exception as exc:
                        emit("warning", "Failed to capture UI dump for %s: %s", target, exc)
        except Exception:
            posted_via_your_story = False

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        if not posted_via_your_story:
            if not self._tap_next(target, adb_client, logger=log):
                emit("warning", "Unable to reliably tap Next on story compose screen for %s", target)
            time.sleep(5)
            if self._tap_next(target, adb_client, logger=log):
                time.sleep(3)
                posted_via_your_story = self._verify_story_post_completed(target, adb_client, logger=log)
                if posted_via_your_story:
                    emit("info", "Confirmed story post for %s via Next path", target)
                else:
                    emit("warning", "Story post was not confirmed for %s after tapping Next; trying Share fallback", target)
                    try:
                        root = _adb_capture_ui_dump(target, logger=log)
                        if root is None:
                            emit("warning", "UI dump returned no data for %s after Next path failed", target)
                        else:
                            node_count = sum(1 for _ in root.iter())
                            emit("info", "UI dump node count for %s after Next path failed: %s", target, node_count)
                    except Exception as exc:
                        emit("warning", "Failed to capture UI dump for %s after Next path failed: %s", target, exc)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        if not posted_via_your_story:
            if not self._tap_share_story(target, adb_client, logger=log):
                emit("warning", "Unable to reliably tap Share on story upload screen for %s", target)
            time.sleep(8)
            posted_via_your_story = self._verify_story_post_completed(target, adb_client, logger=log)
            if posted_via_your_story:
                emit("info", "Confirmed story post for %s via Share path", target)
            else:
                emit("warning", "Story post could not be confirmed for %s after all story upload attempts", target)
                try:
                    root = _adb_capture_ui_dump(target, logger=log)
                    if root is None:
                        emit("warning", "UI dump returned no data for %s after all attempts", target)
                    else:
                        node_count = sum(1 for _ in root.iter())
                        emit("info", "Final UI dump node count for %s after all attempts: %s", target, node_count)
                except Exception as exc:
                    emit("warning", "Failed to capture final UI dump for %s: %s", target, exc)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        home_command = _adb_find_instagram_home_button_center(target, logger=log)
        if home_command is not None:
            adb_client.run_command(f"adb -s {target} shell {tap(*home_command)}")
        else:
            adb_client.run_command(f"adb -s {target} shell {home()}")

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        if not posted_via_your_story:
            emit("warning", "Instagram story upload did not complete successfully for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

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
        # Prefer tapping the story-row plus button in the stories section.
        # This avoids the top-left white-background create icon.
        story_row_plus_center = _adb_find_instagram_story_row_plus_center(target, logger=logger)
        if story_row_plus_center is not None:
            adb_client.run_command(f"adb -s {target} shell {tap(*story_row_plus_center)}")
            time.sleep(1)
            return True

        # Fall back to a story action button that is still within the stories row area.
        story_center = _adb_find_instagram_story_action_center(target, logger=logger)
        if story_center is not None:
            adb_client.run_command(f"adb -s {target} shell {tap(*story_center)}")
            time.sleep(1)
            return True

        # Final fallback: try left/top and right/top generic taps only when the home feed is visible.
        if _adb_ensure_instagram_feed_visible(target, adb_client, logger=logger):
            fallback_points = [
                _adb_get_relative_point(target, 0.08, 0.10, logger=logger),
                _adb_get_relative_point(target, 0.92, 0.10, logger=logger),
            ]
            for x, y in fallback_points:
                adb_client.run_command(f"adb -s {target} shell {tap(x, y)}")
                time.sleep(2)
            return True

        if logger is not None:
            _emit(logger, "warning", "Cannot safely open story composer because Instagram home feed is not detected on %s", target)
        return False

    def _select_story_media(self, target: str, adb_client, logger=None) -> bool:
        gallery_center = _adb_find_instagram_gallery_center(target, logger=logger)
        if gallery_center is not None:
            adb_client.run_command(f"adb -s {target} shell {tap(*gallery_center)}")
            return True

        media_thumb = _adb_find_ui_element_center(target, ("photo", "image", "gallery", "recent", "camera roll", "thumbnail"), logger=logger)
        if media_thumb is not None:
            adb_client.run_command(f"adb -s {target} shell {tap(*media_thumb)}")
            return True

        if _adb_is_instagram_story_composer_visible(target, logger=logger):
            fallback_media = _adb_get_relative_point(target, 0.18, 0.82, logger=logger)
            _emit(logger, "info", "Tapping fallback story media at %s,%s for %s", fallback_media[0], fallback_media[1], target)
            adb_client.run_command(f"adb -s {target} shell {tap(*fallback_media)}")
            return True

        if logger is not None:
            logger.warning("Unable to locate a story media thumbnail or gallery button for %s", target)
        return False

    def _tap_your_story(self, target: str, adb_client, logger=None) -> bool:
        """Try to locate and tap the 'Your story' share target using UI dump or OCR."""
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

        # 1) Prefer UI dump search for explicit 'your story' text
        ui_center = _adb_find_ui_element_center(target, ("your story", "yourstory", "your story button"), logger=log)
        if ui_center is not None:
            x, y = ui_center
            emit("info", "Tapping 'Your story' via UI dump at %s,%s for %s", x, y, target)
            adb_client.run_command(f"adb -s {target} shell {tap(x, y)}")
            return True

        # 2) Try OCR on a bottom-right crop of a fresh screenshot
        if pytesseract is not None and Image is not None and cv2 is not None:
            try:
                tmp_remote = "/sdcard/instagram_your_story.png"
                tmp_local = Path(tempfile.gettempdir()) / f"instagram_your_story_{target}.png"
                subprocess.run(f"adb -s {target} shell screencap -p {tmp_remote}", shell=True, check=True)
                subprocess.run(f"adb -s {target} pull {tmp_remote} {tmp_local}", shell=True, check=True)
                img = cv2.imread(str(tmp_local))
                if img is not None:
                    h, w = img.shape[:2]
                    x0 = int(w * 0.55)
                    y0 = int(h * 0.65)
                    crop = img[y0:h, x0:w]
                    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                    # optional thresholding
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
                            adb_client.run_command(f"adb -s {target} shell {tap(x_abs, y_abs)}")
                            return True
            except Exception as exc:
                emit("info", "OCR attempt for 'Your story' failed for %s: %s", target, exc)

        # 3) Conservative fallback: do not tap blindly if no explicit target is detected.
        emit("warning", "Unable to locate an explicit 'Your story' target for %s; skipping blind tap", target)
        return False

    def _tap_next(self, target: str, adb_client, logger=None) -> bool:
        next_center = _adb_find_instagram_next_center(target, logger=logger)
        if next_center is not None:
            adb_client.run_command(f"adb -s {target} shell {tap(*next_center)}")
            return True

        if _adb_wait_for_instagram_story_share_screen(target, logger=logger):
            fallback_next = _adb_get_relative_point(target, 0.90, 0.10, logger=logger)
            adb_client.run_command(f"adb -s {target} shell {tap(*fallback_next)}")
            return True

        if logger is not None:
            _emit(logger, "warning", "Skip tapping Next because story share screen is not detected on %s", target)
        return False

    def _tap_share_story(self, target: str, adb_client, logger=None) -> bool:
        share_center = _adb_find_instagram_share_center(target, logger=logger)
        if share_center is not None:
            adb_client.run_command(f"adb -s {target} shell {tap(*share_center)}")
            return True

        if _adb_wait_for_instagram_story_share_screen(target, logger=logger):
            fallback_share = _adb_get_relative_point(target, 0.90, 0.92, logger=logger)
            adb_client.run_command(f"adb -s {target} shell {tap(*fallback_share)}")
            return True

        if logger is not None:
            _emit(logger, "warning", "Skip tapping Share because story share screen is not detected on %s", target)
        return False


class InstagramUpdateBioFlow(InstagramNotificationsFlow):
    name = "update_bio"

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
                emit("info", "Abort requested during Instagram update bio flow for profile %s", profile.id)
                return True
            return False

        emit("info", "Starting Instagram update bio flow for profile %s", profile.id)
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            if "monkey" in command or "am start" in command:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(3)
            else:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(1)

        emit("info", "Waiting for Instagram to load before profile navigation")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(10)

        profile_button_command = f"adb -s {target} shell {tap(980, 2290)}"
        emit("info", "Tapping Instagram profile button: %s", profile_button_command)
        adb_client.run_command(profile_button_command)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(10)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            screenshot_path = Path(handle.name)

        emit("info", "Capturing screenshot to verify Edit Profile button; temporary file: %s", screenshot_path)
        try:
            capture_result = self._capture_screenshot(target, str(screenshot_path), logger=log)
            if not capture_result:
                emit("warning", "Screenshot capture failed for profile %s", profile.id)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            image = self._load_screenshot_image(str(screenshot_path), logger=log)
            if image is None:
                emit("warning", "Unable to load screenshot for profile %s", profile.id)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            edit_profile_position = self._find_edit_profile_button_location(image, logger=log)
            if edit_profile_position is None:
                edit_profile_position = (235, 860)
                emit("warning", "Edit Profile button not detected; using fallback coordinates %s", edit_profile_position)
            else:
                emit("info", "Detected Edit Profile button at %s", edit_profile_position)

            edit_profile_x, edit_profile_y = edit_profile_position
            adb_client.run_command(f"adb -s {target} shell {tap(edit_profile_x, edit_profile_y)}")
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            time.sleep(10)

            bio_button_command = f"adb -s {target} shell {tap(485, 1350)}"
            emit("info", "Tapping Bio field: %s", bio_button_command)
            adb_client.run_command(bio_button_command)
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            time.sleep(10)

            text_command = f"adb -s {target} shell {write_text('Entering the Bio here as test. Hello Hello Hello')}"
            emit("info", "Entering test bio text")
            adb_client.run_command(text_command)
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            time.sleep(10)

            for back_index in range(5):
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                adb_client.run_command(f"adb -s {target} shell {back()}")
                emit("info", "Pressed back button %d/5", back_index + 1)
                time.sleep(1)

        except Exception as exc:  # pragma: no cover - diagnostic path
            emit("exception", "Unexpected error during update bio flow for profile %s: %s", profile.id, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False}
        finally:
            try:
                screenshot_path.unlink(missing_ok=True)
            except OSError as exc:
                emit("warning", "Failed to remove temporary screenshot file %s: %s", screenshot_path, exc)

        emit("info", "Instagram update bio flow completed")
        return {"profile_id": profile.id, "target": target}

    def _find_edit_profile_button_location(self, image, logger=None):
        log = logger if logger is not None else None
        if pytesseract is None or Image is None:
            if log is None:
                print("pytesseract/Pillow is not available; skipping Edit Profile OCR detection")
            else:
                log.info("pytesseract/Pillow is not available; skipping Edit Profile OCR detection")
            return None

        tesseract_exe = shutil.which("tesseract")
        if not tesseract_exe:
            default_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            if os.path.exists(default_path):
                try:
                    pytesseract.pytesseract.tesseract_cmd = default_path
                except Exception:
                    pass
            else:
                if log is None:
                    print("Tesseract executable not found; OCR will be skipped")
                else:
                    log.warning("Tesseract executable not found; OCR will be skipped")
                return None

        try:
            pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            data = pytesseract.image_to_data(pil_image, output_type=pytesseract.Output.DICT)
        except Exception as exc:  # pragma: no cover - optional OCR path
            if log is None:
                print(f"OCR failed: {exc}")
            else:
                log.warning("OCR failed: %s", exc)
            return None

        candidates = []
        for index, text in enumerate(data.get("text", []) or []):
            cleaned = (text or "").strip().lower()
            if not cleaned:
                continue
            if cleaned == "edit profile" or cleaned in {"edit", "profile"}:
                x = int(data["left"][index])
                y = int(data["top"][index])
                width = int(data["width"][index])
                height = int(data["height"][index])
                candidates.append((cleaned, x, y, width, height))

        if not candidates:
            return None

        # Prefer the exact button label when available.
        exact = next((item for item in candidates if item[0] == "edit profile"), None)
        chosen = exact or candidates[0]
        _, x, y, width, height = chosen
        return (x + width // 2, y + height // 2)


class InstagramWarmUpDay1Flow(InstagramNotificationsFlow, InstagramScrollFlow):
    name = "warm_up_process"

    def get_progress_total_steps(self, target: str) -> int:
        # Calculate total progress ticks based on where the flow actually
        # calls `adb_client.mark_progress_step()`:
        # - 1 for the launch group
        # - 1 for feed verification
        # - 1 per swipe in the warm-up sequence
        # - 1 for opening notifications
        # - 5 for notification list scrolls
        # - up to 5 follow-button taps
        # - 1 for post-follow processing (screenshot/analysis)
        # - 1 for final return/force-stop
        try:
            sequence = self._build_sequence(target, target_total_delay=600.0) or []
        except Exception:
            sequence = []

        total = 0
        total += 1  # launch group
        total += 1  # feed verification
        total += len(sequence)  # per-swipe progress
        total += 1  # open notifications
        total += 5  # notification list scrolls
        total += 5  # follow-button attempts
        total += 1  # post-follow processing
        total += 1  # final force-stop

        return int(total)

    def _build_return_home_command(self, target: str) -> str:
        home_center = _adb_find_instagram_home_button_center(target)
        if home_center is not None:
            x, y = home_center
        else:
            x, y = _adb_get_relative_point(target, 0.11, 0.95)
        return f"adb -s {target} shell {tap(x, y)}"

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
                emit("info", "Abort requested during Instagram warm-up day 1 flow for profile %s", profile.id)
                return True
            return False

        emit("info", "Starting Instagram warm-up day 1 flow for profile %s", profile.id)
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            if "monkey" in command or "am start" in command:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(3)
            else:
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                time.sleep(1)

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        emit("info", "Waiting for Instagram to load before warm-up")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(5)

        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=log):
            emit("warning", "Instagram feed verification failed for profile %s; aborting warm-up flow", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False}

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        emit("info", "Warm-up scrolling feed for 10 minutes")
        sequence = self._build_sequence(target, target_total_delay=600.0)
        for step in sequence:
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(step["command"])
            emit("info", "Executed swipe: %s", step["command"])
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            # Report progress for each swipe so UI progress advances during the 10-minute warm-up
            try:
                if hasattr(adb_client, "mark_progress_step"):
                    adb_client.mark_progress_step()
            except Exception:
                pass
            time.sleep(step["delay"])


        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        post_scroll_home_command = self._build_return_home_command(target)
        emit("info", "Tapping home button after warm-up scrolling: %s", post_scroll_home_command)
        adb_client.run_command(post_scroll_home_command)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(5)

        open_notifications_command = self._build_open_notifications_command(target)
        emit("info", "Opening Instagram notifications tab: %s", open_notifications_command)
        adb_client.run_command(open_notifications_command)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()
        time.sleep(5)

        for scroll_index in range(5):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            scroll_command = self._build_scroll_down_command(target)
            adb_client.run_command(scroll_command)
            emit("info", "Scrolled notifications list (step %d/5)", scroll_index + 1)
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            # Progress per notification scroll
            try:
                if hasattr(adb_client, "mark_progress_step"):
                    adb_client.mark_progress_step()
            except Exception:
                pass
            time.sleep(2)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            screenshot_path = Path(handle.name)

        emit("info", "Preparing screenshot analysis for warm-up notifications; temporary file: %s", screenshot_path)
        try:
            emit("info", "Capturing screenshot from device %s", target)
            capture_result = self._capture_screenshot(target, str(screenshot_path), logger=log)
            if not capture_result:
                emit("warning", "Screenshot capture returned no output path for profile %s", profile.id)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            emit("info", "Screenshot capture completed; captured path: %s", capture_result)
            if not screenshot_path.exists():
                emit("warning", "Screenshot file was not created at %s after capture", screenshot_path)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            image = self._load_screenshot_image(str(screenshot_path), logger=log)
            if image is None:
                emit("warning", "Unable to read screenshot for warm-up day 1 notifications flow from %s", screenshot_path)
                return {"profile_id": profile.id, "target": target, "aborted": False}

            emit("info", "Loaded screenshot image with shape %s and dtype %s", image.shape, image.dtype)
            button_candidates = self._find_follow_button_candidates(image, logger=log)
            emit("info", "Detected %d follow-button candidate(s)", len(button_candidates))
            for index, candidate in enumerate(button_candidates[:5], start=1):
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                x, y, width, height = candidate
                tap_x, tap_y = get_follow_button_tap_position((x, y, width, height), image.shape)
                command = f"adb -s {target} shell {tap(tap_x, tap_y)}"
                emit("info", "Tapping follow-button candidate %d at box=%s,%s size=%sx%s -> tap=%s,%s", index, x, y, width, height, tap_x, tap_y)
                adb_client.run_command(command)
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                # Mark progress for each follow attempt
                try:
                    if hasattr(adb_client, "mark_progress_step"):
                        adb_client.mark_progress_step()
                except Exception:
                    pass
                time.sleep(0.8)

            if hasattr(adb_client, "mark_progress_step"):
                adb_client.mark_progress_step()
        except Exception as exc:  # pragma: no cover - diagnostic path
            emit("exception", "Unexpected error during screenshot/OpenCV processing for profile %s: %s", profile.id, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False}
        finally:
            try:
                screenshot_path.unlink(missing_ok=True)
            except OSError as exc:
                emit("warning", "Failed to remove temporary screenshot file %s: %s", screenshot_path, exc)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        adb_client.run_command(f"adb -s {target} shell {home()}")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(2)

        # Ensure Instagram is closed after returning home
        try:
            adb_client.run_command(f"adb -s {target} shell am force-stop com.instagram.android")
            emit("info", "Force-stopped Instagram on %s", target)
        except Exception:
            emit("warning", "Failed to force-stop Instagram on %s; continuing", target)

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        emit("info", "Instagram warm-up day 1 flow completed")
        return {"profile_id": profile.id, "target": target}
