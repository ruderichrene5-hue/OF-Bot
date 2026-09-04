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

try:
    import uiautomator2 as u2
except ImportError:  # pragma: no cover - optional uiautomator2 backend
    u2 = None

from adb_bot.core import human_timing
from adb_bot.core.adb_commands import back, home, swipe, tap, write_text
from adb_bot.core.models import Profile
from adb_bot.core.proc import adb as _adb_run, run as _run_hidden
from adb_bot.automation.flows.story_media import StoryMediaQueueManager, discover_story_media_files, get_story_media_queue
from adb_bot.automation.flows import interruptions
from adb_bot.automation import ban_detection


def _resolve_tesseract_executable() -> str | None:
    tesseract_exe = shutil.which("tesseract")
    if tesseract_exe:
        return tesseract_exe

    env_path = (os.environ.get("TESSERACT_CMD") or "").strip()
    if env_path and os.path.exists(env_path):
        try:
            if pytesseract is not None:
                pytesseract.pytesseract.tesseract_cmd = env_path
            return env_path
        except Exception:
            pass

    default_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",            # Linux distro package (apt/dnf)
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
        "/usr/local/opt/tesseract/bin/tesseract",
        "/opt/homebrew/opt/tesseract/bin/tesseract",
    ]
    for default_path in default_paths:
        if os.path.exists(default_path):
            try:
                if pytesseract is not None:
                    pytesseract.pytesseract.tesseract_cmd = default_path
                return default_path
            except Exception:
                pass

    if shutil.which("brew"):
        try:
            brew_result = _run_hidden(["brew", "--prefix", "tesseract"], capture_output=True, text=True, check=False)
            brew_prefix = (brew_result.stdout or "").strip()
        except Exception:
            brew_prefix = ""
        if brew_prefix:
            for candidate_path in (
                os.path.join(brew_prefix, "bin", "tesseract"),
                os.path.join(brew_prefix, "opt", "tesseract", "bin", "tesseract"),
            ):
                if os.path.exists(candidate_path):
                    try:
                        if pytesseract is not None:
                            pytesseract.pytesseract.tesseract_cmd = candidate_path
                        return candidate_path
                    except Exception:
                        pass

    return None


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


def visible_text_from_hierarchy(xml: str | None) -> str:
    """The `text` and `content-desc` of every node in a u2 hierarchy dump,
    lowercased -- i.e. what a person looking at the phone can actually read.

    `classify_block_text` documents that it wants *screen text*, and it decides
    whether an account is challenged, banned, or fine. Handing it the raw XML
    breaks that contract in the worst direction: the dump also carries
    `resource-id`, `class` and `package` on every node, so any marker that
    happens to appear inside an Instagram widget id classifies a working screen
    as a block. That is the same mistake that produced 17 false
    "Human Verification Required" flags out of 29 on 2026-08-11, fixed there and
    left standing here.

    Parse failures return "" rather than the raw XML: no classification is
    better than one made from ids, because the caller's answer stops a profile
    posting and asks a person to go and look at it.
    """
    if not xml:
        return ""
    try:
        import xml.etree.ElementTree as ElementTree
        root = ElementTree.fromstring(xml)
    except Exception:
        return ""
    parts = []
    for node in root.iter():
        for key in ("text", "content-desc"):
            value = str(node.attrib.get(key, "") or "").strip()
            if value:
                parts.append(value)
    return " ".join(parts).lower()


def account_flag_u2(d, logger=None) -> str | None:
    """Classify an IG block screen from the u2 hierarchy: a ban_detection kind
    ("banned" / "human_verification" / "action_block"), or None if the screen
    isn't one. Reads the whole dumped hierarchy, so bans and action-blocks are
    caught too, not just the human-verification checkpoint.

    Module-level on purpose: the reel-upload flow is not part of the u2 bio
    flow's class tree, and a checkpoint stops a reel post exactly as dead as it
    stops a bio edit. Keeping one implementation means a new marker in
    ban_detection reaches every flow at once.

    Reads only the visible text (see `visible_text_from_hierarchy`), and logs
    the phrase it matched. A flag costs a person a trip to the phone, so the log
    has to say what was on screen -- "flagged: human_verification" is not
    something anyone can check, and a wrong one is invisible until somebody
    launches the phone by hand.
    """
    try:
        xml = d.dump_hierarchy()
    except Exception:
        xml = ""
    text = visible_text_from_hierarchy(xml)
    kind, marker = ban_detection.classify_block_text_marker(text)
    if kind:
        where = text.find(marker)
        _emit(logger, "info", "block screen: %s matched %r in: ...%s...",
              kind, marker, text[max(0, where - 60):where + 90])
    return kind


def _u2_describe(sel) -> str:
    """Compact one-line description of a uiautomator2 element for logging:
    its text, content-desc, resource-id, class, bounds and clickable/enabled
    state -- i.e. exactly what the flow matched, so a run log shows what it was
    about to tap before it tapped it."""
    try:
        info = sel.info or {}
    except Exception as exc:
        return f"<could not read element info: {exc}>"
    bounds = info.get("bounds") or {}
    return (
        f"text={info.get('text')!r} desc={info.get('contentDescription')!r} "
        f"id={info.get('resourceName')!r} class={info.get('className')!r} "
        f"clickable={info.get('clickable')} enabled={info.get('enabled')} "
        f"bounds=[{bounds.get('left')},{bounds.get('top')}][{bounds.get('right')},{bounds.get('bottom')}]"
    )


def _u2_find(d, selectors, logger=None, purpose="element"):
    """Return the first present selector from `selectors` (a list of kwargs
    dicts), logging every selector tried and, for the one that matches, the full
    element description. Returns None if none are present."""
    _emit(logger, "info", "u2: locating %s (trying %s selector(s))", purpose, len(selectors))
    for kwargs in selectors:
        sel = d(**kwargs)
        try:
            present = sel.exists
        except Exception as exc:
            _emit(logger, "warning", "u2:   selector %s errored for %s: %s", kwargs, purpose, exc)
            continue
        if present:
            _emit(logger, "info", "u2:   MATCH %s via %s -> %s", purpose, kwargs, _u2_describe(sel))
            return sel
        _emit(logger, "info", "u2:   no match via %s", kwargs)
    _emit(logger, "info", "u2: %s not found by any selector", purpose)
    return None


def _u2_click(d, selectors, logger=None, purpose="element", fallback_ratio=None):
    """Find (with logging) and click the first matching selector, logging the
    element right before the tap. If nothing matches and `fallback_ratio` is
    given, tap that screen ratio instead (also logged, with resolved pixels).
    Returns True if something was tapped."""
    sel = _u2_find(d, selectors, logger=logger, purpose=purpose)
    if sel is not None:
        _emit(logger, "info", "u2: clicking %s -> %s", purpose, _u2_describe(sel))
        try:
            sel.click()
            _emit(logger, "info", "u2: clicked %s", purpose)
            return True
        except Exception as exc:
            _emit(logger, "warning", "u2: click failed for %s: %s", purpose, exc)
    if fallback_ratio is not None:
        fx, fy = fallback_ratio
        try:
            width, height = d.window_size()
        except Exception:
            width, height = (0, 0)
        px = int(width * fx) if width else "?"
        py = int(height * fy) if height else "?"
        _emit(logger, "info", "u2: %s not tappable via selector; FALLBACK tap at ratio (%.2f,%.2f) ~ pixels (%s,%s)", purpose, fx, fy, px, py)
        try:
            d.click(fx, fy)
            return True
        except Exception as exc:
            _emit(logger, "warning", "u2: fallback tap failed for %s: %s", purpose, exc)
    return False


def _ensure_instagram_home_feed_u2(d, target, logger=None) -> None:
    """If Instagram opened on the Reels tab (or any tab that isn't the home
    feed), tap the bottom-nav Home tab to return to the main feed.

    Uses the Home tab's `selected` state -- theme-independent, unlike checking
    whether the screen is dark (in dark mode the home feed is dark too) -- with
    reels-specific UI as a secondary signal. Tapping Home from anywhere lands on
    the feed; if we were already there it just scrolls to top, so it's safe."""
    # Scope to IG's own Home tab by resource-id: the Android system Home button
    # (com.android.launcher3:id/home) also has content-desc "Home", and tapping
    # it backgrounds Instagram (which then cascades into launcher taps / drops).
    home_selectors = [
        {"resourceId": "com.instagram.android:id/feed_tab"},
        {"resourceIdMatches": r"com\.instagram\.android:id/(feed_tab|main_home_tab)"},
        {"description": "Home", "resourceIdMatches": r"com\.instagram\.android:id/.*"},
    ]

    # Already on the feed? (IG's Home tab reports selected=true.)
    try:
        sel = d(resourceId="com.instagram.android:id/feed_tab")
        if sel.exists and (sel.info or {}).get("selected") is True:
            return
    except Exception:
        pass

    # Secondary signal (for the log): reels/clips viewer present.
    on_reels = False
    for kw in ({"resourceIdMatches": "(?i).*clips_viewer.*"}, {"descriptionContains": "reel"}, {"textMatches": "(?i)^reels$"}):
        try:
            if d(**kw).exists:
                on_reels = True
                break
        except Exception:
            continue

    _emit(logger, "info", "u2: Instagram not on the home feed for %s (reels marker=%s); tapping Home", target, on_reels)
    _u2_click(d, home_selectors, logger=logger, purpose="Home tab (leave Reels -> feed)", fallback_ratio=(0.08, 0.95))
    time.sleep(1.5)


def _sleep_after_instagram_launch(target: str, logger=None, delay_seconds: int = 3) -> None:
    _emit(logger, "info", "Waiting %s seconds for Instagram to open completely on %s", delay_seconds, target)
    time.sleep(delay_seconds)


# Matches the XML hierarchy that `uiautomator dump /dev/tty` streams to stdout.
# The command appends a trailing "UI hierchary dumped to: ..." status line we
# do not want, so we extract exactly the <?xml ...>...</hierarchy> span.
_UI_DUMP_XML_RE = re.compile(rb"<\?xml.*?</hierarchy>", re.DOTALL)

# Sentinel returned by the fast exec-out dump path to mean "exec-out produced
# nothing usable here; the caller should fall back to the classic dump+pull
# path" -- as distinct from returning None, which (matching the original
# behavior) means the dump ran but reported no idle state / an error.
_DUMP_TRY_FALLBACK = object()
# Sentinel meaning the adb tunnel to the device dropped (offline / not found).
_DUMP_OFFLINE = object()

# adb error fragments that indicate the device tunnel is down (not just a stale
# UI). When we see these we reconnect rather than treating it as a UI failure.
_OFFLINE_MARKERS = (
    "device offline",
    "not found",
    "no devices",
    "no such device",
    "device unauthorized",
    "closed",
)


def _adb_reconnect_device(target: str, logger=None) -> bool:
    """Bring a dropped Multilogin adb tunnel back online: disconnect + connect,
    then poll get-state until the device is usable. Returns True if recovered."""
    _emit(logger, "info", "Device %s appears offline; attempting to reconnect", target)
    try:
        _adb_run("disconnect", target, check=False, capture_output=True, text=True, timeout=15)
        _adb_run("connect", target, check=False, capture_output=True, text=True, timeout=15)
    except Exception as exc:
        _emit(logger, "warning", "Reconnect command failed for %s: %s", target, exc)
        return False
    for _ in range(8):
        try:
            state = _adb_run("-s", target, "get-state", check=False, capture_output=True, text=True, timeout=10)
            out = (state.stdout or state.stderr or "").strip().lower()
        except Exception:
            out = ""
        if out == "device":
            _emit(logger, "info", "Device %s reconnected and back online", target)
            return True
        time.sleep(1)
    _emit(logger, "warning", "Device %s did not come back online after reconnect", target)
    return False


def _adb_capture_ui_dump(target: str, logger=None, idle_retries: int = 1):
    """Capture and parse the current UI hierarchy for `target`.

    Fast path: `adb exec-out uiautomator dump /dev/tty` streams the XML back
    over the existing adb connection in a single round trip -- no on-device
    file and no separate `adb pull`. Self-heals a dropped tunnel by
    reconnecting once, and falls back to the classic dump-to-file + pull path
    if exec-out produces nothing usable. `idle_retries` controls how many extra
    times to re-dump on a transient 'could not get idle state'; pass 0 when a
    fast failure is preferable (e.g. probing whether a tap left an animating
    screen).
    """
    result = _adb_capture_ui_dump_exec_out(target, logger=logger, idle_retries=idle_retries)
    if result is _DUMP_OFFLINE:
        # The adb tunnel dropped mid-session -- reconnect once and retry.
        if _adb_reconnect_device(target, logger=logger):
            result = _adb_capture_ui_dump_exec_out(target, logger=logger, idle_retries=idle_retries)
        if result is _DUMP_OFFLINE:
            return None
    if result is _DUMP_TRY_FALLBACK:
        return _adb_capture_ui_dump_pull(target, logger=logger)
    return result


def _adb_capture_ui_dump_exec_out(target: str, logger=None, idle_retries: int = 1):
    for attempt in range(idle_retries + 1):
        try:
            _emit(logger, "info", "Dumping UI hierarchy for %s", target)
            result = _run_hidden(
                ["adb", "-s", target, "exec-out", "uiautomator", "dump", "/dev/tty"],
                shell=False,
                check=False,
                capture_output=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            _emit(logger, "warning", "exec-out UI dump timed out for %s; treating device as offline", target)
            return _DUMP_OFFLINE
        except Exception as exc:
            _emit(logger, "warning", "exec-out UI dump could not run for %s: %s", target, exc)
            return _DUMP_TRY_FALLBACK

        stdout_bytes = result.stdout or b""
        match = _UI_DUMP_XML_RE.search(stdout_bytes)
        if match:
            try:
                return ET.fromstring(match.group(0))
            except Exception as exc:
                _emit(logger, "warning", "Failed to parse exec-out UI dump for %s: %s", target, exc)
                return _DUMP_TRY_FALLBACK

        combined = (stdout_bytes + b" " + (result.stderr or b"")).decode("utf-8", "ignore").strip()
        lowered = combined.lower()
        if any(marker in lowered for marker in _OFFLINE_MARKERS):
            _emit(logger, "warning", "Device %s offline during UI dump: %s", target, combined or "<no output>")
            return _DUMP_OFFLINE
        if "could not get idle state" in lowered:
            # Transient: the UI is still animating/loading. Re-dump quickly
            # instead of failing the whole verification.
            if attempt < idle_retries:
                _emit(logger, "info", "UI not idle for %s; retrying dump (%s/%s)", target, attempt + 1, idle_retries)
                time.sleep(1.5)
                continue
            _emit(logger, "warning", "uiautomator dump could not get idle state for %s; ignoring stale UI dump", target)
            return None
        if "error:" in lowered:
            _emit(logger, "warning", "uiautomator dump failed for %s; ignoring stale UI dump", target)
            return None
        # No XML and no recognizable error marker -> exec-out unusable here.
        return _DUMP_TRY_FALLBACK
    return None


def _adb_capture_ui_dump_pull(target: str, logger=None):
    dump_remote = "/sdcard/instagram_ui_dump.xml"
    dump_local = Path(tempfile.gettempdir()) / f"instagram_ui_dump_{target}.xml"

    try:
        _emit(logger, "info", "Falling back to pull-based UI dump for %s", target)
        result = _adb_run(
            "-s", target, "shell", f"uiautomator dump {dump_remote}",
            check=True,
            capture_output=True,
            text=True,
        )
        dump_stdout = result.stdout.strip()
        dump_stderr = result.stderr.strip()
        if dump_stdout:
            _emit(logger, "info", "uiautomator dump stdout for %s: %s", target, dump_stdout)
        if dump_stderr:
            _emit(logger, "info", "uiautomator dump stderr for %s: %s", target, dump_stderr)
            if "error:" in dump_stderr.lower() or "could not get idle state" in dump_stderr.lower() or "failed" in dump_stderr.lower():
                _emit(logger, "warning", "uiautomator dump failed for %s; ignoring stale UI dump", target)
                return None

        result = _adb_run(
            "-s", target, "pull", dump_remote, dump_local,
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


def _adb_get_foreground_activity(target: str, logger=None) -> str | None:
    """Return the foreground Instagram activity via dumpsys, or None.

    This does NOT require the UI to be idle, so it works even when the home
    feed is auto-playing a video/reel -- the exact case where `uiautomator
    dump` hangs and fails with 'could not get idle state'.
    """
    for command in ("dumpsys activity activities", "dumpsys window"):
        try:
            result = _adb_run(
                "-s", target, "shell", command,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            continue
        out = result.stdout or ""
        if "com.instagram.android" not in out:
            continue
        for pattern in (
            r"topResumedActivity[^\n]*?(com\.instagram\.android/[\w./]+)",
            r"mResumedActivity[^\n]*?(com\.instagram\.android/[\w./]+)",
            r"ResumedActivity[^\n]*?(com\.instagram\.android/[\w./]+)",
            r"mCurrentFocus[^\n]*?(com\.instagram\.android/[\w./]+)",
            r"mFocusedApp[^\n]*?(com\.instagram\.android/[\w./]+)",
        ):
            match = re.search(pattern, out)
            if match:
                return match.group(1)
    return None


def _adb_is_instagram_foreground(target: str, logger=None) -> bool:
    activity = _adb_get_foreground_activity(target, logger=logger)
    if activity:
        _emit(logger, "info", "Foreground Instagram activity for %s: %s", target, activity)
        return True
    return False


def _adb_is_instagram_home_feed_visible(target: str, logger=None) -> bool:
    # Primary check: is Instagram itself in the foreground? This uses dumpsys,
    # which works even while the feed auto-plays a video (uiautomator dump
    # cannot -- it hangs waiting for an 'idle' UI that never comes). Right after
    # launching to MainTabActivity, Instagram being foreground means the feed
    # is showing; downstream steps verify the specific screens they need.
    if _adb_is_instagram_foreground(target, logger=logger):
        _emit(logger, "info", "Instagram is in the foreground for %s; treating home feed as visible", target)
        return True

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
        if (has_stories_row or has_feed_resource or has_feed_content) and not (has_profile_tab_selected or has_reels_tab_selected):
            _emit(logger, "info", "Instagram home feed appears visible for %s by content markers despite missing explicit home tab", target)
            return True
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


def _adb_ensure_instagram_feed_visible(target: str, adb_client, logger=None, max_attempts: int = 5, retry_delay_seconds: int = 5) -> bool:
    _emit(logger, "info", "Ensuring Instagram feed is visible for %s (up to %s attempts)", target, max_attempts)
    for attempt in range(1, max_attempts + 1):
        if _adb_is_instagram_home_feed_visible(target, logger=logger):
            _emit(logger, "info", "Instagram feed verification succeeded on attempt %s/%s for %s", attempt, max_attempts, target)
            return True
        
        if attempt < max_attempts:
            _emit(logger, "info", "Feed verification failed on attempt %s/%s; waiting %s seconds before retry", attempt, max_attempts, retry_delay_seconds)
            time.sleep(retry_delay_seconds)
        else:
            _emit(logger, "warning", "Instagram feed verification failed after %s attempts for %s", max_attempts, target)

    return False


def _adb_get_screen_size(target: str, logger=None) -> tuple[int, int] | None:
    try:
        result = _adb_run(
            "-s", target, "shell", "wm size",
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


def _adb_tap(target: str, x: int, y: int, adb_client, logger=None, description: str | None = None) -> None:
    if logger is not None:
        desc = description or "tap"
        _emit(logger, "info", "%s at %s for %s", desc, (x, y), target)
    adb_client.run_command(f"adb -s {target} shell {tap(x, y)}")


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


def _adb_go_to_home_feed(target: str, adb_client, logger=None) -> None:
    """Force-navigate to the Instagram home feed by tapping the Home (house)
    button at the bottom-left of the nav bar.

    Instagram sometimes opens on the Reels tab, and both Reels and the feed
    auto-play video, so uiautomator dump can't locate the button (it hangs on a
    non-idle UI). The house icon sits at a stable far-left position -- far from
    the Reels icon (2nd from left) -- and tapping it goes to the feed from any
    tab (or scrolls to the top if already on the feed), so it is always safe.
    """
    x, y = _adb_get_relative_point(target, 0.09, 0.95, logger=logger)
    _emit(logger, "info", "Tapping Home (house) button to ensure the home feed for %s", target)
    _adb_tap(target, x, y, adb_client, logger=logger, description="Tapping Home (house) button")
    time.sleep(2)


def _adb_find_instagram_activity_heart_center(target: str, logger=None) -> tuple[int, int] | None:
    """Locate the Activity/Notifications heart in the top-right of the home
    feed's top bar. Strict: it matches only nodes whose content-desc/resource-id
    say 'notification'/'activity' AND excludes post 'Like'/'Comment' hearts, and
    requires the top-right region -- so it is never confused with a post's like
    button. Returns None if the top bar (and heart) isn't visible/dumpable."""
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen

    for node in root.iter():
        attrs = node.attrib
        desc = str(attrs.get("content-desc", "") or "").strip().lower()
        rid = str(attrs.get("resource-id", "") or "").strip().lower()
        # Exclude the per-post Like / Comment hearts explicitly.
        if "like" in desc or "comment" in desc:
            continue
        is_activity = (
            "notification" in desc or "activity" in desc
            or "notification" in rid or "activity" in rid or "news" in rid
        )
        if not is_activity:
            continue

        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", "") or "")
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        # Must be in the top-right corner (the top app bar), not down in the feed.
        if cx > int(width * 0.6) and cy < int(height * 0.15):
            return (cx, cy)
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


def _adb_find_instagram_follow_button_centers(target: str, logger=None, max_buttons: int = 5) -> list[tuple[int, int]]:
    """Locate real, tappable "Follow" buttons from the live UI hierarchy.

    This is far more reliable than screenshot/OpenCV analysis: it returns the
    exact centre of each Android view whose visible text (or content-desc) is
    exactly "Follow"/"Follow back", so taps land on the button and never on an
    avatar or username (which would open a profile). The exact match also
    excludes "Following" (tapping which would UNFOLLOW), "Followers",
    "Follow requests", etc. Returns an empty list when the current screen has
    no Follow buttons (e.g. the notifications tab did not actually open).
    """
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return []

    follow_labels = {"follow", "follow back"}
    clickable_hits: list[tuple[int, int, int]] = []
    any_hits: list[tuple[int, int, int]] = []
    for node in root.iter():
        attrs = node.attrib
        text_value = str(attrs.get("text", "") or "").strip().lower()
        content_desc = str(attrs.get("content-desc", "") or "").strip().lower()
        if text_value not in follow_labels and content_desc not in follow_labels:
            continue

        enabled = attrs.get("enabled", "true").lower() != "false"
        if not enabled:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        any_hits.append((cy, cx, cy))
        if attrs.get("clickable", "false").lower() == "true":
            clickable_hits.append((cy, cx, cy))

    chosen = clickable_hits if clickable_hits else any_hits
    # Sort top-to-bottom so follows happen in a natural reading order.
    chosen.sort(key=lambda item: (item[0], item[1]))
    seen: set[tuple[int, int]] = set()
    centers: list[tuple[int, int]] = []
    for _, cx, cy in chosen:
        key = (cx, cy)
        if key in seen:
            continue
        seen.add(key)
        centers.append((cx, cy))
        if len(centers) >= max_buttons:
            break
    return centers


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


_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _media_store_collection(remote_media_path: str) -> str:
    """The MediaStore collection a pushed file lands in, from its extension.

    Videos live in `external/video/media` and images in `external/images/media`.
    Querying the generic `external/file` table for a video returned an empty
    result on the MLX phones, which is half of why the index check never
    confirmed anything.
    """
    suffix = Path(remote_media_path).suffix.lower()
    if suffix in _VIDEO_SUFFIXES:
        return "content://media/external/video/media"
    if suffix in _IMAGE_SUFFIXES:
        return "content://media/external/images/media"
    return "content://media/external/file"


def _adb_wait_for_media_store_index(target: str, remote_media_path: str, logger=None, max_attempts: int = 4, delay_seconds: float = 0.5) -> bool:
    """Best-effort poll for MediaStore to index a just-pushed file before the picker opens.

    The MEDIA_SCANNER_SCAN_FILE broadcast is fire-and-forget, so without this the flow
    can open the Instagram media picker before the new file appears in it. Some
    Android/OEM builds restrict `content query`, so a failure here is not fatal —
    callers should proceed with the existing fixed-sleep behavior either way.

    This used to query `external/file` with `_data='<path>'` and never once
    succeeded — every push spent ~9 s exhausting its attempts and then proceeded
    unconfirmed. Two reasons, both fixed here and both verified on a real MLX
    phone: `_data` is the raw filesystem path, deprecated and unqueryable under
    scoped storage on Android 10+; and a video is not in the generic `file`
    table. `_display_name='<basename>'` against the right collection resolves
    immediately (it returned `content://media/external/video/media/110`).

    `_data` is still tried as a fallback for older builds where it does work.
    """
    collection = _media_store_collection(remote_media_path)
    basename = Path(remote_media_path).name

    def query(where_clause: str) -> bool:
        command = [
            "adb", "-s", target, "shell",
            "content", "query",
            "--uri", shlex.quote(collection),
            "--projection", "_id",
            # adb joins the post-`shell` arguments without escaping them, so this
            # is quoted for the *device* shell that ultimately parses it.
            "--where", shlex.quote(where_clause),
        ]
        result = _run_hidden(command, shell=False, check=False, capture_output=True, text=True)
        output = (result.stdout or "").strip()
        return bool(output) and "no result" not in output.lower()

    for attempt in range(1, max_attempts + 1):
        try:
            if query(f"_display_name='{basename}'"):
                _emit(logger, "info", "MediaStore indexed %s for %s after %s attempt(s)",
                      remote_media_path, target, attempt)
                return True
            # Legacy builds where the deprecated path column still answers.
            if attempt == max_attempts and query(f"_data='{remote_media_path}'"):
                _emit(logger, "info", "MediaStore indexed %s for %s via _data (legacy)",
                      remote_media_path, target)
                return True
        except Exception as exc:
            _emit(logger, "info", "MediaStore index check failed for %s: %s", target, exc)
            return False
        if attempt < max_attempts:
            time.sleep(delay_seconds)

    _emit(logger, "info", "MediaStore index not confirmed for %s after %s attempts; proceeding anyway", remote_media_path, max_attempts)
    return False


def _adb_push_media_to_device(target: str, local_media_path: str, remote_media_path: str,
                              logger=None, max_attempts: int = 3,
                              retry_delay_seconds: float = 5.0) -> bool:
    """Retries the push itself on failure -- requested 2026-09-01 after a real
    failure that was purely a network hiccup on the tunnel to a remote cloud
    phone (`file_sync_client.cpp:473 protocol fault: failed to read stat
    response: Success`), not a real error. `_adb_push_media_to_device_once`
    already treats "exited non-zero but the file matches on the device" as
    success; this covers the case where it doesn't -- the file genuinely
    never arrived, and a second attempt likely just works, same as a flaky
    network transfer usually does on a plain retry."""
    for attempt in range(1, max_attempts + 1):
        if _adb_push_media_to_device_once(target, local_media_path, remote_media_path,
                                          logger=logger):
            return True
        if attempt < max_attempts:
            _emit(logger, "warning", "adb push attempt %s/%s failed for %s; retrying in %ss",
                 attempt, max_attempts, target, retry_delay_seconds)
            time.sleep(retry_delay_seconds)
    return False


def _adb_push_media_to_device_once(target: str, local_media_path: str, remote_media_path: str, logger=None) -> bool:
    local_path = Path(local_media_path)
    if not local_path.exists() or not local_path.is_file():
        _emit(logger, "warning", "Local story media file does not exist: %s", local_media_path)
        return False

    try:
        remote_dir = os.path.dirname(remote_media_path)
        _emit(logger, "info", "Ensuring remote directory %s exists on %s", remote_dir, target)
        mkdir_result = _run_hidden(
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
        try:
            result = _run_hidden(
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
        except subprocess.CalledProcessError as exc:
            # adb can transfer every byte and still exit non-zero, typically
            # `failed to read copy response` when the tunnel to a cloud phone
            # hiccups on the way back. Its own output says so in the same
            # breath: "1 file pushed, 0 skipped ... (22833002 bytes in 7.100s)"
            # followed by the error. Believing the exit code there throws away
            # a post that is already on the device -- seen twice on 2026-08-10,
            # both on the larger clips over a slow link.
            #
            # The text is not the evidence, though. Hash the file on the device
            # and only continue if it matches the local one, so a genuinely
            # half-written push still fails.
            stdout = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            if not _adb_verify_remote_media_matches_local(
                    target, local_media_path, remote_media_path, logger=logger):
                _emit(logger, "warning",
                      "adb push failed for %s and the file on the device does not "
                      "match the local one: %s", target, stderr or stdout)
                return False
            _emit(logger, "warning",
                  "adb push exited non-zero for %s (%s) but the file on the device "
                  "matches the local hash -- treating it as delivered",
                  target, stderr or stdout)
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
        scan_result = _run_hidden(
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

        _adb_wait_for_media_store_index(target, remote_media_path, logger=logger)

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
        result = _run_hidden(
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
        result = _run_hidden(
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
        result2 = _run_hidden(
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
            dir_result = _run_hidden(
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
    # Retry capturing the UI dump a few times — uiautomator can return transient null/root errors.
    root = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        root = _adb_capture_ui_dump(target, logger=logger)
        if root is not None:
            break
        if logger is not None:
            _emit(logger, "info", "UI dump returned no data for %s (attempt %s/%s); retrying", target, attempt, max_attempts)
        time.sleep(0.25)

    screen = _adb_get_screen_size(target, logger=logger)
    if root is None:
        return None

    width, height = (1080, 2340) if screen is None else screen
    best_center = None
    best_area = -1
    max_x = int(width * 0.55)
    max_y = int(height * 0.40)

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
        if cy <= int(height * 0.10) or cy > max_y:
            continue

        width_px = x2 - x1
        height_px = y2 - y1
        if min(width_px, height_px) < 16:
            continue

        if cx > max_x:
            continue

        area = width_px * height_px
        if area > best_area:
            best_area = area
            best_center = (cx, cy)

    if best_center is None and logger is not None:
        _emit(logger, "info", "No story row plus found in left-top region; widening search region to %s%% height for %s", int(max_y / height * 100), target)

    return best_center


def _adb_find_instagram_gallery_center(target: str, logger=None) -> tuple[int, int] | None:
    return _adb_find_ui_element_center(target, ("gallery", "recent", "photos", "camera roll"), logger=logger)


def _adb_find_instagram_reel_media_thumbnail_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen

    target_y = int(height * 0.30)
    min_y = int(height * 0.18)
    max_y = int(height * 0.60)

    candidates: list[tuple[int, int, int, int, int, int]] = []
    for node in root.iter():
        attrs = node.attrib
        text = (attrs.get("text") or "").lower()
        desc = (attrs.get("content-desc") or attrs.get("contentDescription") or "").lower()
        resource = (attrs.get("resource-id") or "").lower()
        node_class = (attrs.get("class") or "").lower()
        combined = " ".join((text, desc, resource, node_class)).strip()

        if any(skip in combined for skip in ("draft", "drafts", "templates", "new reel", "reel option", "story", "post", "share", "send", "button", "next", "continue")):
            continue
        if not any(keyword in combined for keyword in ("photo", "image", "thumbnail", "video", "recent", "gallery", "media", "preview", "cover")):
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        if x1 < int(width * 0.05) and x2 > int(width * 0.95):
            continue
        if y1 < int(height * 0.12) or y2 > int(height * 0.85):
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        if cy < min_y or cy > max_y:
            continue

        area = (x2 - x1) * (y2 - y1)
        if area < 5000:
            continue

        if "imageview" not in node_class and "image" not in node_class and "thumbnail" not in combined and "gallery" not in combined:
            continue

        candidates.append((abs(cy - target_y), -area, cx, cy, x1, y1))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    _, _, cx, cy, _, _ = candidates[0]
    return cx, cy


def _adb_find_instagram_media_selection_center(target: str, logger=None) -> tuple[int, int] | None:
    media_center = _adb_find_instagram_reel_media_thumbnail_center(target, logger=logger)
    if media_center is not None:
        return media_center

    screen = _adb_get_screen_size(target, logger=logger)
    if screen is None:
        return _adb_get_relative_point(target, 0.50, 0.30, logger=logger)

    width, height = screen
    return int(width * 0.50), int(height * 0.30)


def _adb_find_instagram_next_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen
    candidates: list[tuple[int, int, int]] = []
    keywords = ("next", "forward", "continue", "arrow")

    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if not any(keyword in combined for keyword in keywords):
            continue

        clickable = attrs.get("clickable", "false").lower() == "true"
        enabled = attrs.get("enabled", "true").lower() != "false"
        if not clickable or not enabled:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        area = (x2 - x1) * (y2 - y1)
        if area < 1500:
            continue

        if "add location" in combined:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        score = 0
        if "next" in combined:
            score += 300
        if "continue" in combined:
            score += 175
        if "forward" in combined:
            score += 125
        if "arrow" in combined:
            score += 80
        if "share" in combined:
            score += 40
        if "button" in attrs.get("class", "").lower():
            score += 25
        if cx > width * 0.65:
            score += 20
        if cy > height * 0.70:
            score += 20
        score += min(area // 1200, 50)

        candidates.append((score, cx, cy))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])
    return candidates[0][1], candidates[0][2]


def _adb_capture_screen_cv2(target: str, logger=None):
    """Capture the device screen as a BGR cv2 image via a single exec-out call.

    Uses `adb exec-out screencap -p` (binary-safe) and decodes the PNG bytes
    in-memory, avoiding an on-device file and a separate `adb pull`. Returns
    None if capture/decode fails so callers can fall back to their own method.
    """
    if cv2 is None or np is None:
        return None
    try:
        result = _run_hidden(
            ["adb", "-s", target, "exec-out", "screencap", "-p"],
            shell=False,
            check=False,
            capture_output=True,
        )
    except Exception as exc:
        _emit(logger, "warning", "Unable to capture screen image for %s: %s", target, exc)
        return None
    data = result.stdout or b""
    if not data:
        return None
    try:
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as exc:
        _emit(logger, "warning", "Unable to decode screen image for %s: %s", target, exc)
        return None


def _classify_region_blue(image, x1: int, y1: int, x2: int, y2: int) -> bool | None:
    """Return whether the [x1,y1][x2,y2] region of `image` is mostly IG-blue."""
    height, width = image.shape[:2]
    x1_clamped = max(0, min(width - 1, x1))
    y1_clamped = max(0, min(height - 1, y1))
    x2_clamped = max(0, min(width, x2))
    y2_clamped = max(0, min(height, y2))
    if x2_clamped <= x1_clamped or y2_clamped <= y1_clamped:
        return None

    region = image[y1_clamped:y2_clamped, x1_clamped:x2_clamped]
    if region.size == 0:
        return None

    region_hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    lower_blue = np.array([100, 70, 70], dtype=np.uint8)
    upper_blue = np.array([140, 255, 255], dtype=np.uint8)
    blue_mask = cv2.inRange(region_hsv, lower_blue, upper_blue)
    blue_pixels = int(cv2.countNonZero(blue_mask))
    blue_ratio = blue_pixels / max(1, region.size // 3)
    return blue_ratio >= 0.02


def _adb_find_instagram_share_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    # The real reel/story "Share" button that posts carries a visible "Share"
    # *text* label on a blue background. Small share/send glyphs elsewhere only
    # carry a "share" content-desc (no visible text) and are not blue. Keep the
    # two groups apart so the text-labelled button is always preferred, and
    # only ever tap a candidate whose background is actually verified blue.
    text_candidates: list[tuple[int, int, int, str]] = []
    desc_candidates: list[tuple[int, int, int, str]] = []
    for node in root.iter():
        attrs = node.attrib
        text_value = str(attrs.get("text", "") or "").strip().lower()
        content_desc = str(attrs.get("content-desc", "") or "").strip().lower()
        has_text_share = "share" in text_value
        has_desc_share = "share" in content_desc
        if not has_text_share and not has_desc_share:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        area = (x2 - x1) * (y2 - y1)
        if area < 500:
            continue

        clickable = attrs.get("clickable", "false").lower() == "true"
        enabled = attrs.get("enabled", "true").lower() != "false"
        if not clickable or not enabled:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        score = area
        if "post" in text_value or "post" in content_desc:
            score += 50
        if "button" in attrs.get("class", "").lower() or "button" in attrs.get("resource-id", "").lower():
            score += 25
        entry = (score, cx, cy, bounds)
        if has_text_share:
            text_candidates.append(entry)
        else:
            desc_candidates.append(entry)

    if not text_candidates and not desc_candidates:
        return None

    text_candidates.sort(reverse=True, key=lambda item: item[0])
    desc_candidates.sort(reverse=True, key=lambda item: item[0])

    # Capture the screen once and reuse it across all candidates -- the screen
    # doesn't change between these checks, so re-screenshotting per candidate
    # was pure overhead. If this capture fails (returns None), each check falls
    # back to taking its own screenshot, matching the original behavior.
    shared_image = _adb_capture_screen_cv2(target, logger=logger)

    # Only tap a candidate whose background is verified Instagram-blue. Check
    # the visible-text "Share" button(s) first, then content-desc-only ones.
    any_verifiable = False
    for _, cx, cy, bounds in text_candidates + desc_candidates:
        blue_verified = _adb_is_instagram_share_button_blue(target, bounds, logger=logger, image=shared_image)
        if blue_verified is True:
            return cx, cy
        if blue_verified is False:
            any_verifiable = True

    # No blue-verified candidate. If the colour could not be checked for ANY
    # candidate (OpenCV/Pillow unavailable or every screenshot failed), fall
    # back to the best visible-text "Share" label -- never a content-desc-only
    # glyph, which is what caused the mis-tap on the small share icon.
    if not any_verifiable and text_candidates:
        _, cx, cy, _ = text_candidates[0]
        _emit(logger, "info", "Share colour check unavailable; using visible-text Share candidate for %s", target)
        return cx, cy

    _emit(logger, "info", "No blue-verified Share button found for %s", target)
    return None


def _adb_is_instagram_share_button_blue(target: str, bounds: str, logger=None, image=None) -> bool | None:
    if cv2 is None or Image is None:
        return None

    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None

    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        return None

    # Fast path: classify against a screenshot the caller already captured.
    if image is not None:
        return _classify_region_blue(image, x1, y1, x2, y2)

    # Fallback path: capture our own screenshot. Kept as the original, proven
    # screencap + pull sequence so behavior is unchanged when no shared image
    # is available.
    temp_remote = "/sdcard/instagram_share_button.png"
    temp_local = Path(tempfile.gettempdir()) / f"instagram_share_button_{target}.png"
    try:
        _adb_run("-s", target, "shell", f"screencap -p {temp_remote}", check=True)
        _adb_run("-s", target, "pull", temp_remote, temp_local, check=True)
        image = cv2.imread(str(temp_local))
        if image is None:
            return None
        return _classify_region_blue(image, x1, y1, x2, y2)
    except subprocess.CalledProcessError as exc:
        _emit(logger, "warning", "Unable to verify share button color for %s: %s", target, exc)
        return None
    except Exception as exc:
        _emit(logger, "warning", "Unexpected error verifying share button color for %s: %s", target, exc)
        return None
    finally:
        try:
            temp_local.unlink(missing_ok=True)
        except OSError:
            pass


def _adb_find_instagram_bottom_blue_action_center(
    target: str, labels: tuple[str, ...] = ("next", "share"), logger=None
) -> tuple[tuple[int, int], str] | None:
    """Locate the primary blue action button in the bottom-right of the reel
    composer -- the button that advances/posts. Its visible label is sometimes
    "Next" and sometimes "Share" depending on the Instagram version/screen, but
    it is always a blue button in the lower part of the screen, right-of-centre.
    Returns ((cx, cy), matched_label) for the best blue-verified candidate, or
    None.

    Mirrors :func:`_adb_find_instagram_share_center`: only a candidate whose
    background is verified Instagram-blue is returned. If the colour cannot be
    checked for ANY candidate (OpenCV/Pillow unavailable or every screenshot
    failed), it falls back to the best labelled bottom-right candidate so the
    flow still advances.
    """
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen
    wanted = tuple(label.lower() for label in labels if label)

    candidates: list[tuple[int, int, int, str, str]] = []  # (score, cx, cy, bounds, label)
    for node in root.iter():
        attrs = node.attrib
        text_value = str(attrs.get("text", "") or "").strip().lower()
        desc_value = str(attrs.get("content-desc", "") or "").strip().lower()

        matched_label = None
        exact = False
        for label in wanted:
            if text_value == label or desc_value == label:
                matched_label = label
                exact = True
                break
        if matched_label is None:
            for label in wanted:
                if label in text_value or label in desc_value:
                    matched_label = label
                    break
        if matched_label is None:
            continue

        clickable = attrs.get("clickable", "false").lower() == "true"
        enabled = attrs.get("enabled", "true").lower() != "false"
        if not clickable or not enabled:
            continue

        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", "") or "")
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        area = (x2 - x1) * (y2 - y1)
        if area < 500:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        # Lower part of the screen, right-of-centre. A full-width "Share" sits
        # centre-bottom, so allow from ~0.40 width; never the left side, and
        # never the top bar (which is where a small "Next" arrow can also live).
        if cx < int(width * 0.40) or cy < int(height * 0.58):
            continue

        score = area
        if exact:
            score += 400
        if matched_label == "share":
            score += 150
        if "post" in text_value or "post" in desc_value:
            score += 100
        candidates.append((score, cx, cy, attrs.get("bounds", ""), matched_label))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])

    # Capture the screen once and reuse it across the blue checks.
    shared_image = _adb_capture_screen_cv2(target, logger=logger)
    any_verifiable = False
    for _, cx, cy, bounds, label in candidates:
        blue_verified = _adb_is_instagram_share_button_blue(target, bounds, logger=logger, image=shared_image)
        if blue_verified is True:
            _emit(logger, "info", "Found blue '%s' action button at (%s, %s) for %s", label, cx, cy, target)
            return (cx, cy), label
        if blue_verified is False:
            any_verifiable = True

    if not any_verifiable:
        _, cx, cy, _, label = candidates[0]
        _emit(logger, "info", "Blue colour check unavailable; using best bottom-right '%s' candidate at (%s, %s) for %s", label, cx, cy, target)
        return (cx, cy), label

    _emit(logger, "info", "No blue-verified Next/Share action button found for %s", target)
    return None


def _adb_ocr_find_text_center(
    target: str,
    keywords: tuple[str, ...],
    logger=None,
    min_x_frac: float = 0.0,
    min_y_frac: float = 0.0,
    max_x_frac: float = 1.0,
    max_y_frac: float = 1.0,
    min_conf: float = 45.0,
) -> tuple[tuple[int, int], str] | None:
    """Locate an on-screen button by OCR and return ((cx, cy), matched_word).

    Reads a screenshot (which captures a video frame fine) rather than the UI
    dump, so it works on the reel PREVIEW and other auto-playing screens where
    `uiautomator dump` never reaches idle. Returns the centre of the
    highest-confidence word that EXACTLY matches one of `keywords`, restricted
    to the given fractional screen region. Returns None when OCR is unavailable
    or nothing matches -- callers must fail safe, never blind-tap.
    """
    if pytesseract is None or Image is None or cv2 is None:
        return None
    if not _resolve_tesseract_executable():
        return None
    image = _adb_capture_screen_cv2(target, logger=logger)
    if image is None:
        return None

    height, width = image.shape[:2]
    x_min, x_max = int(width * min_x_frac), int(width * max_x_frac)
    y_min, y_max = int(height * min_y_frac), int(height * max_y_frac)
    wanted = {k.lower() for k in keywords if k}

    try:
        data = pytesseract.image_to_data(
            Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)),
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:
        _emit(logger, "warning", "OCR text search failed for %s: %s", target, exc)
        return None

    best: tuple[float, int, int, str] | None = None  # (conf, cx, cy, word)
    for i in range(len(data.get("text", []))):
        word = (data["text"][i] or "").strip().lower()
        if word not in wanted:
            continue
        try:
            conf = float(data.get("conf", [])[i])
        except (ValueError, TypeError, IndexError):
            conf = -1.0
        if conf < min_conf:
            continue
        left, top = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        cx, cy = left + w // 2, top + h // 2
        if not (x_min <= cx <= x_max and y_min <= cy <= y_max):
            continue
        if best is None or conf > best[0]:
            best = (conf, cx, cy, word)

    if best is None:
        return None
    _emit(logger, "info", "OCR located '%s' (conf=%.0f) at (%s, %s) for %s", best[3], best[0], best[1], best[2], target)
    return (best[1], best[2]), best[3]


def _adb_find_instagram_dialog_action_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    screen = _adb_get_screen_size(target, logger=logger)
    if root is None:
        return None

    width, height = (1080, 2340) if screen is None else screen
    candidates: list[tuple[int, int, int]] = []
    keywords = ("next", "share", "ok", "continue", "done", "send")

    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if not any(keyword in combined for keyword in keywords):
            continue

        clickable = attrs.get("clickable", "false").lower() == "true"
        enabled = attrs.get("enabled", "true").lower() != "false"
        if not clickable or not enabled:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        area = (x2 - x1) * (y2 - y1)
        if area < 400:
            continue

        if "add location" in combined:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        score = cy + area // 100
        if "next" in combined:
            score += 200
        if "continue" in combined:
            score += 150
        if "share" in combined:
            score += 100
        if "ok" in combined:
            score += 50
        if "send" in combined:
            score += 50
        if cx > width * 0.5:
            score += 25
        if cy > height * 0.55:
            score += 20

        candidates.append((score, cx, cy))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])
    return candidates[0][1], candidates[0][2]


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
    return (
        _adb_find_instagram_share_center(target, logger=logger) is not None
        or _adb_find_instagram_next_center(target, logger=logger) is not None
        or _adb_find_instagram_dialog_action_center(target, logger=logger) is not None
    )


def _adb_is_instagram_reel_composer_visible(target: str, logger=None) -> bool:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return False

    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        if any(keyword in combined for keyword in ("reel", "gallery", "recent", "photos", "camera roll", "thumbnail", "post", "share", "next")):
            return True
    return False


def _adb_wait_for_instagram_story_composer(target: str, logger=None, max_attempts: int = 4, delay_seconds: int = 2) -> bool:
    for attempt in range(1, max_attempts + 1):
        if _adb_is_instagram_story_composer_visible(target, logger=logger):
            return True
        if attempt < max_attempts:
            time.sleep(delay_seconds)
    return False


def _adb_wait_for_instagram_reel_composer(target: str, logger=None, max_attempts: int = 4, delay_seconds: int = 2) -> bool:
    for attempt in range(1, max_attempts + 1):
        if _adb_is_instagram_reel_composer_visible(target, logger=logger):
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


def _adb_find_instagram_reel_option_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    screen = _adb_get_screen_size(target, logger=logger)
    if root is None:
        return None

    width, height = (1080, 2340) if screen is None else screen
    bottom_min_y = int(height * 0.55)
    bottom_max_y = int(height * 0.97)

    candidates: list[tuple[int, int, int, int, int]] = []
    for node in root.iter():
        attrs = node.attrib
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or attrs.get("contentDescription") or "").strip()
        resource = (attrs.get("resource-id") or "").lower()
        combined = " ".join((text, desc, resource)).strip().lower()
        if "reel" not in combined:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        if cy < bottom_min_y or cy > bottom_max_y:
            continue

        score = 0
        normalized_text = text.lower().strip()
        normalized_desc = desc.lower().strip()
        if normalized_text == "reel" or normalized_desc == "reel":
            score += 20
        if normalized_text == "reels" or normalized_desc == "reels":
            score += 15
        if "reel" in resource:
            score += 5
        if "reel" in normalized_text or "reel" in normalized_desc:
            score += 2

        candidates.append((score, x1, y1, x2, y2))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])
    _, x1, y1, x2, y2 = candidates[0]
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    return cx, cy


def _adb_find_instagram_start_new_video_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen

    best_candidate = None
    best_score = -1
    for node in root.iter():
        attrs = node.attrib
        combined = " ".join(
            str(attrs.get(key, "")) for key in ("text", "content-desc", "resource-id", "class")
        ).lower()
        has_strong_match = "start new video" in combined
        has_weak_match = "start new" in combined or "new video" in combined
        if not has_strong_match and not has_weak_match:
            continue

        enabled = attrs.get("enabled", "true").lower() != "false"
        if not enabled:
            continue

        clickable = attrs.get("clickable", "false").lower() == "true"
        node_class = (attrs.get("class") or "").lower()
        node_resource = attrs.get("resource-id", "").lower()
        looks_actionable = (
            clickable
            or any(token in node_class for token in ("button", "imagebutton", "android.widget", "androidx."))
            or any(token in node_resource for token in ("button", "action", "new"))
        )
        # Trust an exact "start new video" text match even when the clickable
        # attribute lives on a parent node (common in Instagram's dialogs).
        # Require actionable attributes only for the weaker partial matches,
        # which are more prone to false positives.
        if not has_strong_match and not looks_actionable:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
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

        score = cy + area // 100
        if "start new video" in combined:
            score += 500
        elif "new video" in combined:
            score += 300
        elif "start new" in combined:
            score += 100

        if cy > height * 0.6:
            score += 100

        if score > best_score:
            best_score = score
            best_candidate = (cx, cy)

    if best_candidate is not None:
        return best_candidate

    # No "Start new video" element was found -> there is no draft dialog.
    # Return None so callers skip draft-dialog handling entirely. Returning a
    # blind fallback point here (as this used to) made every caller believe a
    # draft dialog was present and tap an empty location (~0.5, 0.78), which
    # could dismiss/mis-tap the real composer and break media selection.
    _emit(logger, "info", "No 'Start new video' / draft dialog detected for %s", target)
    return None


def _adb_find_instagram_reel_create_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    screen = _adb_get_screen_size(target, logger=logger)
    if root is None:
        return None

    width, height = (1080, 2340) if screen is None else screen
    top_left_limit_x = int(width * 0.24)
    top_left_limit_y = int(height * 0.16)

    for node in root.iter():
        attrs = node.attrib
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or attrs.get("contentDescription") or "").strip()
        resource = (attrs.get("resource-id") or "").lower()
        combined = " ".join((text, desc, resource)).strip().lower()

        if not any(token in combined for token in ("+", "new", "create", "add", "post")):
            continue
        if any(token in combined for token in ("story", "camera", "reel option", "reel")) and "post" not in combined and "new" not in combined and "create" not in combined:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        if cx > top_left_limit_x or cy > top_left_limit_y:
            continue

        width_px = x2 - x1
        height_px = y2 - y1
        if min(width_px, height_px) < 20:
            continue

        return cx, cy

    return _adb_get_relative_point(target, 0.08, 0.08, logger=logger)


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
        _sleep_after_instagram_launch(target, logger=logger, delay_seconds=10)

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
        launch_tap_x, launch_tap_y = _adb_get_relative_point(target, 0.10, 0.29)
        return [
            f"adb -s {target} shell {tap(launch_tap_x, launch_tap_y)}",
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    def _build_navigate_to_reels_command(self, target: str) -> str:
        start_x = random.randint(800, 900)
        start_y = random.randint(900, 1200)
        end_x = random.randint(100, 200)
        end_y = random.randint(900, 1200)
        duration = human_timing.swipe_duration_ms(275, spread_frac=0.27)
        return f"adb -s {target} shell {swipe(start_x, start_y, end_x, end_y, duration)}"

    def _build_like_reel_command(self, target: str) -> str:
        x, y = _adb_get_relative_point(target, 0.91, 0.42)
        return f"adb -s {target} shell {tap(x, y)}"

    def _build_next_reel_swipe_command(self, target: str) -> str:
        start_x = random.randint(520, 580)
        start_y = random.randint(1000, 1200)
        end_x = random.randint(520, 580)
        end_y = random.randint(300, 500)
        duration = human_timing.swipe_duration_ms(450, spread_frac=0.33)
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
        _sleep_after_instagram_launch(target, logger=log)

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

        try:
            follows = self._follow_visible_accounts(target, adb_client, logger=log, should_stop=should_stop, max_follows=4)
            emit("info", "Followed %d account(s) for profile %s", follows, profile.id)
        except Exception as exc:  # pragma: no cover - diagnostic path
            emit("exception", "Unexpected error during follow processing for profile %s: %s", profile.id, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False}

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        adb_client.run_command(f"adb -s {target} shell {home()}")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        time.sleep(2)
        emit("info", "Instagram notifications flow completed")
        return {"profile_id": profile.id, "target": target}

    def build_launch_commands(self, target: str) -> list[str]:
        launch_tap_x, launch_tap_y = _adb_get_relative_point(target, 0.10, 0.29)
        return [
            f"adb -s {target} shell {tap(launch_tap_x, launch_tap_y)}",
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
        # Scroll down within the safe central band (relative, resolution-proof).
        screen = _adb_get_screen_size(target)
        width, height = (1080, 2340) if screen is None else screen
        cx = width // 2
        jitter = max(6, int(width * 0.03))
        start_x = cx + random.randint(-jitter, jitter)
        end_x = cx + random.randint(-jitter, jitter)
        start_y = int(height * random.uniform(0.64, 0.70))
        end_y = int(height * random.uniform(0.34, 0.40))
        duration = human_timing.swipe_duration_ms(475, spread_frac=0.37)
        return f"adb -s {target} shell {swipe(start_x, start_y, end_x, end_y, duration)}"

    def _build_scroll_up_command(self, target: str) -> str:
        # Scroll UP (toward the top) within the safe central band -- ends well
        # above the bottom gesture zone so it can't trigger the system home/back
        # gesture. Swipes never mis-tap a button.
        x1, y1 = _adb_get_relative_point(target, 0.5, 0.34, logger=None)
        x2, y2 = _adb_get_relative_point(target, 0.5, 0.68, logger=None)
        duration = human_timing.swipe_duration_ms(400, spread_frac=0.3)
        return f"adb -s {target} shell {swipe(x1, y1, x2, y2, duration)}"

    def _ocr_screen_text(self, target: str, logger=None) -> str:
        """Return the lowercased OCR text of the current screen. Works on the
        video-playing feed/reels (a screenshot captures a frame regardless of
        idle), so it can tell which screen we're on when a UI dump can't."""
        if pytesseract is None or Image is None or cv2 is None:
            return ""
        if not _resolve_tesseract_executable():
            return ""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            shot = Path(handle.name)
        try:
            if not self._capture_screenshot(target, str(shot), logger=logger):
                return ""
            image = self._load_screenshot_image(str(shot), logger=logger)
            if image is None:
                return ""
            try:
                text = pytesseract.image_to_string(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)))
            except Exception as exc:
                _emit(logger, "warning", "OCR of screen failed for %s: %s", target, exc)
                return ""
            return (text or "").lower()
        finally:
            try:
                shot.unlink(missing_ok=True)
            except OSError:
                pass

    def _capture_screenshot(self, target: str, output_path: str, logger=None) -> str | None:
        remote_path = "/sdcard/instagram_notifications.png"
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
            shell_result = _adb_run("-s", target, "shell", f"screencap -p {remote_path}",
                                    check=True, capture_output=True, text=True)
            emit("info", "ADB screencap command succeeded for %s: %s", target, shell_result.stdout.strip() or "<no stdout>")
        except subprocess.CalledProcessError as exc:
            emit("warning", "ADB screencap command failed for %s: %s", target, exc.stderr.strip() or str(exc))
            return None

        try:
            pull_result = _adb_run("-s", target, "pull", remote_path, output_path,
                                   check=True, capture_output=True, text=True)
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

    def _follow_visible_accounts(self, target: str, adb_client, logger=None, should_stop=None, max_follows: int = 5) -> int:
        """Follow accounts visible on the activity/notifications screen.

        Prefers exact "Follow" buttons located from the UI dump, which land on
        the real button and never on an avatar/username (that would open a
        profile). Only falls back to screenshot + OpenCV/OCR detection when the
        dump exposes no Follow buttons. Returns the number of Follow taps done.
        """
        def emit(level: str, message: str, *args) -> None:
            target_logger = logger if logger is not None else print
            method = getattr(target_logger, level, None)
            if callable(method):
                method(message, *args)
            else:
                if args:
                    message = message % args
                print(message)

        def check_abort() -> bool:
            return callable(should_stop) and should_stop()

        follow_centers = _adb_find_instagram_follow_button_centers(target, logger=logger, max_buttons=max_follows)
        if follow_centers:
            emit("info", "Found %d Follow button(s) via UI dump for %s", len(follow_centers), target)
            taps = 0
            for index, (fx, fy) in enumerate(follow_centers, start=1):
                if check_abort():
                    break
                emit("info", "Tapping Follow button %d via UI dump at %s,%s for %s", index, fx, fy, target)
                adb_client.run_command(f"adb -s {target} shell {tap(fx, fy)}")
                taps += 1
                try:
                    if hasattr(adb_client, "mark_progress_step"):
                        adb_client.mark_progress_step()
                except Exception:
                    pass
                time.sleep(0.8)
            return taps

        emit("info", "No Follow buttons in UI dump for %s; falling back to screenshot detection", target)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            screenshot_path = Path(handle.name)
        try:
            if not self._capture_screenshot(target, str(screenshot_path), logger=logger):
                emit("warning", "Screenshot capture failed for %s; following nobody", target)
                return 0
            image = self._load_screenshot_image(str(screenshot_path), logger=logger)
            if image is None:
                emit("warning", "Unable to read screenshot for %s; following nobody", target)
                return 0
            candidates = self._find_follow_button_candidates(image, logger=logger)
            emit("info", "Detected %d follow-button candidate(s) via screenshot for %s", len(candidates), target)
            taps = 0
            for index, candidate in enumerate(candidates[:max_follows], start=1):
                if check_abort():
                    break
                x, y, width, height = candidate
                tap_x, tap_y = get_follow_button_tap_position((x, y, width, height), image.shape)
                emit("info", "Tapping follow-button candidate %d at box=%s,%s size=%sx%s -> tap=%s,%s", index, x, y, width, height, tap_x, tap_y)
                adb_client.run_command(f"adb -s {target} shell {tap(tap_x, tap_y)}")
                taps += 1
                try:
                    if hasattr(adb_client, "mark_progress_step"):
                        adb_client.mark_progress_step()
                except Exception:
                    pass
                time.sleep(0.8)
            return taps
        finally:
            try:
                screenshot_path.unlink(missing_ok=True)
            except OSError:
                pass

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
        tesseract_exe = _resolve_tesseract_executable()
        if tesseract_exe:
            if log is None:
                print(f"Configured pytesseract to use {tesseract_exe}")
            else:
                log.info("Configured pytesseract to use %s", tesseract_exe)
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
        # Intentionally return no regions. This used to emit blind, fixed-
        # position guesses that were tapped without any verification -- the
        # main cause of taps landing on avatars/usernames and opening user
        # profiles instead of hitting a Follow button. When neither the UI dump
        # nor OCR/contour detection finds a real Follow button, it is safer to
        # follow nobody than to tap blindly.
        if logger is not None:
            logger.info("No verified follow buttons detected; skipping blind fallback taps")
        return []


class InstagramScrollFlow:
    name = "instagram_scroll"

    def __init__(self, scroll_seconds: float | None = None) -> None:
        # None keeps _build_sequence's own default (600s / 10 min) -- the
        # right length for warm-up. Callers that want the Active_Posting
        # protocol's short pre-post scroll (30-45s, confirmed 2026-08-29)
        # pass a smaller value instead of subclassing.
        self.scroll_seconds = scroll_seconds

    def get_progress_total_steps(self, target: str) -> int:
        launch_commands = self.build_launch_commands(target)
        sequence = (self._build_sequence(target, target_total_delay=self.scroll_seconds)
                   if self.scroll_seconds is not None else self._build_sequence(target))
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

        sequence = (self._build_sequence(target, target_total_delay=self.scroll_seconds)
                   if self.scroll_seconds is not None else self._build_sequence(target))
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
        # Resolve the real screen size once and build every swipe from fractions
        # of it, confined to the SAFE CENTRAL BAND (x ~0.5, y between ~0.30 and
        # ~0.70). This keeps swipes away from: the bottom nav bar and the Android
        # gesture zone at the very bottom (a low swipe there triggers the
        # system back/home gesture, which is what mis-navigated to Reels or
        # closed the app), the left/right edges (edge-swipe = back on gesture
        # nav), and the top status bar. It also scales to any resolution.
        screen = _adb_get_screen_size(target)
        width, height = (1080, 2340) if screen is None else screen
        cx = width // 2
        x_jitter = max(6, int(width * 0.03))

        sequence = []
        current_total_delay = 0.0
        down_swipes = 0
        up_swipes = 0

        while current_total_delay < target_total_delay:
            if down_swipes >= up_swipes * 2 and random.random() < 0.25:
                direction = "up"
            else:
                direction = "down" if random.random() < 0.95 else "up"

            if direction == "down":  # scroll down the feed (finger moves up)
                start_y = int(height * random.uniform(0.66, 0.70))
                end_y = int(height * random.uniform(0.30, 0.36))
                duration = human_timing.swipe_duration_ms(435, spread_frac=0.49)
                delay = round(random.uniform(2.5, 6.5), 2)
                down_swipes += 1
            else:  # scroll back up a little (finger moves down)
                start_y = int(height * random.uniform(0.34, 0.40))
                end_y = int(height * random.uniform(0.62, 0.68))
                duration = human_timing.swipe_duration_ms(410, spread_frac=0.46)
                delay = round(random.uniform(2.0, 3.8), 2)
                up_swipes += 1

            start_x = cx + random.randint(-x_jitter, x_jitter)
            end_x = cx + random.randint(-x_jitter, x_jitter)
            command = f"adb -s {target} shell {swipe(start_x, start_y, end_x, end_y, duration)}"
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

        _sleep_after_instagram_launch(target, logger=log)
        self._last_next_center = None
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

        if self._tap_next(target, adb_client, logger=log):
            emit("info", "Tapped Next after selecting story media for %s", target)
            time.sleep(3)
            if self._tap_next(target, adb_client, logger=log):
                emit("info", "Tapped second Next for %s", target)
                time.sleep(3)

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
        # Prefer tapping the story-row plus button in the stories section.
        # This avoids the top-left white-background create icon.
        story_row_plus_center = _adb_find_instagram_story_row_plus_center(target, logger=logger)
        if story_row_plus_center is not None:
            _adb_tap(target, story_row_plus_center[0], story_row_plus_center[1], adb_client, logger=logger, description="Tapping story row plus")
            time.sleep(1)
            return True

        # Fall back to a story action button that is still within the stories row area.
        story_center = _adb_find_instagram_story_action_center(target, logger=logger)
        if story_center is not None:
            _adb_tap(target, story_center[0], story_center[1], adb_client, logger=logger, description="Tapping story action")
            time.sleep(1)
            return True

        # Final fallback: try left/top and right/top generic taps only when the home feed is visible.
        if _adb_ensure_instagram_feed_visible(target, adb_client, logger=logger):
            fallback_points = [
                _adb_get_relative_point(target, 0.08, 0.10, logger=logger),
                _adb_get_relative_point(target, 0.92, 0.10, logger=logger),
            ]
            for x, y in fallback_points:
                _adb_tap(target, x, y, adb_client, logger=logger, description="Tapping fallback story composer top")
                time.sleep(2)
            return True

        if logger is not None:
            _emit(logger, "warning", "Cannot safely open story composer because Instagram home feed is not detected on %s", target)
        return False

    def _select_story_media(self, target: str, adb_client, logger=None, prefer_reel_text: bool = False) -> bool:
        if prefer_reel_text:
            reel_option = _adb_find_instagram_reel_option_center(target, logger=logger)
            if reel_option is not None:
                _emit(logger, "info", "Selecting REEL text option at %s for %s", reel_option, target)
                _adb_tap(target, reel_option[0], reel_option[1], adb_client, logger=logger, description="Tapping REEL option text")
                time.sleep(1.5)

        if not prefer_reel_text:
            gallery_center = _adb_find_instagram_gallery_center(target, logger=logger)
            if gallery_center is not None:
                _emit(logger, "info", "Selecting story gallery target at %s,%s for %s", gallery_center[0], gallery_center[1], target)
                _adb_tap(target, gallery_center[0], gallery_center[1], adb_client, logger=logger, description="Tapping story gallery selection")
                return True

        media_thumb = _adb_find_instagram_media_selection_center(target, logger=logger)
        if media_thumb is not None:
            description = "Tapping reel media target" if prefer_reel_text else "Tapping story media target"
            _emit(logger, "info", "Selecting media target at %s for %s", media_thumb, target)
            _adb_tap(target, media_thumb[0], media_thumb[1], adb_client, logger=logger, description=description)
            return True

        if prefer_reel_text:
            _emit(logger, "warning", "REEL text was tapped for %s, but no media target was detected afterwards", target)
            return False

        if _adb_is_instagram_story_composer_visible(target, logger=logger):
            fallback_media = _adb_get_relative_point(target, 0.18, 0.82, logger=logger)
            _emit(logger, "info", "Tapping fallback story media at %s,%s for %s", fallback_media[0], fallback_media[1], target)
            _adb_tap(target, fallback_media[0], fallback_media[1], adb_client, logger=logger, description="Tapping fallback story media")
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
            _adb_tap(target, x, y, adb_client, logger=log, description="Tapping Your story via UI dump")
            return True

        # 2) Try OCR on a bottom-right crop of a fresh screenshot
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
                            _adb_tap(target, x_abs, y_abs, adb_client, logger=log, description="Tapping Your story via OCR")
                            return True
            except Exception as exc:
                emit("info", "OCR attempt for 'Your story' failed for %s: %s", target, exc)

        # 3) Conservative fallback: do not tap blindly if no explicit target is detected.
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

        # The UI dump found no Next button. This is common on the reel PREVIEW,
        # which auto-plays the clip so the dump never reaches idle. Read the
        # button off a screenshot with OCR (a frame captures fine), restricted
        # to the right side where Next/Share live -- rather than blind-tapping a
        # fixed spot. The old (0.90, 0.92) guess was landing in the Android
        # navigation-bar band and backgrounding Instagram.
        ocr_hit = _adb_ocr_find_text_center(target, ("next", "share"), logger=logger, min_x_frac=0.45)
        if ocr_hit is not None:
            (ocr_x, ocr_y), ocr_word = ocr_hit
            self._last_next_center = (ocr_x, ocr_y)
            _emit(logger, "info", "Tapping OCR-located '%s' at %s,%s for %s", ocr_word, ocr_x, ocr_y, target)
            _adb_tap(target, ocr_x, ocr_y, adb_client, logger=logger, description=f"Tapping OCR-located '{ocr_word}'")
            return True

        if logger is not None:
            _emit(logger, "warning", "Next button not found via UI dump or OCR for %s; not tapping (avoiding a nav-bar mis-tap that closes Instagram)", target)
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

    def _tap_share_story(self, target: str, adb_client, logger=None) -> bool:
        share_center = _adb_find_instagram_share_center(target, logger=logger)
        if share_center is not None:
            _adb_tap(target, share_center[0], share_center[1], adb_client, logger=logger, description="Tapping Share")
            return True

        if logger is not None:
            _emit(logger, "warning", "Share button not detected for %s; skipping tap", target)
        return False


def _adb_find_instagram_reel_caption_center(target: str, logger=None) -> tuple[int, int] | None:
    root = _adb_capture_ui_dump(target, logger=logger)
    if root is None:
        return None

    candidates: list[tuple[int, int, int, int, int, int]] = []
    for node in root.iter():
        attrs = node.attrib
        text = (attrs.get("text") or "").lower()
        desc = (attrs.get("content-desc") or attrs.get("contentDescription") or "").lower()
        resource = (attrs.get("resource-id") or "").lower()
        node_class = (attrs.get("class") or "").lower()
        combined = " ".join((text, desc, resource, node_class)).strip()
        if "write a caption" not in combined and "add hashtags" not in combined and "caption" not in combined:
            continue

        bounds = attrs.get("bounds", "")
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not match:
            continue

        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue

        width = x2 - x1
        height = y2 - y1
        area = width * height
        if area < 1500:
            continue

        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        score = 0
        if "write a caption" in combined:
            score += 300
        if "add hashtags" in combined:
            score += 200
        if "caption" in combined:
            score += 100
        if "edittext" in node_class or "textfield" in node_class or "input" in node_class:
            score += 70
        if attrs.get("clickable", "false").lower() == "true":
            score += 40
        if "comment" in combined or "caption" in resource:
            score += 20

        screen = _adb_get_screen_size(target, logger=logger)
        screen_width, screen_height = (1080, 2340) if screen is None else screen
        if screen_height * 0.25 < cy < screen_height * 0.60:
            score += 50

        candidates.append((score, cx, cy, x1, y1, x2, y2))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda item: item[0])
    _, cx, cy, x1, y1, x2, y2 = candidates[0]
    click_x = x1 + max(min(int((x2 - x1) * 0.10), 120), 20)
    return click_x, cy


class InstagramReelUploadFlow(InstagramStoryUploadFlow):
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

        media_path = _adb_resolve_story_media_path(logger=log)
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

        # Pushing a file to the phone is not the same as posting it: the media is
        # only marked used once the reel actually goes out, so a failed run
        # leaves the clip in the queue for the next attempt.
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

        _sleep_after_instagram_launch(target, logger=log)
        # Reset the remembered Next location so a coordinate from a PREVIOUS
        # profile (this flow object is a single shared instance) can never leak
        # into this run and mis-tap.
        self._last_next_center = None
        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=log):
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
            media_outcome = self._select_reel_media_or_wait(
                target,
                adb_client,
                logger=log,
                profile=profile,
                should_stop=should_stop,
                status_callback=status_callback,
                manual_continue_event=manual_continue_event,
                manual_continue_callback=manual_continue_callback,
            )
        except Exception as exc:
            media_outcome = "failed"
            emit("warning", "Exception when selecting reel media for %s: %s", target, exc)
        if media_outcome == "aborted":
            return {"profile_id": profile.id, "target": target, "aborted": True}
        if media_outcome != "ok":
            emit("warning", "Unable to reliably select reel media for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        time.sleep(3)
        reel_posted = False
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

            # Second action button: a blue button in the bottom-right that is
            # sometimes "Next" and sometimes "Share". Detect it (verified blue)
            # instead of blind-tapping, so we never hit the phone's nav bar.
            second_label = self._tap_blue_next_or_share(target, adb_client, logger=log)
            if second_label is not None:
                emit("info", "Tapped the second blue '%s' button for %s", second_label, target)
                if second_label == "share":
                    reel_posted = True
                time.sleep(6 if second_label == "share" else 3)
            else:
                emit("warning", "Second blue Next/Share button was not detected for %s", target)
        else:
            emit("warning", "First Next was not detected for %s", target)

        # After the second button, check for a further blue Share button (some
        # versions go Next -> Share). If there is one, tap it; if not, tap
        # nothing -- we may already have posted via the second button.
        share_center = _adb_find_instagram_share_center(target, logger=log)
        if share_center is not None:
            _adb_tap(target, share_center[0], share_center[1], adb_client, logger=log, description="Tapping final blue Share")
            emit("info", "Tapped the final blue Share button for %s", target)
            time.sleep(8)
            reel_posted = True
        elif reel_posted:
            emit("info", "No additional Share button for %s; the reel was posted via the second blue button", target)
        else:
            emit("warning", "No blue Share button detected for %s; not tapping anything", target)

        # Return to the Instagram feed via the in-app house button (never the
        # phone's Android home, which would background the app) and verify we
        # actually landed on the main feed.
        self._go_home_and_verify_feed(target, adb_client, logger=log)

        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        if not reel_posted:
            keep_media_for_retry("post not confirmed")
            emit("warning", "Instagram reel upload did not complete successfully for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        commit_media_used()
        emit("info", "Instagram reel upload flow completed")
        return {"profile_id": profile.id, "target": target, "aborted": False, "success": True}

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
            screen_size = _adb_get_screen_size(target, logger=logger) or (1080, 2340)
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

                if attempt < 3:
                    _emit(
                        logger,
                        "warning",
                        "Start new video button still present after click; retrying for %s",
                        target,
                    )
                    start_new_video_center = current_start_new_video_center

        reel_option = _adb_find_instagram_reel_option_center(target, logger=logger)
        if reel_option is not None:
            _emit(logger, "info", "Clicking REEL option at %s for %s", reel_option, target)
            _adb_tap(target, reel_option[0], reel_option[1], adb_client, logger=logger, description="Tapping REEL option")
            time.sleep(2)
            return True

        if _adb_is_instagram_reel_composer_visible(target, logger=logger):
            _emit(logger, "info", "Reel composer already visible for %s after plus dialog open", target)
            return True

        _emit(logger, "warning", "REEL option was not visible for %s; failing reel composer opening", target)
        return False

    def _select_reel_media_or_wait(
        self,
        target: str,
        adb_client,
        logger=None,
        profile=None,
        should_stop=None,
        status_callback=None,
        manual_continue_event=None,
        manual_continue_callback=None,
    ) -> str:
        """Select the REEL option and a media clip. Returns 'ok', 'failed', or
        'aborted'.

        On small screens the "REEL" label at the bottom of the create tray is
        cut off and can't be detected. When that happens we do NOT guess -- we
        pause the flow and enable the app's per-profile Continue button so the
        operator can select REEL by hand, then resume and let the bot pick the
        media clip automatically.
        """
        reel_option = _adb_find_instagram_reel_option_center(target, logger=logger)
        if reel_option is not None:
            _emit(logger, "info", "Selecting REEL option at %s for %s", reel_option, target)
            _adb_tap(target, reel_option[0], reel_option[1], adb_client, logger=logger, description="Tapping REEL option")
            time.sleep(1.5)
        else:
            _emit(logger, "warning", "REEL option/text not detected for %s (screen may be too small); pausing for manual input", target)
            wait_outcome = self._wait_for_manual_continue(
                target,
                adb_client,
                profile=profile,
                logger=logger,
                should_stop=should_stop,
                status_callback=status_callback,
                manual_continue_event=manual_continue_event,
                manual_continue_callback=manual_continue_callback,
            )
            if wait_outcome == "aborted":
                return "aborted"
            # The operator has handled the REEL selection; fall through and let
            # the bot select the media clip.

        media_thumb = _adb_find_instagram_media_selection_center(target, logger=logger)
        if media_thumb is not None:
            _emit(logger, "info", "Selecting reel media target at %s for %s", media_thumb, target)
            _adb_tap(target, media_thumb[0], media_thumb[1], adb_client, logger=logger, description="Tapping reel media target")
            return "ok"

        _emit(logger, "warning", "No reel media thumbnail detected for %s after REEL selection", target)
        return "failed"

    def _wait_for_manual_continue(
        self,
        target: str,
        adb_client,
        profile=None,
        logger=None,
        should_stop=None,
        status_callback=None,
        manual_continue_event=None,
        manual_continue_callback=None,
        timeout_seconds: int = 300,
    ) -> str:
        """Pause the flow until the operator clicks the app's Continue button.
        Returns 'ok' (continue) or 'aborted'. Mirrors the scroll flow's
        manual-continue handling: surface the 'manual_continue' status, enable
        the Continue button, then wait on the event while watching for an abort
        or the device screen turning off."""
        profile_id = getattr(profile, "id", None)
        if callable(status_callback) and profile_id is not None:
            status_callback(profile_id, "manual_continue")
        if manual_continue_callback is not None and profile_id is not None:
            manual_continue_callback(profile_id)

        if manual_continue_event is None:
            _emit(logger, "warning", "No manual-continue channel for %s; proceeding without waiting", target)
            return "ok"

        _emit(logger, "info", "Waiting for manual continue for %s (up to %ss) -- click Continue after selecting REEL", target, timeout_seconds)
        wait_start = time.time()
        while not manual_continue_event.is_set():
            if callable(should_stop) and should_stop():
                _emit(logger, "info", "Abort requested while waiting for manual continue for %s", target)
                return "aborted"
            if not _adb_is_device_screen_on(target, adb_client, logger=logger):
                _emit(logger, "warning", "Device screen inactive while waiting for manual continue for %s; aborting", target)
                return "aborted"
            if time.time() - wait_start > timeout_seconds:
                _emit(logger, "warning", "Manual continue timed out for %s after %ss; proceeding anyway", target, timeout_seconds)
                break
            time.sleep(0.5)

        if manual_continue_event.is_set():
            manual_continue_event.clear()
            _emit(logger, "info", "Manual continue received for %s; resuming", target)
        return "ok"

    def _tap_blue_next_or_share(self, target: str, adb_client, logger=None) -> str | None:
        """Detect and tap the blue bottom-right action button, whose label is
        either 'Next' or 'Share'. Returns the tapped label ('next'/'share'), or
        None when no blue-verified button was found."""
        found = _adb_find_instagram_bottom_blue_action_center(target, labels=("next", "share"), logger=logger)
        if found is not None:
            (cx, cy), label = found
            _adb_tap(target, cx, cy, adb_client, logger=logger, description=f"Tapping blue '{label}' button")
            return label

        # Dump-based detection missed (e.g. the screen is still animating). Fall
        # back to OCR on a screenshot, restricted to the bottom-right where this
        # button lives, so we still advance without blind-tapping the nav bar.
        ocr_hit = _adb_ocr_find_text_center(target, ("share", "next"), logger=logger, min_x_frac=0.45, min_y_frac=0.55)
        if ocr_hit is None:
            return None
        (cx, cy), label = ocr_hit
        _emit(logger, "info", "Blue detector missed; tapping OCR-located '%s' at %s,%s for %s", label, cx, cy, target)
        _adb_tap(target, cx, cy, adb_client, logger=logger, description=f"Tapping OCR-located '{label}' button")
        return label

    def _go_home_and_verify_feed(self, target: str, adb_client, logger=None) -> bool:
        """Tap the Instagram home (house) button -- located from the UI dump
        first, then a hardcoded bottom-left nav position as a fallback (never the
        Android home key, which would background the app) -- and verify we
        landed on the main feed. Returns True if the feed was confirmed."""
        home_center = _adb_find_instagram_home_button_center(target, logger=logger)
        if home_center is not None:
            _emit(logger, "info", "Clicking the IG home (house) button via UI dump at %s for %s", home_center, target)
            _adb_tap(target, home_center[0], home_center[1], adb_client, logger=logger, description="Clicking IG home (house) button")
        else:
            hx, hy = _adb_get_relative_point(target, 0.09, 0.94, logger=logger)
            _emit(logger, "info", "IG home button not in UI dump for %s; clicking hardcoded house position (%s, %s)", target, hx, hy)
            _adb_tap(target, hx, hy, adb_client, logger=logger, description="Clicking IG home (house) button [hardcoded]")
        time.sleep(3)

        on_feed = _adb_is_instagram_home_feed_visible(target, logger=logger)
        if on_feed:
            _emit(logger, "info", "Confirmed on the main feed for %s after posting the reel", target)
        else:
            _emit(logger, "warning", "Could not confirm the main feed for %s after posting the reel", target)
        return on_feed


# --- Update-bio flow: dump-based helpers -------------------------------------

# Labels of buttons that are always safe to tap to close an interstitial pop-up.
# Never includes affirmative/action buttons ("OK", "Turn on", "Save", ...).
_SAFE_DISMISS_LABELS = (
    "not now",
    "skip",
    "skip for now",
    "cancel",
    "dismiss",
    "later",
    "maybe later",
    "no thanks",
    "no, thanks",
    "close",
)


def _node_center(attrs) -> tuple[int, int] | None:
    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", "") or "")
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def _root_has_labels(root, labels: tuple[str, ...]) -> bool:
    """True when every label appears (as a substring) in some node's text/desc."""
    needed = {label.lower() for label in labels if label}
    if not needed:
        return False
    found: set[str] = set()
    for node in root.iter():
        attrs = node.attrib
        text = str(attrs.get("text", "") or "").lower()
        desc = str(attrs.get("content-desc", "") or "").lower()
        for label in needed:
            if label in text or label in desc:
                found.add(label)
        if found >= needed:
            return True
    return found >= needed


def _find_center_by_exact_label(root, labels: tuple[str, ...]) -> tuple[int, int] | None:
    """Center of the first node whose text/content-desc equals one of `labels`
    (case-insensitive). Prefers a clickable node, else the first match."""
    wanted = {label.lower() for label in labels if label}
    fallback = None
    for node in root.iter():
        attrs = node.attrib
        text = str(attrs.get("text", "") or "").strip().lower()
        desc = str(attrs.get("content-desc", "") or "").strip().lower()
        if text not in wanted and desc not in wanted:
            continue
        center = _node_center(attrs)
        if center is None:
            continue
        if attrs.get("clickable", "false").lower() == "true":
            return center
        if fallback is None:
            fallback = center
    return fallback


def _adb_dismiss_blocking_dialogs(target: str, adb_client, logger=None, max_rounds: int = 3) -> bool:
    """Tap safe dismiss controls (Not now / Skip / Cancel / Close ...) to clear
    Instagram's random interstitial pop-ups. Returns True if anything was
    dismissed. Only ever taps the safe-dismiss set, never action buttons."""
    dismissed = False
    for _ in range(max_rounds):
        root = _adb_capture_ui_dump(target, logger=logger)
        if root is None:
            break
        center = None
        for node in root.iter():
            attrs = node.attrib
            text = str(attrs.get("text", "") or "").strip().lower()
            desc = str(attrs.get("content-desc", "") or "").strip().lower()
            label = text or desc
            is_safe = label in _SAFE_DISMISS_LABELS or desc in ("close", "dismiss")
            if not is_safe:
                continue
            if attrs.get("enabled", "true").lower() == "false":
                continue
            center = _node_center(attrs)
            if center is not None:
                _emit(logger, "info", "Dismissing pop-up '%s' for %s at %s", label, target, center)
                break
        if center is None:
            break
        _adb_tap(target, center[0], center[1], adb_client, logger=logger, description="Dismissing pop-up")
        dismissed = True
        time.sleep(1.5)
    return dismissed


def _adb_find_instagram_profile_tab_center(target: str, root, logger=None) -> tuple[int, int] | None:
    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen
    for node in root.iter():
        attrs = node.attrib
        desc = str(attrs.get("content-desc", "") or "").strip().lower()
        if desc == "profile" or desc.startswith("profile,") or desc.startswith("profile "):
            center = _node_center(attrs)
            if center is not None and center[1] > int(height * 0.85):
                return center
    return None


def _adb_find_bio_field_center(target: str, root, logger=None) -> tuple[int, int] | None:
    """The 'Bio' field/row on the Edit profile screen."""
    return _find_center_by_exact_label(root, ("bio",))


def _adb_read_bio_char_count(root) -> int | None:
    """Parse the 'N/150' character counter on the Bio editor. Returns N."""
    for node in root.iter():
        text = str(node.attrib.get("text", "") or "").strip()
        match = re.fullmatch(r"(\d+)\s*/\s*\d+", text)
        if match:
            return int(match.group(1))
    return None


def _adb_read_bio_edittext(root) -> str:
    """Current text of the bio EditText (ignoring the 'Bio' hint)."""
    for node in root.iter():
        attrs = node.attrib
        if "edittext" in str(attrs.get("class", "") or "").lower():
            text = str(attrs.get("text", "") or "").strip()
            if text and text.lower() != "bio":
                return text
    return ""


# Individual words that appear as labels/chrome on the Edit profile screen --
# used to tell a real bio VALUE apart from the form's own text when reading it
# via OCR (which returns word-by-word boxes).
_EDIT_PROFILE_LABEL_WORDS = {
    "bio", "name", "username", "pronouns", "gender", "add", "link", "links",
    "banners", "reorder", "grid", "ai", "creator", "edit", "picture", "or",
    "avatar", "prefer", "not", "to", "say", "learn", "more", "music", "profiles",
    "and", "share", "profile", "dashboard", "your", "new",
}

_EDIT_PROFILE_FIELD_LABELS = {
    "bio", "name", "username", "pronouns", "gender", "add link", "add banners",
    "reorder grid", "ai creator", "edit profile", "edit picture or avatar",
    "prefer not to say", "add music, profiles and more.", "share profile",
    "your dashboard", "add your bio", "learn more",
}


def _adb_read_labeled_field_value(root, label_texts) -> str:
    """Read the value sitting in the field box below one of `label_texts`.

    Shared by the Bio and Links readers: locate a label node whose text
    matches one of `label_texts` (case-insensitive), then return the nearest
    non-label text within roughly one field-height below it -- the value
    that sits inside the same field box. Works on the Edit profile screen,
    which -- being a static form -- dumps reliably (unlike the
    feed/profile/bio-editor screens).
    """
    if root is None:
        return ""
    wanted = {t.lower() for t in label_texts}
    label_bounds = None
    for node in root.iter():
        attrs = node.attrib
        if str(attrs.get("text", "") or "").strip().lower() in wanted:
            match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", "") or "")
            if match:
                label_bounds = tuple(map(int, match.groups()))
                break
    if label_bounds is None:
        return ""
    _bl_x1, bl_y1, _bl_x2, bl_y2 = label_bounds
    for node in root.iter():
        attrs = node.attrib
        text = str(attrs.get("text", "") or "").strip()
        if not text or text.lower() in _EDIT_PROFILE_FIELD_LABELS or text.lower() in wanted:
            continue
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", "") or "")
        if not match:
            continue
        _x1, y1, _x2, y2 = map(int, match.groups())
        cy = (y1 + y2) // 2
        # The value sits within the field box: from the label row down to
        # roughly one field-height below it.
        if bl_y1 - 10 <= cy <= bl_y2 + 170:
            return text
    return ""


def _adb_read_bio_field_value(root) -> str:
    """Read the Bio field's current value from the Edit profile screen dump.

    Returns the existing bio text, or '' if empty/unreadable.
    """
    return _adb_read_labeled_field_value(root, ("bio",))


def _root_is_bio_editor(root) -> bool:
    """True when `root` looks like the Bio edit screen. Uses several markers so
    it does not depend on any single label: the 'N/150' character counter, the
    'Change font' button, or a focused EditText."""
    if root is None:
        return False
    if _adb_read_bio_char_count(root) is not None:
        return True
    if _root_has_labels(root, ("change font",)):
        return True
    for node in root.iter():
        attrs = node.attrib
        if "edittext" in str(attrs.get("class", "") or "").lower() and attrs.get("focused", "false").lower() == "true":
            return True
    return False


def _adb_find_bio_save_center(target: str, root, logger=None) -> tuple[int, int] | None:
    """The top-right save control (checkmark) on the Bio editor."""
    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen
    save_labels = ("done", "save", "apply", "confirm", "submit", "check", "checkmark")
    for node in root.iter():
        attrs = node.attrib
        text = str(attrs.get("text", "") or "").strip().lower()
        desc = str(attrs.get("content-desc", "") or "").strip().lower()
        if text in save_labels or desc in save_labels or "done" in desc or "save" in desc:
            center = _node_center(attrs)
            if center is not None and center[0] > int(width * 0.65) and center[1] < int(height * 0.18):
                return center
    return None


def _adb_find_bio_cancel_center(target: str, root, logger=None) -> tuple[int, int] | None:
    """The top-left cancel/close control (X) on the Bio editor."""
    screen = _adb_get_screen_size(target, logger=logger)
    width, height = (1080, 2340) if screen is None else screen
    cancel_labels = ("close", "cancel", "back", "navigate up", "dismiss")
    for node in root.iter():
        attrs = node.attrib
        desc = str(attrs.get("content-desc", "") or "").strip().lower()
        if desc in cancel_labels:
            center = _node_center(attrs)
            if center is not None and center[0] < int(width * 0.25) and center[1] < int(height * 0.18):
                return center
    return None


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

        bio_text = (profile.bio or "").replace("\r", " ").replace("\n", " ").strip()
        if not bio_text:
            emit("warning", "No bio text provided for profile %s; aborting update bio flow", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        emit("info", "Starting Instagram update bio flow for profile %s", profile.id)
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            time.sleep(3 if ("monkey" in command or "am start" in command) else 1)

        emit("info", "Waiting for Instagram to load before profile navigation")
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        _sleep_after_instagram_launch(target, logger=log, delay_seconds=10)
        _adb_ensure_instagram_feed_visible(target, adb_client, logger=log, max_attempts=5, retry_delay_seconds=5)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # Step 1: open the profile tab and reach the Edit profile screen.
        open_outcome = self._open_edit_profile(target, adb_client, log)
        if open_outcome in ban_detection.ALL_KINDS:
            emit("warning", "Instagram flagged %s (%s); closing the profile", profile.id, open_outcome)
            return {"profile_id": profile.id, "target": target, "aborted": False, "account_flag": open_outcome}
        if open_outcome != "ok":
            emit("warning", "Could not reach the Edit profile screen for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # Step 2: open the Bio editor; skip if a bio already exists, else type + save.
        outcome = self._set_bio(target, adb_client, bio_text, log)
        if outcome in ban_detection.ALL_KINDS:
            emit("warning", "Instagram flagged %s (%s); closing the profile", profile.id, outcome)
            return {"profile_id": profile.id, "target": target, "aborted": False, "account_flag": outcome}
        if outcome == "already_has_bio":
            emit("info", "Profile %s already has a bio; it will be closed and skipped", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "already_has_bio": True}
        if outcome != "success":
            emit("warning", "Unable to update the bio for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        # Step 3: leave the Edit profile screen via the top-left back arrow.
        exit_root = _adb_capture_ui_dump(target, logger=log)
        back_center = _adb_find_bio_cancel_center(target, exit_root, logger=log) if exit_root is not None else None
        if back_center is None:
            back_center = _adb_get_relative_point(target, 0.07, 0.06, logger=log)
        _adb_tap(target, back_center[0], back_center[1], adb_client, logger=log, description="Leaving Edit profile")
        time.sleep(2)

        emit("info", "Instagram update bio flow completed for %s", target)
        return {"profile_id": profile.id, "target": target, "aborted": False, "success": True}

    def _ensure_screen(self, target, adb_client, required_labels, logger=None, attempts: int = 4, settle_seconds: float = 1.8):
        """Return a UI dump once the expected screen (all `required_labels`
        present) is visible, dismissing interstitial pop-ups between tries.
        Returns None if the screen never appeared."""
        for _ in range(attempts):
            root = _adb_capture_ui_dump(target, logger=logger)
            if root is not None and _root_has_labels(root, required_labels):
                return root
            _adb_dismiss_blocking_dialogs(target, adb_client, logger=logger)
            time.sleep(settle_seconds)
        return None

    # Candidate relative positions for the Profile tab (rightmost bottom-nav
    # avatar), tried in order. Its exact spot varies with the device's
    # navigation-bar setup (gesture vs 3-button). We verify arrival with OCR
    # (below), not a UI dump, because the profile screen animates/loads and
    # uiautomator dump cannot get an idle state on it.
    _PROFILE_TAB_CANDIDATES = (
        (0.90, 0.95),
        (0.90, 0.93),
        (0.93, 0.95),
    )

    def _ocr_find_text_center(self, target, phrases, logger=None):
        """Locate on-screen text via a screenshot + OCR. Unlike uiautomator
        dump, this works even while the screen animates/loads (feed, profile).
        `phrases` are lowercase; a single word matches a word box, a two-word
        phrase matches two adjacent words on the same OCR line."""
        words = self._ocr_words(target, logger=logger)
        if not words:
            return None

        for phrase in phrases:
            parts = phrase.split()
            if len(parts) >= 2:
                for idx in range(len(words) - 1):
                    w1, w2 = words[idx], words[idx + 1]
                    if w1[0] == parts[0] and w2[0] == parts[1] and w1[5] == w2[5]:
                        x1, y1 = w1[1], min(w1[2], w2[2])
                        x2, y2 = w2[1] + w2[3], max(w1[2] + w1[4], w2[2] + w2[4])
                        return ((x1 + x2) // 2, (y1 + y2) // 2)
            target_word = parts[0]
            for token, x, y, w, h, _ln in words:
                if token == target_word:
                    return (x + w // 2, y + h // 2)
        return None

    def _ocr_words(self, target, logger=None) -> list:
        """Screenshot + OCR -> [(text_lower, x, y, w, h, line_num), ...].

        Screenshots capture a frame regardless of whether the UI is idle, so
        this works on screens where `uiautomator dump` cannot (the feed, the
        profile, and -- as seen in practice -- the Edit profile form while its
        avatar/content is still animating)."""
        if pytesseract is None or Image is None or cv2 is None:
            _emit(logger, "warning", "OCR unavailable (opencv/pytesseract/Pillow missing) for %s", target)
            return []
        if not _resolve_tesseract_executable():
            _emit(logger, "warning", "Tesseract binary not found; cannot OCR-locate text for %s", target)
            return []

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            shot = Path(handle.name)
        try:
            if not self._capture_screenshot(target, str(shot), logger=logger):
                return []
            image = self._load_screenshot_image(str(shot), logger=logger)
            if image is None:
                return []
            try:
                data = pytesseract.image_to_data(
                    Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)),
                    output_type=pytesseract.Output.DICT,
                )
            except Exception as exc:
                _emit(logger, "warning", "OCR failed for %s: %s", target, exc)
                return []

            texts = data.get("text", []) or []
            words = []
            for i in range(len(texts)):
                token = (texts[i] or "").strip().lower()
                if not token:
                    continue
                words.append((
                    token,
                    int(data["left"][i]), int(data["top"][i]),
                    int(data["width"][i]), int(data["height"][i]),
                    int(data.get("line_num", [0] * len(texts))[i]),
                ))
            return words
        finally:
            try:
                shot.unlink(missing_ok=True)
            except OSError:
                pass

    def _ocr_read_bio_value(self, target, logger=None) -> str:
        """Best-effort read of an existing bio from the Edit profile screen via
        OCR (used when the UI dump can't reach an idle state). Returns the text
        sitting just under the 'Bio' label, or '' when the field is empty."""
        words = self._ocr_words(target, logger=logger)
        if not words:
            return ""
        bio_box = next((w for w in words if w[0] == "bio"), None)
        if bio_box is None:
            return ""
        _token, bx, by, _bw, bh, _ln = bio_box
        band_bottom = by + max(int(bh * 3.5), 60)
        value_words = []
        for token, x, y, _w, _h, _line in words:
            if token in _EDIT_PROFILE_LABEL_WORDS:
                continue
            if by < y <= band_bottom and abs(x - bx) < max(300, bh * 10):
                value_words.append((x, token))
        value_words.sort()
        return " ".join(token for _x, token in value_words).strip()

    def _open_edit_profile(self, target, adb_client, logger=None) -> str:
        """Reach the Edit profile screen. Returns 'ok', 'human_verification'
        (caller should stop and close the profile), or 'failed'."""
        edit_center = None
        for cx_frac, cy_frac in self._PROFILE_TAB_CANDIDATES:
            px, py = _adb_get_relative_point(target, cx_frac, cy_frac, logger=logger)
            _adb_tap(target, px, py, adb_client, logger=logger, description="Tapping Profile tab")
            time.sleep(5)  # let the profile screen load
            # Verify arrival with OCR (the profile screen animates, so a UI dump
            # would hang). The 'Edit profile' button appears in the profile
            # chrome even while posts are still loading.
            edit_center = self._ocr_find_text_center(target, ("edit profile", "edit"), logger=logger)
            if edit_center is not None:
                _emit(logger, "info", "Found 'Edit profile' via OCR at %s for %s (tab %.2f,%.2f)", edit_center, target, cx_frac, cy_frac)
                break
            # Something other than the profile is on screen -- a permission
            # prompt, human verification, etc. Deal with it before retrying.
            outcome = interruptions.check_and_handle(target, adb_client, logger=logger, flow=self)
            _flag = interruptions.account_flag_for(outcome)
            if _flag:
                return _flag

        if edit_center is None:
            _emit(logger, "warning", "Profile screen with 'Edit profile' not detected for %s", target)
            return "failed"

        _adb_tap(target, edit_center[0], edit_center[1], adb_client, logger=logger, description="Tapping Edit profile")
        time.sleep(3)

        # Confirm the Edit profile form actually loaded ('Username' is unique to
        # it). If not, it may be the blank/stuck page, a permission prompt, or
        # human verification -- handle it and re-check once.
        if self._edit_profile_loaded(target, adb_client, logger=logger):
            return "ok"

        outcome = interruptions.check_and_handle(
            target, adb_client, logger=logger, flow=self, expect="edit_profile"
        )
        _flag = interruptions.account_flag_for(outcome)
        if _flag:
            return _flag
        if outcome in (interruptions.OUTCOME_HANDLED, interruptions.OUTCOME_RESTART):
            if self._edit_profile_loaded(target, adb_client, logger=logger):
                return "ok"
            # Granting a permission can bounce us back to the feed -- start the
            # profile -> Edit profile navigation over once.
            if outcome == interruptions.OUTCOME_RESTART:
                _emit(logger, "info", "Re-navigating to Edit profile for %s after handling an interruption", target)
                return self._open_edit_profile(target, adb_client, logger=logger)

        _emit(logger, "warning", "Edit profile screen not detected for %s", target)
        return "failed"

    def _edit_profile_loaded(self, target, adb_client, logger=None) -> bool:
        """True when the Edit profile form (not a blank/spinner page) is showing."""
        root = self._ensure_screen(target, adb_client, ("username",), logger=logger, attempts=2)
        if root is not None:
            return True
        return self._ocr_find_text_center(target, ("username", "pronouns"), logger=logger) is not None

    def _set_bio(self, target, adb_client, bio_text, logger=None) -> str:
        # The Edit profile screen is a static form and dumps reliably. Read it
        # to (a) skip profiles that already have a bio and (b) locate the Bio
        # field. We do NOT gate typing on detecting the separate Bio editor --
        # its dump is flaky because of the keyboard animation. Instead we type
        # into the focused field and VERIFY the result on the Edit profile
        # screen afterwards.
        root = _adb_capture_ui_dump(target, logger=logger)
        bio_center = None
        if root is not None:
            existing = _adb_read_bio_field_value(root)
            if existing:
                _emit(logger, "info", "Bio already set ('%s') for %s; skipping without changes", existing, target)
                return "already_has_bio"
            bio_center = _adb_find_bio_field_center(target, root, logger=logger)

        # The Edit profile screen does not always reach an idle state (its
        # avatar/content keeps animating), so the UI dump can fail outright.
        # OCR reads the visible "Bio" label from a screenshot regardless.
        if bio_center is None:
            _emit(logger, "info", "UI dump unavailable; locating the 'Bio' label via OCR for %s", target)
            existing_ocr = self._ocr_read_bio_value(target, logger=logger)
            if existing_ocr:
                _emit(logger, "info", "Bio already set ('%s') for %s (read via OCR); skipping", existing_ocr, target)
                return "already_has_bio"
            bio_center = self._ocr_find_text_center(target, ("bio",), logger=logger)

        if bio_center is None:
            # Neither the dump nor OCR could see the Bio field -- check whether
            # an interruption (stuck page / permission prompt / human check) is
            # covering it, recover, and look once more.
            outcome = interruptions.check_and_handle(
                target, adb_client, logger=logger, flow=self, expect="edit_profile"
            )
            _flag = interruptions.account_flag_for(outcome)
            if _flag:
                return _flag
            if outcome in (interruptions.OUTCOME_HANDLED, interruptions.OUTCOME_RESTART):
                retry_root = _adb_capture_ui_dump(target, logger=logger)
                if retry_root is not None:
                    bio_center = _adb_find_bio_field_center(target, retry_root, logger=logger)
                if bio_center is None:
                    bio_center = self._ocr_find_text_center(target, ("bio",), logger=logger)

        if bio_center is None:
            _emit(logger, "warning", "Bio field not found on Edit profile screen for %s", target)
            return "failed"

        # Tap the Bio field to open the Bio editor (the field is now focused)
        # and type immediately. Do NOT run the pop-up dismisser here -- the Bio
        # editor's own top-left "X" has a "close" content-desc, and the dismisser
        # would tap it, closing the editor before we type (which looked like the
        # flow "going back"). The user-confirmed flow is: tap Bio -> type ->
        # tap the checkmark -> go back.
        _adb_tap(target, bio_center[0], bio_center[1], adb_client, logger=logger, description="Tapping Bio field")
        time.sleep(3)
        _emit(logger, "info", "Typing bio for %s: %s", target, bio_text)
        adb_client.run_command(f"adb -s {target} shell {write_text(bio_text)}")
        time.sleep(1.5)

        # Save via the Bio editor's top-right checkmark. We locate it from a
        # dump when possible, else tap the known top-right position. We must NOT
        # press Back here -- in the separate Bio editor that discards the edit.
        save_root = _adb_capture_ui_dump(target, logger=logger, idle_retries=0)
        save_center = _adb_find_bio_save_center(target, save_root, logger=logger) if save_root is not None else None
        if save_center is None:
            save_center = _adb_get_relative_point(target, 0.93, 0.06, logger=logger)
            _emit(logger, "info", "Save checkmark not located via dump for %s; tapping top-right position", target)
        _adb_tap(target, save_center[0], save_center[1], adb_client, logger=logger, description="Saving bio (checkmark)")
        time.sleep(2.5)

        # Verify the Bio field on the Edit profile screen now holds our text.
        # Prefer the dump; fall back to OCR when it can't reach an idle state.
        expected = bio_text.strip().lower()[:15]
        for _ in range(3):
            verify_root = _adb_capture_ui_dump(target, logger=logger)
            if verify_root is not None:
                current = _adb_read_bio_field_value(verify_root)
                if current and expected in current.lower():
                    _emit(logger, "info", "Verified bio set for %s: %s", target, current)
                    return "success"
            # OCR fallback: the field was empty before we typed, so ANY value
            # under the Bio label now means the bio was saved.
            ocr_value = self._ocr_read_bio_value(target, logger=logger)
            if ocr_value:
                _emit(logger, "info", "Bio appears set for %s (read via OCR: %s)", target, ocr_value)
                return "success"
            time.sleep(2)

        _emit(logger, "warning", "Could not verify the bio was set for %s", target)
        return "failed"


class InstagramUpdateBioU2Flow(InstagramNotificationsFlow):
    """uiautomator2 version of the update-bio flow, for side-by-side comparison
    with InstagramUpdateBioFlow (which uses `uiautomator dump` + OCR).

    The difference is entirely in the *detection/interaction* layer:
      * Launch is identical to the dump/OCR flow (same commands) so the two are
        an apples-to-apples comparison.
      * Elements are selected from the LIVE view tree by content-desc / text /
        class / resource-id via a persistent uiautomator2 session, with built-in
        implicit waits -- no re-dumping XML and no OCR for the standard controls.
      * Text is entered with `set_text` on the focused EditText, which is
        focus-independent and does not depend on the on-screen keyboard.
      * Only the top-right "save" checkmark falls back to a coordinate tap, since
        Instagram renders it without a stable, queryable id (Litho). This is the
        residual image/coordinate part I flagged: most taps become clean
        selectors, a couple stay positional.

    Requires `uiautomator2` (pip install uiautomator2). `target` is the device's
    ADB serial (ip:port), which uiautomator2 connects to directly.
    """

    name = "update_bio_u2"
    IG_PACKAGE = "com.instagram.android"
    SELECTOR_WAIT_SECONDS = 15.0

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
                emit("info", "Abort requested during Instagram update bio (u2) flow for profile %s", profile.id)
                return True
            return False

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

        bio_text = (profile.bio or "").replace("\r", " ").replace("\n", " ").strip()
        if not bio_text:
            emit("warning", "No bio text provided for profile %s; aborting update bio (u2) flow", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        emit("info", "Starting Instagram update bio (u2) flow for profile %s", profile.id)
        # Launch Instagram with the exact same commands as the dump/OCR flow so
        # the only thing that differs between the two is how we find elements.
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            time.sleep(3 if ("monkey" in command or "am start" in command) else 1)

        emit("info", "Connecting uiautomator2 to %s", target)
        try:
            d = u2.connect(target)
            d.implicitly_wait(self.SELECTOR_WAIT_SECONDS)
        except Exception as exc:
            emit("warning", "uiautomator2 could not connect to %s: %s", target, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        _sleep_after_instagram_launch(target, logger=log, delay_seconds=10)
        self._dismiss_popups_u2(d, logger=log)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        _flag = self._account_flag_u2(d)
        if _flag:
            emit("warning", "Instagram flagged %s (%s); closing the profile", profile.id, _flag)
            return {"profile_id": profile.id, "target": target, "aborted": False, "account_flag": _flag}

        # Step 1: profile tab -> Edit profile screen.
        if not self._open_edit_profile_u2(d, target, emit, log):
            _flag = self._account_flag_u2(d)
            if _flag:
                emit("warning", "Instagram flagged %s (%s); closing the profile", profile.id, _flag)
                return {"profile_id": profile.id, "target": target, "aborted": False, "account_flag": _flag}
            emit("warning", "Could not reach the Edit profile screen for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # Step 2: open the Bio editor; skip if a bio already exists, else type + save.
        # `_set_bio_with_mention_u2` handles a bio with an "@handle" mention
        # (real keystrokes + tapping Instagram's own suggestion, required for
        # it to become a real link) and delegates to the plain `_set_bio_u2`
        # for a mention-free bio, so this is always the right call.
        outcome = self._set_bio_with_mention_u2(d, target, bio_text, emit, log)
        if outcome == "already_has_bio":
            emit("info", "Profile %s already has a bio; it will be closed and skipped", profile.id)
            self._leave_edit_profile_u2(d, logger=log)
            return {"profile_id": profile.id, "target": target, "aborted": False, "already_has_bio": True}
        if outcome != "success":
            emit("warning", "Unable to update the bio for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        # Step 3: leave the Edit profile screen.
        self._leave_edit_profile_u2(d, logger=log)
        time.sleep(2)

        emit("info", "Instagram update bio (u2) flow completed for %s", target)
        return {"profile_id": profile.id, "target": target, "aborted": False, "success": True}

    # -- uiautomator2 helpers -------------------------------------------------

    def _first_present(self, d, selectors, timeout=None, logger=None, purpose="element"):
        """Return the first selector (from a list of kwargs dicts) whose element
        is present, waiting up to `timeout` seconds. Logs what it's waiting for
        and, when found, the matched element's attributes."""
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

    def _dismiss_popups_u2(self, d, logger=None, max_rounds: int = 3) -> bool:
        """Tap only safe dismiss controls (Not now / Skip / Cancel / Close ...)
        to clear Instagram's interstitial pop-ups. Never taps action buttons."""
        dismissed = False
        for _ in range(max_rounds):
            tapped = False
            for label in _SAFE_DISMISS_LABELS:
                pattern = f"(?i)^{re.escape(label)}$"
                sel = d(textMatches=pattern)
                if not sel.exists:
                    sel = d(descriptionMatches=pattern)
                if sel.exists:
                    _emit(logger, "info", "u2: dismissing pop-up '%s' -> %s", label, _u2_describe(sel))
                    try:
                        sel.click()
                        tapped = True
                        dismissed = True
                        time.sleep(1.2)
                        break
                    except Exception as exc:
                        _emit(logger, "warning", "u2: failed to dismiss pop-up '%s': %s", label, exc)
                        continue
            if not tapped:
                break
        return dismissed

    def _account_flag_u2(self, d) -> str | None:
        """Classify an IG block screen from the u2 hierarchy: returns a
        ban_detection kind ("banned" / "human_verification" / "action_block") or
        None. Reads the whole dumped hierarchy so ban/suspend/action-block
        screens are caught, not just the human-verification checkpoint."""
        return account_flag_u2(d)

    def _looks_like_human_verification_u2(self, d) -> bool:
        """Back-compat: True if any IG account-flag screen is showing."""
        return self._account_flag_u2(d) is not None

    def _open_edit_profile_u2(self, d, target, emit, logger=None) -> bool:
        # Tap the bottom-nav Profile tab. Its resource-id is the stable
        # handle -- try that first; fall back to known relative positions
        # only if the selector misses.
        try:
            width, height = d.window_size()
        except Exception:
            width, height = (1080, 2340)
        _emit(logger, "info", "u2: screen size for %s is %sx%s", target, width, height)

        tapped_profile = False
        profile_tab = d(resourceIdMatches=r"com\.instagram\.android:id/(profile_tab|main_profile_tab)")
        if profile_tab.exists:
            _emit(logger, "info", "u2: Profile tab found by resource-id -> %s", _u2_describe(profile_tab))
            profile_tab.click()
            tapped_profile = True
        else:
            # Fallback: content-desc, but exact -- `descriptionStartsWith`
            # also matches "Profile picture of <reel author>" on any avatar
            # inline in a Reels feed, not just the nav tab. Found live
            # 2026-09-04: that avatar sat at bounds.top=1942 on a
            # height=2424 screen (1942/2424=0.8012), just over the old
            # bounds.top > height*0.80 cutoff -- clicked a stranger's
            # profile instead of our own, so "Edit profile" was never going
            # to appear. An exact match plus a stricter, nav-bar-only cutoff
            # closes both holes at once.
            profile_tab = d(description="Profile")
            if not profile_tab.exists:
                profile_tab = d(descriptionMatches=r"(?i)^profile( tab)?$")
            if profile_tab.exists:
                _emit(logger, "info", "u2: Profile tab candidate -> %s", _u2_describe(profile_tab))
                try:
                    bounds = profile_tab.info.get("bounds", {})
                    if bounds.get("top", 0) > height * 0.90:
                        _emit(logger, "info", "u2: clicking bottom-nav Profile tab (in bottom 10%% of screen)")
                        profile_tab.click()
                        tapped_profile = True
                    else:
                        _emit(logger, "info", "u2: Profile candidate not in bottom nav (top=%s); using position fallback", bounds.get("top"))
                except Exception as exc:
                    _emit(logger, "warning", "u2: could not read Profile tab bounds: %s", exc)
            else:
                _emit(logger, "info", "u2: no 'Profile' resource-id or exact content-desc on screen; using position fallback")

        if not tapped_profile:
            for fx, fy in ((0.90, 0.95), (0.90, 0.93), (0.93, 0.95)):
                _emit(logger, "info", "u2: FALLBACK tapping Profile-tab position ratio (%.2f,%.2f) ~ px (%s,%s)", fx, fy, int(width * fx), int(height * fy))
                try:
                    d.click(fx, fy)  # uiautomator2 treats 0<val<1 as a ratio
                except Exception as exc:
                    _emit(logger, "warning", "u2: profile position tap failed: %s", exc)
                    continue
                time.sleep(3)
                if d(textMatches="(?i)edit profile").wait(timeout=5):
                    _emit(logger, "info", "u2: 'Edit profile' appeared after position tap (%.2f,%.2f)", fx, fy)
                    tapped_profile = True
                    break
        else:
            time.sleep(3)

        if not _u2_click(
            d,
            [{"textMatches": "(?i)edit profile"}, {"descriptionMatches": "(?i)edit profile"}],
            logger=logger,
            purpose="'Edit profile' button",
        ):
            _emit(logger, "warning", "'Edit profile' button not found for %s", target)
            return False
        time.sleep(2)
        self._dismiss_popups_u2(d, logger=logger)

        # Confirm the Edit profile form loaded ('Username' is unique to it, and
        # the 'Bio' row is what we act on next).
        marker = self._first_present(
            d,
            [{"textMatches": "(?i)username"}, {"textMatches": "(?i)^bio$"}],
            timeout=10,
            logger=logger,
            purpose="Edit profile form (Username/Bio marker)",
        )
        if marker is None:
            _emit(logger, "warning", "Edit profile form not detected for %s", target)
            return False
        return True

    def _set_bio_u2(self, d, target, bio_text, emit, logger=None) -> str:
        # Open the Bio row. Unlike the dump/OCR flow -- which reads the value off
        # the Edit profile screen because the editor's dump is flaky under the
        # keyboard animation -- uiautomator2 can read the editor's EditText
        # directly once it settles, so we read the current value there.
        bio_row = self._first_present(
            d,
            [{"text": "Bio"}, {"textMatches": "(?i)^(bio|add your bio)$"}],
            timeout=10,
            logger=logger,
            purpose="Bio row",
        )
        if bio_row is None:
            _emit(logger, "warning", "Bio field not found on Edit profile screen for %s", target)
            return "failed"
        _emit(logger, "info", "u2: clicking Bio row -> %s", _u2_describe(bio_row))
        bio_row.click()
        time.sleep(1.5)

        editor = d(className="android.widget.EditText")
        if not editor.wait(timeout=self.SELECTOR_WAIT_SECONDS):
            _emit(logger, "warning", "Bio editor (EditText) did not open for %s", target)
            return "failed"
        _emit(logger, "info", "u2: bio editor open -> %s", _u2_describe(editor))

        current = ""
        try:
            current = (editor.get_text() or "").strip()
        except Exception:
            current = ""
        _emit(logger, "info", "u2: current bio field value read as %r for %s", current, target)
        if current and current.lower() not in ("bio", "add your bio"):
            _emit(logger, "info", "Bio already set ('%s') for %s; skipping without changes", current, target)
            self._tap_bio_cancel_u2(d, logger=logger)
            time.sleep(1)
            return "already_has_bio"

        _emit(logger, "info", "u2: setting bio text for %s: %r", target, bio_text)
        try:
            editor.set_text(bio_text)  # focus-independent; works on background windows too
        except Exception as exc:
            _emit(logger, "warning", "Failed to set bio text for %s: %s", target, exc)
            return "failed"
        time.sleep(1)
        try:
            _emit(logger, "info", "u2: bio field now reads %r after set_text", (editor.get_text() or "").strip())
        except Exception:
            pass

        if not self._tap_bio_save_u2(d, logger=logger):
            _emit(logger, "warning", "Could not find the save control for %s", target)
            return "failed"
        time.sleep(2)

        # Verify: back on the Edit profile screen the Bio row should now show our
        # text (Instagram may truncate, so match a leading snippet).
        snippet = bio_text.strip()[:15]
        _emit(logger, "info", "u2: verifying Edit profile now shows bio snippet %r for %s", snippet, target)
        if snippet and d(textContains=snippet).wait(timeout=6):
            _emit(logger, "info", "Verified bio set for %s", target)
            return "success"
        _emit(logger, "warning", "Could not verify the bio was set for %s", target)
        return "failed"

    def _set_bio_with_mention_u2(self, d, target, bio_text, emit, logger=None) -> str:
        """Like `_set_bio_u2`, but for a bio containing a real `@handle`
        mention that must come from Instagram's own autocomplete suggestion
        list to become a genuine, tappable mention -- typing (or `set_text`ing)
        the string alone leaves "@handle" as plain text forever, never a real
        link. Confirmed live 2026-09-04: `set_text` never brings up the
        suggestion list at all (it sets the field's value in one shot, not as
        a sequence of real keystrokes); `d.send_keys(ch, ...)` one character at
        a time does, and lands `entity_suggestions_list`
        (`row_search_user_username` rows) once the typed handle matches a real
        account. Tapping the row whose username exactly matches replaces
        whatever was typed with the confirmed handle and turns it blue for
        real -- unmatched typed text (a typo, a private/renamed account) never
        produces a row, which is the caller's signal to abort rather than
        save a bio with a dead "@handle" string in it.

        `bio_text` must contain exactly one `@handle` (letters/digits/dot/
        underscore); text before and after it is typed the same real-keystroke
        way, with the suggestion tap in between. A bio with no `@` in it is
        cheaper to just set in one shot, so this delegates to `_set_bio_u2`.
        """
        match = re.search(r"@([A-Za-z0-9_.]+)", bio_text)
        if not match:
            return self._set_bio_u2(d, target, bio_text, emit, logger=logger)
        handle = match.group(1)
        before_and_handle = bio_text[:match.end()]
        after = bio_text[match.end():]

        bio_row = self._first_present(
            d,
            [{"text": "Bio"}, {"textMatches": "(?i)^(bio|add your bio)$"}],
            timeout=10,
            logger=logger,
            purpose="Bio row",
        )
        if bio_row is None:
            _emit(logger, "warning", "Bio field not found on Edit profile screen for %s", target)
            return "failed"
        _emit(logger, "info", "u2: clicking Bio row -> %s", _u2_describe(bio_row))
        bio_row.click()
        time.sleep(1.5)

        editor = d(className="android.widget.EditText")
        if not editor.wait(timeout=self.SELECTOR_WAIT_SECONDS):
            _emit(logger, "warning", "Bio editor (EditText) did not open for %s", target)
            return "failed"

        current = ""
        try:
            current = (editor.get_text() or "").strip()
        except Exception:
            current = ""
        if current and current.lower() not in ("bio", "add your bio"):
            _emit(logger, "info", "Bio already set ('%s') for %s; skipping without changes", current, target)
            self._tap_bio_cancel_u2(d, logger=logger)
            time.sleep(1)
            return "already_has_bio"

        try:
            editor.click()
        except Exception:
            pass
        time.sleep(0.3)
        _emit(logger, "info", "u2: typing bio up to mention '@%s' for %s via real keystrokes "
                             "(set_text would never trigger the suggestion list)", handle, target)
        for ch in before_and_handle:
            d.send_keys(ch, clear=False)
            time.sleep(0.05)

        suggestion = self._first_present(
            d,
            [{"resourceId": "com.instagram.android:id/row_search_user_username", "text": handle}],
            timeout=6,
            logger=logger,
            purpose=f"mention suggestion for @{handle}",
        )
        if suggestion is None:
            _emit(logger, "warning", "u2: no matching suggestion for @%s on %s -- "
                        "would save as plain text, not a real mention; aborting", handle, target)
            self._tap_bio_cancel_u2(d, logger=logger)
            time.sleep(1)
            return "failed"
        _emit(logger, "info", "u2: tapping mention suggestion @%s -> %s", handle, _u2_describe(suggestion))
        suggestion.click()
        time.sleep(1)

        if after:
            for ch in after:
                d.send_keys(ch, clear=False)
                time.sleep(0.05)
            time.sleep(0.5)

        if not self._tap_bio_save_u2(d, logger=logger):
            _emit(logger, "warning", "Could not find the save control for %s", target)
            return "failed"
        time.sleep(2)

        _emit(logger, "info", "u2: verifying Edit profile now shows the mention @%s for %s", handle, target)
        if d(textContains=handle).wait(timeout=6):
            _emit(logger, "info", "Verified bio with mention @%s set for %s", handle, target)
            return "success"
        _emit(logger, "warning", "Could not verify the bio+mention was set for %s", target)
        return "failed"

    def _tap_bio_save_u2(self, d, logger=None) -> bool:
        # Prefer a labelled control; fall back to the top-right checkmark
        # position (the one control Instagram doesn't expose as a stable id).
        return _u2_click(
            d,
            [
                {"descriptionMatches": "(?i)^(done|save|apply|confirm|submit)$"},
                {"textMatches": "(?i)^(done|save)$"},
                {"resourceId": "com.instagram.android:id/action_bar_button_action"},
            ],
            logger=logger,
            purpose="Bio save (checkmark)",
            fallback_ratio=(0.93, 0.06),
        )

    def _tap_bio_cancel_u2(self, d, logger=None) -> None:
        if _u2_click(
            d,
            [{"descriptionMatches": "(?i)^(close|cancel|back|navigate up|dismiss)$"}],
            logger=logger,
            purpose="Bio editor cancel (X)",
        ):
            return
        _emit(logger, "info", "u2: no cancel control found; pressing hardware Back")
        try:
            d.press("back")
        except Exception:
            pass

    def _leave_edit_profile_u2(self, d, logger=None) -> None:
        if _u2_click(
            d,
            [{"descriptionMatches": "(?i)^(back|navigate up|close)$"}],
            logger=logger,
            purpose="Leave Edit profile (back arrow)",
        ):
            return
        _emit(logger, "info", "u2: no back control found; pressing hardware Back")
        try:
            d.press("back")
        except Exception:
            pass


class InstagramUpdateProfilePictureU2Flow(InstagramUpdateBioU2Flow):
    """Update the account's profile picture (uiautomator2).

    Like the update-bio u2 flow up to the Edit profile screen, then: tap
    'Edit picture or avatar', select the most-recent gallery photo (the one this
    flow pushed to the device), tap 'Done', and wait. The photo is chosen in the
    UI (same slot the caption input uses) and pushed + media-scanned exactly the
    way the story/reel flows push media, so it shows up first in the picker.
    """

    name = "update_profile_picture"
    PICTURE_STAY_SECONDS = 10

    def _build_remote_media_path(self, local_media_path: str) -> str:
        return f"/sdcard/Download/{Path(local_media_path).name.replace(' ', '_')}"

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
                emit("info", "Abort requested during Instagram update profile picture flow for profile %s", profile.id)
                return True
            return False

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

        picture_path = (getattr(profile, "picture", None) or "").strip()
        if not picture_path:
            emit("warning", "No profile picture selected for profile %s; aborting", profile.id)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        media_source = Path(picture_path).expanduser()
        if not media_source.exists() or not media_source.is_file():
            emit("warning", "Selected profile picture does not exist for %s: %s", profile.id, media_source)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        emit("info", "Starting Instagram update profile picture flow for profile %s (%s)", profile.id, media_source.name)

        # --- Push the chosen photo (push + verify + media scan) --------------
        remote_media_path = self._build_remote_media_path(str(media_source))
        emit("info", "Pushing profile picture for %s: %s -> %s", profile.id, media_source, remote_media_path)
        if not _adb_push_media_to_device(target, str(media_source), remote_media_path, logger=log):
            emit("warning", "adb push failed for profile %s on target %s", profile.id, target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        for attempt in range(1, 4):
            if _adb_verify_remote_media_exists(target, remote_media_path, logger=log) and \
                    _adb_verify_remote_media_matches_local(target, str(media_source), remote_media_path, logger=log):
                emit("info", "Verified pushed profile picture on device for %s", target)
                break
            if attempt >= 3:
                emit("warning", "Failed to verify pushed profile picture on device for %s", target)
                return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}
            if not _adb_push_media_to_device(target, str(media_source), remote_media_path, logger=log):
                emit("warning", "adb push retry %s failed for %s", attempt + 1, target)
        _adb_wait_for_media_store_index(target, remote_media_path, logger=log)

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # --- Launch Instagram + connect uiautomator2 -------------------------
        for command in self.build_launch_commands(target):
            if check_abort():
                return {"profile_id": profile.id, "target": target, "aborted": True}
            adb_client.run_command(command)
            time.sleep(3 if ("monkey" in command or "am start" in command) else 1)

        emit("info", "Connecting uiautomator2 to %s", target)
        try:
            d = u2.connect(target)
            d.implicitly_wait(self.SELECTOR_WAIT_SECONDS)
        except Exception as exc:
            emit("warning", "uiautomator2 could not connect to %s: %s", target, exc)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        _sleep_after_instagram_launch(target, logger=log, delay_seconds=10)
        self._dismiss_popups_u2(d, logger=log)

        _flag = self._account_flag_u2(d)
        if _flag:
            emit("warning", "Instagram flagged %s (%s); closing the profile", profile.id, _flag)
            return {"profile_id": profile.id, "target": target, "aborted": False, "account_flag": _flag}

        # --- Reach Edit profile (reused from the bio flow) -------------------
        if not self._open_edit_profile_u2(d, target, emit, log):
            _flag = self._account_flag_u2(d)
            if _flag:
                emit("warning", "Instagram flagged %s (%s); closing the profile", profile.id, _flag)
                return {"profile_id": profile.id, "target": target, "aborted": False, "account_flag": _flag}
            emit("warning", "Could not reach the Edit profile screen for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}

        # --- Open picker -> select photo -> Done -----------------------------
        if not self._open_edit_picture_u2(d, target, emit, log):
            emit("warning", "Could not open the profile-picture picker for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if not self._select_first_gallery_photo_u2(d, target, emit, log):
            emit("warning", "Could not select a profile picture from the gallery for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        if not self._tap_done_u2(d, target, emit, log):
            emit("warning", "Could not tap 'Done' to confirm the profile picture for %s", target)
            return {"profile_id": profile.id, "target": target, "aborted": False, "success": False}

        emit("info", "Tapped Done; waiting %ss for the profile picture to apply for %s", self.PICTURE_STAY_SECONDS, target)
        for _ in range(self.PICTURE_STAY_SECONDS):
            if check_abort():
                break
            time.sleep(1)

        emit("info", "Instagram update profile picture flow completed for %s", target)
        return {"profile_id": profile.id, "target": target, "aborted": False, "success": True}

    # -- uiautomator2 helpers -------------------------------------------------

    def _open_edit_picture_u2(self, d, target, emit, logger=None) -> bool:
        # Tap the blue "Edit picture or avatar" link on the Edit profile screen.
        if not _u2_click(
            d,
            [
                {"textContains": "Edit picture or avatar"},
                {"textContains": "Edit picture"},
                {"descriptionContains": "Edit picture"},
                {"textContains": "Change profile photo"},
            ],
            logger=logger,
            purpose="'Edit picture or avatar' link",
        ):
            return False
        time.sleep(2)

        # IG may show an options sheet first (New profile photo / Choose from
        # library / Upload from device). Tap the library/new option if present;
        # otherwise the gallery opens directly. Do NOT run the pop-up dismisser
        # here -- the picker's own top-left "X" has a close content-desc and the
        # dismisser would tap it, closing the picker before we can select.
        _u2_click(
            d,
            [
                {"textContains": "New profile photo"},
                {"textContains": "New profile picture"},
                {"textContains": "Choose from library"},
                {"textContains": "Upload from device"},
                {"textContains": "Choose from device"},
            ],
            logger=logger,
            purpose="profile-photo source option (optional)",
        )

        # Confirm the gallery/photo picker is open.
        ready = self._first_present(
            d,
            [
                {"textMatches": "(?i)^done$"},
                {"descriptionMatches": "(?i)^done$"},
                {"textContains": "Recents"},
                {"descriptionStartsWith": "Photo"},
            ],
            timeout=self.SELECTOR_WAIT_SECONDS,
            logger=logger,
            purpose="profile-picture gallery",
        )
        if ready is None:
            _emit(logger, "warning", "Profile-picture gallery did not open for %s", target)
            return False
        return True

    def _select_first_gallery_photo_u2(self, d, target, emit, logger=None) -> bool:
        # Select the most-recent photo (the one this flow pushed) = the first
        # gallery cell. Cells carry a "Photo, ..." content-desc; fall back to the
        # first thumbnail position (bottom-left of the picker's thumbnail row).
        selected = _u2_click(
            d,
            [
                {"descriptionStartsWith": "Photo"},
                {"descriptionContains": "Photo"},
            ],
            logger=logger,
            purpose="most-recent gallery photo",
            fallback_ratio=(0.12, 0.58),
        )
        time.sleep(1.5)
        return selected

    def _tap_done_u2(self, d, target, emit, logger=None) -> bool:
        # The blue "Done" confirm control, top-right of the picker.
        return _u2_click(
            d,
            [
                {"text": "Done"},
                {"textMatches": "(?i)^done$"},
                {"description": "Done"},
            ],
            logger=logger,
            purpose="Done (confirm profile picture)",
            fallback_ratio=(0.92, 0.05),
        )


class InstagramWarmUpDay1Flow(InstagramNotificationsFlow, InstagramScrollFlow):
    name = "warm_up_process"

    def build_launch_commands(self, target: str) -> list[str]:
        # Force-stop first so Instagram cold-starts on the HOME FEED. A warm
        # resume can land on whatever tab it was last on (often Reels), and we
        # cannot reliably tap the Home button to correct it -- the feed/reels
        # play video, so the nav icons can't be located by dump, and a fixed
        # bottom-left tap risks hitting the Android back button. A cold start
        # avoids the problem entirely.
        return [
            f"adb -s {target} shell am force-stop com.instagram.android",
            f"adb -s {target} shell monkey -p com.instagram.android -c android.intent.category.LAUNCHER 1",
            f"adb -s {target} shell am start -n com.instagram.android/.activity.MainTabActivity",
        ]

    # OCR tokens that only appear on the Reels player, never on the home feed.
    # A screenshot captures a frame even while the video plays, so OCR can tell
    # the two apart when a UI dump can't (the dump hangs on the non-idle video).
    _REELS_OCR_MARKERS = (
        "watch more reels",
        "watch again",
        "original audio",
        "use audio",
        "remix",
    )

    def _is_on_reels_tab(self, target, logger=None) -> bool:
        """True only when OCR of the current screen clearly shows the Reels tab.

        Returns False when OCR is unavailable or the text is inconclusive, so
        callers treat "unknown" as "not Reels" and never correct a screen we
        cannot actually identify as Reels.
        """
        text = self._ocr_screen_text(target, logger=logger)
        if not text:
            _emit(logger, "info", "Fallback (reels check): no OCR text for %s; treating screen as not-Reels", target)
            return False
        snippet = " ".join(text.split())[:200]
        matched = [marker for marker in self._REELS_OCR_MARKERS if marker in text]
        if matched:
            _emit(logger, "info", "Fallback (reels check): %s IS on the Reels tab; matched %s (OCR text: %r)", target, matched, snippet)
            return True
        _emit(logger, "info", "Fallback (reels check): %s not on Reels; no Reels markers in OCR text: %r", target, snippet)
        return False

    def _ensure_on_home_feed(self, target, adb_client, logger=None) -> None:
        """Make sure Instagram is on the HOME FEED (not the Reels tab) before the
        warm-up starts scrolling.

        Instagram can open or resume on Reels, which shares the same activity as
        the feed, so the foreground check can't tell them apart -- scrolling
        there would swipe through Reels instead of the feed. We detect Reels via
        OCR (works on the video-playing screen where a UI dump can't) and, if
        we're on it, tap the Home (house) button in the bottom-left nav bar to
        return to the feed -- exactly what a user would do, and cheaper than a
        cold relaunch.

        To know WHERE to tap we first locate the Home button from the live UI
        dump and tap its real centre. Only if the dump can't find it (the Reels
        video keeps the UI non-idle, so the dump may hang/return nothing) do we
        fall back to hardcoded relative nav positions: the button's height varies
        with the device's system navigation bar, so we tap safest-first (a higher
        y clears a tall nav bar; a miss there is harmless) and confirm Instagram
        is still foreground after each tap, retrying a higher position if a tap
        landed on the Android back button and closed the app. If every tap fails
        to leave Reels, we cold-relaunch as a last resort.
        """
        if not self._is_on_reels_tab(target, logger=logger):
            _emit(logger, "info", "Fallback (reels->home): %s is not on Reels; home feed check passed, no correction needed", target)
            return

        _emit(logger, "info", "Fallback (reels->home) ENGAGED for %s: on the Reels tab, correcting back to the home feed", target)

        # Primary: locate the Home (house) nav button from the live UI dump so we
        # tap its real centre instead of a guessed position. The dump can fail on
        # the video-playing Reels screen (it hangs on a non-idle UI); only then
        # do we fall back to the hardcoded relative nav positions below.
        home_center = _adb_find_instagram_home_button_center(target, logger=logger)
        if home_center is not None:
            _emit(logger, "info", "Fallback (reels->home): located Home button via UI dump at %s for %s; clicking it", home_center, target)
            _adb_tap(target, home_center[0], home_center[1], adb_client, logger=logger, description="Clicking Home (house) button [dump-located]")
            time.sleep(2.5)
            if _adb_is_instagram_foreground(target, logger=logger) and not self._is_on_reels_tab(target, logger=logger):
                _emit(logger, "info", "Fallback (reels->home): SUCCESS for %s after the dump-located Home click", target)
                return
            _emit(logger, "info", "Fallback (reels->home): dump-located Home click did not reach the feed for %s; switching to hardcoded nav-position clicks", target)
        else:
            _emit(logger, "info", "Fallback (reels->home): Home button not found in UI dump for %s (Reels video keeps UI non-idle); using hardcoded nav positions", target)

        # Fallback: the UI dump couldn't locate the button (Reels video keeps the
        # UI non-idle). Tap the bottom-left nav position by hand. Its height
        # varies with the device's system navigation bar, so we tap safest-first
        home_y_candidates = (0.90, 0.92, 0.94, 0.88)
        for attempt, y_frac in enumerate(home_y_candidates, start=1):
            if not _adb_is_instagram_foreground(target, logger=logger):
                _emit(logger, "info", "Fallback (reels->home): Instagram not foreground for %s; relaunching before the Home click", target)
                self._relaunch_instagram(target, adb_client, logger=logger)

            hx, hy = _adb_get_relative_point(target, 0.09, y_frac, logger=logger)
            _emit(logger, "info", "Fallback (reels->home): clicking hardcoded Home position (%s, %s) [x=0.09, y=%.2f] for %s (attempt %s/%s)", hx, hy, y_frac, target, attempt, len(home_y_candidates))
            _adb_tap(target, hx, hy, adb_client, logger=logger, description="Clicking Home (house) button [hardcoded]")
            time.sleep(2.5)

            # A tap that closed the app hit the Android back button -- retry higher.
            if not _adb_is_instagram_foreground(target, logger=logger):
                _emit(logger, "info", "Fallback (reels->home): click at y=%.2f closed Instagram for %s (hit the Android back button); will retry higher", y_frac, target)
                continue

            if not self._is_on_reels_tab(target, logger=logger):
                _emit(logger, "info", "Fallback (reels->home): SUCCESS for %s after the hardcoded Home click at y=%.2f", target, y_frac)
                return

            _emit(logger, "info", "Fallback (reels->home): still on Reels for %s after click at y=%.2f (attempt %s); trying next position", target, y_frac, attempt)

        _emit(logger, "warning", "Fallback (reels->home): hardcoded Home clicks did not leave Reels for %s; cold-relaunching as last resort", target)
        self._relaunch_instagram(target, adb_client, logger=logger)

    def _relaunch_instagram(self, target, adb_client, logger=None) -> None:
        for command in self.build_launch_commands(target):
            adb_client.run_command(command)
            time.sleep(3 if ("monkey" in command or "am start" in command) else 1)
        _sleep_after_instagram_launch(target, logger=logger, delay_seconds=8)

    def _open_activity_feed(self, target, adb_client, logger=None) -> bool:
        """Open Instagram's Activity (notifications) feed reliably.

        Taps the Home (house) button to scroll the feed to the very top so the
        top bar with the Notifications heart appears, then VERIFIES the heart via
        a UI dump before tapping it. The Home button's exact height varies with
        the device's system navigation bar, so we try a few positions; after each
        tap we confirm Instagram is still foreground (a tap that lands on the
        Android back button closes the app -- if so we relaunch and try a
        position higher up). We only tap the heart once it's actually detected,
        so we never blindly hit a post's like button or the '...' menu.
        """
        # Ordered safest-first: higher y clears a tall system nav bar (a miss
        # there is harmless -- app stays open); lower y risks the back button.
        home_y_candidates = (0.90, 0.92, 0.94, 0.88)
        for attempt, y_frac in enumerate(home_y_candidates, start=1):
            if not _adb_is_instagram_foreground(target, logger=logger):
                _emit(logger, "info", "Instagram not foreground for %s; relaunching before Home tap", target)
                self._relaunch_instagram(target, adb_client, logger=logger)

            hx, hy = _adb_get_relative_point(target, 0.09, y_frac, logger=logger)
            _emit(logger, "info", "Tapping Home (house) button at y=%.2f for %s (attempt %s)", y_frac, target, attempt)
            _adb_tap(target, hx, hy, adb_client, logger=logger, description="Tapping Home (house) button")
            time.sleep(2.5)

            # If that tap closed the app (hit the Android back button), relaunch
            # and try a higher position next time.
            if not _adb_is_instagram_foreground(target, logger=logger):
                _emit(logger, "info", "Home tap at y=%.2f closed Instagram for %s; will retry higher", y_frac, target)
                continue

            heart = _adb_find_instagram_activity_heart_center(target, logger=logger)
            if heart is not None:
                _emit(logger, "info", "Notifications heart visible at %s for %s; opening the Activity feed", heart, target)
                _adb_tap(target, heart[0], heart[1], adb_client, logger=logger, description="Tapping Notifications heart")
                time.sleep(3)
                return True

            _emit(logger, "info", "Notifications heart not detected yet for %s (attempt %s); tapping Home again", target, attempt)

        _emit(logger, "warning", "Could not reveal and tap the Notifications heart for %s", target)
        return False

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
        # Tap the Home (house) button at the stable far-left nav position. We do
        # NOT use the dump-based finder here: on the video-playing feed/reels the
        # dump is unreliable and could return the Reels icon's location instead.
        x, y = _adb_get_relative_point(target, 0.09, 0.95)
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
        _sleep_after_instagram_launch(target, logger=log, delay_seconds=10)

        # A fresh profile is walked through a chain of blockers on first launch
        # before the feed: the "Set up on new device" ads-consent screens (Get
        # started -> subscribe/free-with-ads -> Agree cookies -> manage ad
        # experience -> allow contacts) and the runtime permission dialogs
        # (contacts, location/GPS, photos). Walk and clear the whole chain via UI
        # dump before we look for the feed -- otherwise a dialog covers the app,
        # feed verification never succeeds, and the warm-up aborts (previously
        # this forced a manual tap). This no-ops when no prompt is up.
        if interruptions.handle_blocking_prompts(target, adb_client, logger=log, flow=self):
            emit("info", "Cleared a new-device onboarding/permission prompt for profile %s before warm-up", profile.id)

        if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=log, max_attempts=5, retry_delay_seconds=5):
            # A permission prompt (or other interruption) may still be sitting on
            # top of the app -- handle it and re-verify once before giving up,
            # instead of aborting with the dialog on screen.
            outcome = interruptions.check_and_handle(target, adb_client, logger=log, flow=self)
            _flag = interruptions.account_flag_for(outcome)
            if _flag:
                emit("warning", "Instagram flagged profile %s (%s); aborting warm-up flow", profile.id, _flag)
                return {"profile_id": profile.id, "target": target, "aborted": False, "account_flag": _flag}
            if outcome in (interruptions.OUTCOME_HANDLED, interruptions.OUTCOME_RESTART):
                emit("info", "Handled an interruption for profile %s; re-verifying the feed", profile.id)
                if not _adb_ensure_instagram_feed_visible(target, adb_client, logger=log, max_attempts=5, retry_delay_seconds=5):
                    emit("warning", "Instagram feed verification failed after handling an interruption for profile %s; aborting warm-up flow", profile.id)
                    return {"profile_id": profile.id, "target": target, "aborted": False}
            else:
                emit("warning", "Instagram feed verification failed for profile %s; aborting warm-up flow", profile.id)
                return {"profile_id": profile.id, "target": target, "aborted": False}

        # Instagram sometimes opens on the Reels tab (same activity as the feed,
        # so the foreground check can't tell them apart). Detect that by OCR and,
        # if we're on Reels, tap the Home (house) button to return to the feed
        # before we start scrolling -- retrying a safer, higher position if a tap
        # lands on the Android back button, and cold-relaunching only as a last
        # resort. Otherwise we'd swipe through Reels instead of the feed.
        self._ensure_on_home_feed(target, adb_client, logger=log)

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

        # Open the Activity (notifications) feed. Tap the Home (house) button to
        # scroll the feed to the very top so the top bar with the Notifications
        # heart becomes visible, verify the heart via a UI dump, and retry the
        # Home tap (safer positions, relaunching if a tap closed the app) until
        # the heart is found and tapped.
        activity_opened = self._open_activity_feed(target, adb_client, logger=log)
        if check_abort():
            return {"profile_id": profile.id, "target": target, "aborted": True}
        if hasattr(adb_client, "mark_progress_step"):
            adb_client.mark_progress_step()

        if not activity_opened:
            emit("warning", "Could not open the Activity feed for profile %s; skipping the follow step", profile.id)
        else:
            time.sleep(3)
            for scroll_index in range(5):
                if check_abort():
                    return {"profile_id": profile.id, "target": target, "aborted": True}
                adb_client.run_command(self._build_scroll_down_command(target))
                emit("info", "Scrolled notifications list (step %d/5)", scroll_index + 1)
                try:
                    if hasattr(adb_client, "mark_progress_step"):
                        adb_client.mark_progress_step()
                except Exception:
                    pass
                time.sleep(2)

            try:
                follows = self._follow_visible_accounts(target, adb_client, logger=log, should_stop=should_stop, max_follows=5)
                emit("info", "Followed %d account(s) for profile %s", follows, profile.id)
                if hasattr(adb_client, "mark_progress_step"):
                    adb_client.mark_progress_step()
            except Exception as exc:  # pragma: no cover - diagnostic path
                emit("exception", "Unexpected error during follow processing for profile %s: %s", profile.id, exc)
                return {"profile_id": profile.id, "target": target, "aborted": False}

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
