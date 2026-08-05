import json
import os
import sys
from pathlib import Path


def get_app_data_dir() -> Path:
    """Return a stable per-user directory for settings/logs, independent of CWD.

    Needed so a packaged .app (whose working directory isn't the project root)
    and a script run from any directory both read/write the same files.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "ADB Bot"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "ADB Bot"
    else:
        base = Path.home() / ".adb_bot"
    base.mkdir(parents=True, exist_ok=True)
    return base


SETTINGS_FILE = get_app_data_dir() / "dev_settings.json"

_LEGACY_SETTINGS_FILE = Path("dev_settings.json")


def _migrate_legacy_settings_file() -> None:
    """One-time copy of a pre-existing CWD-relative dev_settings.json, if any."""
    if SETTINGS_FILE.exists():
        return
    if not _LEGACY_SETTINGS_FILE.exists():
        return
    try:
        SETTINGS_FILE.write_text(_LEGACY_SETTINGS_FILE.read_text())
    except (IOError, OSError):
        pass


def load_settings() -> dict:
    """Load dev control settings from file. Returns defaults if file doesn't exist."""
    _migrate_legacy_settings_file()
    if not SETTINGS_FILE.exists():
        return {
            "bearer_token": "",
            "batch_launch_delay_seconds": 1,
            "readiness_wait_seconds": 10,
            "readiness_max_attempts": 2,
        }
    
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {
            "bearer_token": "",
            "batch_launch_delay_seconds": 1,
            "readiness_wait_seconds": 10,
            "readiness_max_attempts": 2,
        }


def save_settings(settings: dict) -> bool:
    """Save dev control settings to file. Returns True if successful."""
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except IOError as e:
        print(f"Failed to save settings: {e}")
        return False


def get_saved_bearer_token() -> str:
    """Get the MultiLogin token from saved settings, else MULTILOGIN_TOKEN.

    The env fallback matters on a server: systemd units inherit nothing from a
    shell, so the token arrives via /etc/adbbot/env. Without it `doctor` reported
    "no token configured" while the loops -- which read the env var directly --
    worked fine.
    """
    return _get_saved_or_env("bearer_token", "MULTILOGIN_TOKEN")


def _get_saved_or_env(settings_key: str, env_key: str) -> str:
    """Prefer the saved settings value, then an environment override, else ''."""
    settings = load_settings()
    saved = (settings.get(settings_key, "") or "").strip()
    if saved:
        return saved
    return (os.environ.get(env_key, "") or "").strip()


def get_saved_airtable_token() -> str:
    return _get_saved_or_env("airtable_token", "AIRTABLE_TOKEN")


# OFM Agency OS test base (the clone built for orchestrator testing). Used as a
# fallback so the app points at the test base out of the box; a saved value or
# the AIRTABLE_BASE_ID env var still wins.
DEFAULT_AIRTABLE_BASE_ID = "appNAm6iTuzmOn4ib"


def get_saved_airtable_base_id() -> str:
    return _get_saved_or_env("airtable_base_id", "AIRTABLE_BASE_ID") or DEFAULT_AIRTABLE_BASE_ID


def get_saved_airtable_table_name() -> str:
    return _get_saved_or_env("airtable_table_name", "AIRTABLE_TABLE_NAME") or "Profiles"


def get_saved_batch_launch_delay() -> int:
    """Get saved batch launch delay or default."""
    settings = load_settings()
    try:
        return max(0, int(settings.get("batch_launch_delay_seconds", 1)))
    except (ValueError, TypeError):
        return 1


def get_saved_readiness_wait() -> int:
    """Get saved readiness wait time or default."""
    settings = load_settings()
    try:
        return max(0, int(settings.get("readiness_wait_seconds", 10)))
    except (ValueError, TypeError):
        return 10


def get_saved_readiness_attempts() -> int:
    """Get saved readiness max attempts or default."""
    settings = load_settings()
    try:
        return max(1, int(settings.get("readiness_max_attempts", 2)))
    except (ValueError, TypeError):
        return 2


# How many MultiLogin phones may be open at once ACROSS EVERY LOOP. This is the
# ceiling `MAX_CONCURRENT_PROFILES` was always mistaken for: that one is applied
# by each loop independently, so posting (10) + warmup (10) + recheck (1) could
# legitimately have 21 phones live with nothing coordinating them.
#
# 12 is derived from what the box actually survived on 2026-08-04. The kernel's
# OOM dump counted 78 WebKitWebProcess (12.8 GB) plus 77 phone processes
# (3.6 GB) -- ~215 MB of RSS per live phone -- against 15.2 GiB of RAM, so the
# hard wall is around 68 phones and the machine died at ~172 launches vs 64
# shutdowns. 12 x 215 MB is ~2.6 GB of driven phones; even if the tail of
# already-shut-down-but-not-yet-gone phones doubles that, ~5 GB sits on top of a
# ~4.5 GB baseline (system + MultiLogin agent) and stays inside RAM, leaving the
# 8 GB of swap as an untouched second line of defence rather than a working set.
#
# It is deliberately ABOVE any single loop's cap (10), so ordinary posting is
# never throttled by it: the ceiling only bites when a second loop overlaps,
# which is exactly the case that was uncontrolled.
DEFAULT_MAX_LIVE_PROFILES = 12


def get_saved_max_live_profiles() -> int:
    """The cross-loop ceiling on live phones: saved setting, else
    ADBBOT_MAX_LIVE_PROFILES, else the default. Never less than 1.

    Not routed through `_get_saved_or_env` because that assumes a string value
    and would raise on a JSON number, which is how anyone would write this one.
    """
    raw = load_settings().get("max_live_profiles", None)
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("ADBBOT_MAX_LIVE_PROFILES", "")
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_MAX_LIVE_PROFILES


def get_saved_flow_speed() -> str:
    """Speed profile for on-device flows: 'fast' (0.5x waits), 'normal', or
    'slow' (1.5x). Only scales fallback sleeps -- a step that confirms the next
    screen is ready always returns as fast as the phone allows."""
    return _get_saved_or_env("flow_speed", "FLOW_SPEED") or "normal"


def get_scheduler_config() -> dict:
    """Per-loop scheduler config: {loop: {'enabled': bool, 'interval_min': int}}.
    Empty until the user configures it in the Scheduler window."""
    settings = load_settings()
    cfg = settings.get("scheduler")
    return cfg if isinstance(cfg, dict) else {}


def save_scheduler_config(config: dict) -> bool:
    settings = load_settings()
    settings["scheduler"] = config
    return save_settings(settings)


def get_saved_raw_videos_dir() -> str:
    """Root folder the spoofing pipeline scans for new raw videos (per-model
    subfolders underneath). On the server this is the Google-Drive-synced or
    downloaded `01_Raw_Videos` root; empty until configured."""
    return _get_saved_or_env("raw_videos_dir", "RAW_VIDEOS_DIR")


def get_saved_spoofer_python() -> str:
    """Interpreter for the video_spoofer project (its own venv) used to encode
    spoofed variants."""
    return _get_saved_or_env("spoofer_python", "SPOOFER_PYTHON")


def get_saved_spoofer_root() -> str:
    """Root folder of the video_spoofer project (working dir for its CLI)."""
    return _get_saved_or_env("spoofer_root", "SPOOFER_ROOT")


def get_saved_drive_folder_id() -> str:
    """Google Drive folder id of `01_Raw_Videos` (the pipeline's raw source).
    When set together with a service-account key, Drive wins over the local dir."""
    return _get_saved_or_env("drive_raw_folder_id", "DRIVE_RAW_FOLDER_ID")


def get_saved_google_service_account_json() -> str:
    """Path to the Google service-account JSON key used for Drive access."""
    return _get_saved_or_env("google_service_account_json", "GOOGLE_SERVICE_ACCOUNT_JSON")


def get_saved_spoofed_videos_dir() -> str:
    """Root folder the pipeline writes spoofed variants into (`02_Spoofed_Videos`
    on the server's local disk); empty until configured."""
    return _get_saved_or_env("spoofed_videos_dir", "SPOOFED_VIDEOS_DIR")


# The flows the per-folder media mapping feeds. Only these read a Profile's
# media_path, so mapping a folder must not change any other flow's behaviour.
# Lives here (a leaf module) so both the UI and the headless runners can use it.
REEL_FLOWS = ("instagram_reel_upload", "instagram_reel_upload_u2",
              # The share-Intent probe pushes a real reel video, so it needs the
              # same per-folder media. It reads media_path and ignores caption.
              "instagram_reel_intent_probe")


def get_folder_media_paths() -> dict:
    """Per-Multilogin-folder reels media folders: {folder_key: local_path}.

    `folder_key` is the Multilogin folder id (its name only when the API gave no
    id). Set once in the Profiles list and reused by every later run, so a run
    only has to pick profiles. Empty until the user maps a folder; profiles in
    an unmapped folder keep falling back to the global story media setting."""
    settings = load_settings()
    mapping = settings.get("folder_media_paths")
    if not isinstance(mapping, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in mapping.items()
        if key and isinstance(value, str) and value.strip()
    }


def save_folder_media_paths(mapping: dict) -> bool:
    """Merge-save the folder -> media folder map, keeping the rest of the file."""
    settings = load_settings()
    settings["folder_media_paths"] = {
        str(key): str(value).strip()
        for key, value in (mapping or {}).items()
        if key and str(value or "").strip()
    }
    return save_settings(settings)


def get_folder_media_path(folder_key: str) -> str:
    """Media folder mapped to one Multilogin folder, or '' when unmapped."""
    if not folder_key:
        return ""
    raw = get_folder_media_paths().get(str(folder_key), "")
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser())
    except Exception:
        return raw


def get_saved_story_media_path() -> str:
    """Get saved story media file path or empty string if not set."""
    settings = load_settings()
    raw = (settings.get("story_media_path", "") or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser())
    except Exception:
        return raw
