from __future__ import annotations

from pathlib import Path
from typing import Any

from adb_bot.config.settings import REEL_FLOWS

__all__ = [
    "REEL_FLOWS",
    "build_foldered_profile_groups",
    "build_profile_options",
    "folders_missing_media",
    "get_available_flows",
    "resolve_folder_media_path",
]


def resolve_folder_media_path(
    profile_id: str,
    profile_to_folder: dict[str, str],
    folder_media_paths: dict[str, str],
) -> str | None:
    """Media folder a profile may draw from: strictly the one mapped to its own
    Multilogin folder.

    Returns None when the profile's folder has no mapping, which leaves the run
    on the previous behaviour (the global story media setting). A profile is
    never handed another folder's media -- the lookup only ever goes through
    that profile's own folder key, so an unmapped folder falls back rather than
    borrowing a mapped neighbour's clips.
    """
    folder_key = (profile_to_folder or {}).get(profile_id)
    if not folder_key:
        return None
    media_path = str((folder_media_paths or {}).get(folder_key, "") or "").strip()
    if not media_path:
        return None
    try:
        return str(Path(media_path).expanduser())
    except Exception:
        return media_path


def folders_missing_media(
    profile_ids: list[str],
    profile_to_folder: dict[str, str],
    folder_media_paths: dict[str, str],
    folder_names: dict[str, str] | None = None,
) -> list[str]:
    """Names of the selected profiles' folders that have no media mapping.

    Used to warn before a reels run that mixes mapped and unmapped folders --
    the unmapped ones silently fall back to the global media setting, which is
    exactly the case where the wrong clips could go out.
    """
    names: list[str] = []
    seen: set[str] = set()
    for profile_id in profile_ids or []:
        folder_key = (profile_to_folder or {}).get(profile_id) or ""
        if folder_key in seen:
            continue
        seen.add(folder_key)
        if str((folder_media_paths or {}).get(folder_key, "") or "").strip():
            continue
        names.append((folder_names or {}).get(folder_key) or folder_key or "Unassigned")
    return names


def build_profile_options(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for item in items:
        profile_id = str(item.get("id") or item.get("profile_id") or "").strip()
        name = str(
            item.get("name")
            or item.get("profile_name")
            or item.get("serial_name")
            or item.get("serialNo")
            or "Unnamed profile"
        ).strip()
        if not profile_id:
            continue
        options.append({
            "id": profile_id,
            "name": name,
            "label": f"{name} ({profile_id})",
        })
    return options


def build_foldered_profile_groups(
    profiles: list[dict[str, Any]],
    folders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    folder_lookup = {str(folder.get("folder_id") or ""): folder for folder in folders if folder.get("folder_id")}
    groups: list[dict[str, Any]] = []
    for folder in sorted(folders, key=lambda item: str(item.get("name") or item.get("folder_id") or "").strip().lower()):
        folder_id = str(folder.get("folder_id") or "").strip()
        if not folder_id:
            continue
        folder_name = str(folder.get("name") or folder_id).strip() or folder_id
        profiles_in_folder = [
            profile for profile in profiles if str(profile.get("folder_id") or "").strip() == folder_id
        ]
        profiles_in_folder = sorted(
            profiles_in_folder,
            key=lambda profile: str(
                profile.get("name")
                or profile.get("profile_name")
                or profile.get("serial_name")
                or profile.get("serialNo")
                or "Unnamed profile"
            ).strip().lower(),
        )
        if not profiles_in_folder:
            continue
        groups.append({
            "folder_id": folder_id,
            "name": folder_name,
            "profiles": [
                {
                    "id": str(profile.get("id") or profile.get("profile_id") or "").strip(),
                    "name": str(
                        profile.get("name")
                        or profile.get("profile_name")
                        or profile.get("serial_name")
                        or profile.get("serialNo")
                        or "Unnamed profile"
                    ).strip(),
                    "label": f"{str(profile.get('name') or profile.get('profile_name') or profile.get('serial_name') or profile.get('serialNo') or 'Unnamed profile').strip()} ({str(profile.get('id') or profile.get('profile_id') or '').strip()})",
                }
                for profile in profiles_in_folder
                if str(profile.get("id") or profile.get("profile_id") or "").strip()
            ],
        })

    if not groups:
        for profile in sorted(profiles, key=lambda item: str(
            item.get("name")
            or item.get("profile_name")
            or item.get("serial_name")
            or item.get("serialNo")
            or "Unnamed profile"
        ).strip().lower()):
            profile_id = str(profile.get("id") or profile.get("profile_id") or "").strip()
            if not profile_id:
                continue
            groups.append({
                "folder_id": "",
                "name": "Unassigned",
                "profiles": [{
                    "id": profile_id,
                    "name": str(
                        profile.get("name")
                        or profile.get("profile_name")
                        or profile.get("serial_name")
                        or profile.get("serialNo")
                        or "Unnamed profile"
                    ).strip(),
                    "label": f"{str(profile.get('name') or profile.get('profile_name') or profile.get('serial_name') or profile.get('serialNo') or 'Unnamed profile').strip()} ({profile_id})",
                }],
            })
            break

    return groups


def get_available_flows() -> list[dict[str, str]]:
    """Flows offered in the UI. uiautomator2 is the standard implementation now,
    so each flow appears once and the older screen-dump variants are no longer
    listed. Their classes stay registered, so an Airtable row that still names
    `instagram_reel_upload` or `update_bio` keeps running instead of erroring."""
    return [
        {"value": "warm_up_process", "label": "Warm Up Process (BETA)"},
        {"value": "instagram_story_upload", "label": "Instagram Story Upload (BETA)"},
        {"value": "instagram_reel_upload_u2", "label": "Instagram Reels Upload (BETA)"},
        {"value": "update_bio_u2", "label": "Update Bio (BETA)"},
        {"value": "update_profile_picture", "label": "Update Profile Picture (BETA)"},
    ]
