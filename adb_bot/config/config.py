import os

API_URL = "https://api.multilogin.com/mobile_profiles/phone/adb/info"
DEFAULT_PROFILE_IDS = ["625149430776987991", "624310694145163612"]

def get_bearer_token() -> str:
    """The MultiLogin token, from the environment or saved settings.

    There is deliberately no hardcoded fallback. This used to carry a real
    workspace Automation Token with `owner` role and a ten-year expiry, which
    would have been published the moment this repo went to GitHub. Tokens live
    in the environment (`/etc/adbbot/env` on the server, machine-level variables
    on Windows) or in the app's dev settings, never in source.

    Returns "" when nothing is configured; callers already report that, and
    `run_loop doctor` explains it.
    """
    for variable in ("MULTILOGIN_BEARER_TOKEN", "MULTILOGIN_TOKEN"):
        token = (os.getenv(variable) or "").strip()
        if token:
            return token
    # Imported lazily so this module stays a leaf for anything that only wants
    # API_URL / DEFAULT_PROFILE_IDS.
    from adb_bot.config.settings import get_saved_bearer_token
    return get_saved_bearer_token()


def get_profile_ids() -> list[str]:
    raw_ids = os.getenv("MULTILOGIN_PROFILE_IDS")
    if raw_ids:
        return [profile_id.strip() for profile_id in raw_ids.split(",") if profile_id.strip()]
    return DEFAULT_PROFILE_IDS.copy()


def extract_active_profiles(api_response: dict | None) -> list[dict]:
    """Return only active profiles that contain ADB credentials."""
    if not api_response:
        return []

    items = api_response.get("data", {}).get("items", []) or []
    active_profiles: list[dict] = []

    for item in items:
        profile_id = item.get("id")
        status = item.get("status")

        # Accept both numeric status codes and string labels
        if isinstance(status, int):
            # Treat 2 as 'active' (as observed in API responses)
            is_active = status == 2
        else:
            is_active = str(status).lower() in {"active", "ready"}

        if not is_active:
            continue

        # Ensure required ADB credentials exist
        ip = item.get("ip")
        port = item.get("port")
        pwd = item.get("pwd")
        if not ip or not port or not pwd:
            continue

        active_profiles.append(item)

    return active_profiles
