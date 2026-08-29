"""Raw video content for GeeLark's Active_Posting cycle.

Confirmed structure 2026-08-30, independent of MLX's `01_Raw_Videos` (own
Drive root, `DRIVE_GEELARK_RAW_FOLDER_ID`):

    GeeLark_Raw_Videos/<Model>/<YYYY-MM-DD>/*.mp4

One subfolder per model (exactly matching the GeeLark group name), one
subfolder per calendar day (Europe/Berlin) inside that. The bot only ever
looks at *today's* date folder -- yesterday's is never touched again, and a
missing or empty date folder means no content for that model today, not a
fallback to older videos. This is a deliberate choice: uploading ahead of
time (several days' folders at once) is fine, but forgetting to upload for
today must fail loudly (no post) rather than silently repeat old content.

Within a day, the same video can serve as many accounts as that model needs
-- each gets its own freshly-spoofed variant (`_seed_for` in spoof_pipeline
keeps this deterministic per (video, handle), same as the MLX pipeline), so
"unlimited accounts per video" and "no repeats across days" are not in
tension with each other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from adb_bot.automation.spoof_pipeline import (
    VIDEO_EXTS,
    RawVideo,
    _seed_for,
    build_cli_spoofer,
    finalize_variant,
    next_run_dir,
)
from adb_bot.clients.gdrive import DriveClient

BERLIN = ZoneInfo("Europe/Berlin")

DEFAULT_SPOOF_OUT_ROOT = "/root/.adb_bot/geelark_spoof_variants"


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _find_model_folder_id(client: DriveClient, root_folder_id: str, model: str
                          ) -> str | None:
    for folder in client.list_subfolders(root_folder_id):
        if str(folder.get("name", "")).strip().lower() == model.strip().lower():
            return folder.get("id")
    return None


def today_raw_videos(model: str, client: DriveClient | None = None,
                     root_folder_id: str | None = None,
                     today: date | None = None) -> list[RawVideo]:
    """Every video in `<root>/<model>/<today's Berlin date>/`, or an empty
    list if the model folder, the date folder, or both don't exist.

    An empty list is the normal, expected "nothing uploaded for today" case
    -- callers must treat it as "skip this model today", never as an error.
    """
    client = client or DriveClient(_env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    root_folder_id = root_folder_id or _env("DRIVE_GEELARK_RAW_FOLDER_ID")
    today = today or datetime.now(BERLIN).date()
    date_str = today.isoformat()

    model_folder_id = _find_model_folder_id(client, root_folder_id, model)
    if model_folder_id is None:
        return []

    date_folder_id = None
    for folder in client.list_subfolders(model_folder_id):
        if str(folder.get("name", "")).strip() == date_str:
            date_folder_id = folder.get("id")
            break
    if date_folder_id is None:
        return []

    return [
        RawVideo(model=model, name=item["name"], path="",
                raw_link=item.get("link"), source_id=item["id"])
        for item in client.list_files(date_folder_id)
        if Path(item["name"]).suffix.lower() in VIDEO_EXTS
    ]


@dataclass
class SpoofedContent:
    path: str          # local file, ready to hand to run_active_posting_cycle
    raw_video: RawVideo
    handle: str


def spoof_for_handle(video: RawVideo, handle: str, client: DriveClient,
                     spoofer_python: str, spoofer_root: str,
                     out_root: str = DEFAULT_SPOOF_OUT_ROOT,
                     logger=None) -> SpoofedContent | None:
    """Download `video` (if not already local), spoof one variant for
    `handle`, return its path. `None` on any failure -- download, spoof, or
    otherwise -- logged but never raised, so one account's bad luck doesn't
    take the whole model's posting wave down with it.

    The caller owns deleting the returned file once it's been pushed to the
    phone (see geelark_lifecycle.run_active_posting_cycle) -- this function
    only ever creates it.
    """
    dest = Path("/tmp/geelark_raw_downloads") / video.model / video.name
    try:
        local_raw = client.download(video.source_id, str(dest))
    except Exception as exc:
        if logger:
            logger.warning("geelark_content: download failed for %s (%s)",
                           video.name, exc)
        return None

    try:
        spoof_fn = build_cli_spoofer(spoofer_python, spoofer_root)
        out_dir = next_run_dir(out_root, video.model)
        seed = _seed_for(video.name, handle)
        produced = spoof_fn(local_raw, str(out_dir), seed, logger=logger)
        if produced is None:
            if logger:
                logger.warning("geelark_content: spoof failed for %s -> %s",
                               video.name, handle)
            return None
        final_path = finalize_variant(produced, video.name, handle)
        return SpoofedContent(path=str(final_path), raw_video=video, handle=handle)
    finally:
        try:
            Path(local_raw).unlink()
        except OSError:
            pass


def get_post_media(model: str, handle: str, client: DriveClient | None = None,
                   spoofer_python: str | None = None, spoofer_root: str | None = None,
                   root_folder_id: str | None = None, out_root: str = DEFAULT_SPOOF_OUT_ROOT,
                   logger=None) -> SpoofedContent | None:
    """The one call `geelark_lifecycle.run_active_posting_cycle`'s caller
    needs: today's content for `model`, spoofed fresh for `handle`, or None
    if there's nothing to post today (no date folder, empty folder, or a
    download/spoof failure) -- always a valid "skip today" outcome, never
    an exception a scheduling loop has to catch."""
    client = client or DriveClient(_env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    videos = today_raw_videos(model, client=client, root_folder_id=root_folder_id)
    if not videos:
        if logger:
            logger.info("geelark_content: no content for %s today; skipping", model)
        return None

    import random
    video = random.choice(videos)
    return spoof_for_handle(
        video, handle, client,
        spoofer_python or _env("SPOOFER_PYTHON"),
        spoofer_root or _env("SPOOFER_ROOT"),
        out_root=out_root, logger=logger)
