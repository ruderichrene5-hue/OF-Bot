"""Content / spoofing pipeline (checklist loop #3).

For each new raw video under a model, create a Content Pipeline row and generate
one UNIQUE spoofed variant per currently-active account under that model (never
reuse an output across accounts -- duplicate-content risk on IG), writing a
Spoof Variants row per file.

The raw videos live on Google Drive on the server; the spoofed outputs stay on
the server's disk. Raw access is behind a `RawSource` so the local-folder
implementation works now and a Drive implementation drops in later. The actual
spoofing is an injected `spoof_fn` so the whole orchestration is testable without
running FFmpeg; `build_cli_spoofer()` provides the real one (shells out to the
video_spoofer CLI).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from adb_bot.clients import airtable as at

# Video extensions the pipeline treats as raw sources.
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

SPOOF_METHOD = "video_spoofer/vtf run"

# Most variants one run will produce before stopping and leaving the rest for the
# next cycle. Without this, the first run against a full raw library would kick
# off hundreds of encodes back-to-back and fill the disk.
MAX_VARIANTS_PER_RUN = 20

# What a raw video gets spoofed *for*. See run_pipeline().
TARGETS_ACCOUNTS = "accounts"
TARGETS_PROFILES = "profiles"


@dataclass
class RawVideo:
    model: str          # folder/model name, e.g. "Nikki"
    name: str           # file name -- the stable key against Content Pipeline
    path: str           # local path (empty for a remote source until resolved)
    raw_link: str | None = None
    source_id: str | None = None   # remote id (e.g. Drive file id) for lazy fetch


class LocalRawSource:
    """Raw videos as per-model subfolders on a local (or Drive-synced) path:
    ``{raw_root}/{Model}/*.mp4``.

    A raw source implements:
      - ``list_by_model() -> {model: [RawVideo]}``
      - ``resolve(video) -> local path`` (already local here)
      - ``release(video, path)`` (no-op here; remote sources delete the temp copy)
    """

    def __init__(self, raw_root: str):
        self.raw_root = Path(raw_root)

    def resolve(self, video: RawVideo) -> str | None:
        return video.path or None

    def release(self, video: RawVideo, path: str) -> None:
        return None

    def list_by_model(self) -> dict:
        out: dict = {}
        if not self.raw_root.is_dir():
            return out
        for model_dir in sorted(self.raw_root.iterdir()):
            if not model_dir.is_dir():
                continue
            videos = [
                RawVideo(model=model_dir.name, name=f.name, path=str(f))
                for f in sorted(model_dir.iterdir())
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS
            ]
            if videos:
                out[model_dir.name] = videos
        return out


class DriveRawSource:
    """Raw videos in Google Drive: per-model subfolders under a root folder
    (`01_Raw_Videos/{Model}/*.mp4`), matching the local layout.

    Listing is cheap (metadata only); the file itself is downloaded to a temp
    path in :meth:`resolve` right before spoofing and deleted in :meth:`release`,
    so the server never keeps a full mirror of the raw library on disk.
    """

    def __init__(self, client, root_folder_id: str, temp_dir: str | None = None):
        self.client = client
        self.root_folder_id = root_folder_id
        self.temp_dir = temp_dir

    def list_by_model(self) -> dict:
        out: dict = {}
        for folder in self.client.list_subfolders(self.root_folder_id):
            videos = [
                RawVideo(
                    model=folder["name"],
                    name=item["name"],
                    path="",                       # not local yet -- see resolve()
                    raw_link=item.get("link"),
                    source_id=item["id"],
                )
                for item in self.client.list_files(folder["id"])
                if Path(item["name"]).suffix.lower() in VIDEO_EXTS
            ]
            if videos:
                out[folder["name"]] = videos
        return out

    def resolve(self, video: RawVideo) -> str | None:
        if not video.source_id:
            return video.path or None
        base = Path(self.temp_dir) if self.temp_dir else Path(tempfile.gettempdir())
        dest = base / "adbbot_raw" / video.model / video.name
        return self.client.download(video.source_id, str(dest))

    def release(self, video: RawVideo, path: str) -> None:
        # Only clean up files we downloaded, never a caller-supplied local file.
        if not video.source_id or not path:
            return
        try:
            Path(path).unlink()
        except OSError:
            pass


@dataclass
class PipelineReport:
    processed_videos: list = field(default_factory=list)   # raw names turned into Content Pipeline rows
    variants_created: int = 0
    skipped: list = field(default_factory=list)            # (name, reason)
    errors: list = field(default_factory=list)             # (name, message)
    dry_run: bool = True

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        return (f"[{mode}] raw_videos={len(self.processed_videos)} "
                f"variants={self.variants_created} skipped={len(self.skipped)} errors={len(self.errors)}")


def _seed_for(raw_name: str, handle: str) -> int:
    """Deterministic per (raw video, account) seed so a given account always gets
    the same distinct variant, and two accounts never get the same one."""
    digest = hashlib.sha256(f"{raw_name}|{handle}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def safe_name(text: str) -> str:
    """Filesystem- and shell-safe filename fragment.

    Output paths end up inside Android shell commands (the media-scanner
    broadcast takes the pushed file's path), and the device shell splits on
    spaces, so a name like ``clip 1 aug.mp4`` would break on the phone even
    though `adb push` itself handles it. Collapse anything outside
    ``[A-Za-z0-9._-]`` to a single underscore.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text).strip())
    return cleaned.strip("._-") or "unnamed"


def next_run_dir(out_root: str, model: str) -> Path:
    """``<out_root>/<Model>/run<N>``, N one past the highest run folder present.

    One run folder per raw video. Numbering continues across pipeline
    invocations -- a video picked up tomorrow becomes the next run rather than
    reopening an existing one. A run folder removed by cleanup can have its
    number reused; that is harmless because the Airtable rows pointing at it
    were only cleaned up once already marked Used.
    """
    model_dir = Path(out_root) / model
    highest = 0
    if model_dir.is_dir():
        for child in model_dir.iterdir():
            if not child.is_dir():
                continue
            match = re.fullmatch(r"run(\d+)", child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return model_dir / f"run{highest + 1}"


def finalize_variant(produced: Path, raw_name: str, handle: str) -> Path:
    """Rename a freshly-spoofed file to ``<source>__<handle><ext>``.

    Must happen before the next account is spoofed into the same run folder.
    `vtf` names every output after the SOURCE video, so without this the second
    account's encode overwrites the first (the CLI is run with ``--overwrite``),
    `build_cli_spoofer` then finds no new file, falls back to "newest video in
    the directory", and every account ends up sharing one file -- the exact
    duplicate-content problem the per-account variants exist to avoid.
    """
    target = produced.with_name(
        f"{safe_name(Path(raw_name).stem)}__{safe_name(handle)}{produced.suffix}"
    )
    if target != produced:
        produced.replace(target)   # replace() overwrites an existing target
    return target


def build_cli_spoofer(spoofer_python: str, spoofer_cwd: str, preset: str = "normal"):
    """Return a `spoof_fn(raw_path, out_dir, seed, logger) -> Path|None` that runs
    the video_spoofer CLI (`vtf run`) and returns the produced variant file.

    `spoofer_python` is the interpreter for the video_spoofer project (its own
    venv); `spoofer_cwd` is that project's root.
    """
    def spoof_fn(raw_path: str, out_dir: str, seed: int, logger=None) -> Path | None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        before = {p for p in out.iterdir()} if out.exists() else set()
        cmd = [
            spoofer_python, "-m", "video_testing_framework.cli", "run", raw_path,
            "--dest", str(out), "--preset", preset, "--seed", str(seed),
            "--variants", "1", "--overwrite",
        ]
        if logger:
            logger.info("spoofer: %s", " ".join(cmd))
        proc = subprocess.run(cmd, cwd=spoofer_cwd, capture_output=True, text=True)
        if proc.returncode != 0:
            if logger:
                logger.warning("spoofer failed (%s): %s", proc.returncode, (proc.stderr or "").strip()[-400:])
            return None
        # The new file is whatever appeared in the dest that wasn't there before.
        produced = [p for p in out.iterdir() if p.suffix.lower() in VIDEO_EXTS and p not in before]
        if not produced:
            # --overwrite reuses names; fall back to newest video in the dir.
            vids = [p for p in out.iterdir() if p.suffix.lower() in VIDEO_EXTS]
            produced = [max(vids, key=lambda p: p.stat().st_mtime)] if vids else []
        return produced[0] if produced else None

    return spoof_fn


def build_source(raw_root: str | None = None, drive_folder_id: str | None = None,
                 service_account_json: str | None = None, logger=None):
    """Pick the raw source from config: Drive when a folder id + key are set,
    otherwise the local folder. Returns None if neither is configured."""
    if drive_folder_id and service_account_json:
        from adb_bot.clients.gdrive import DriveClient
        if logger:
            logger.info("pipeline: using Google Drive folder %s as the raw source", drive_folder_id)
        return DriveRawSource(DriveClient(service_account_json), drive_folder_id)
    if raw_root:
        if logger:
            logger.info("pipeline: using local raw folder %s", raw_root)
        return LocalRawSource(raw_root)
    return None


def run_pipeline(airtable, logger, raw_root: str | None, out_root: str | None,
                 spoof_fn=None, source=None, dry_run: bool = True,
                 drive_folder_id: str | None = None, service_account_json: str | None = None,
                 max_variants: int | None = MAX_VARIANTS_PER_RUN,
                 targets: str = TARGETS_ACCOUNTS) -> PipelineReport:
    """Scan for new raw videos and spoof one variant per target under the model.

    `targets` picks what a "target" is:

    - ``'accounts'`` (default): Airtable Accounts at Lifecycle Stage Active.
    - ``'profiles'``: the MLX profile inventory, via Profiles (Cloning). Use this
      for models that have phones but no Accounts rows yet -- the variant links
      to the profile instead of an account.

    Either way a target is ``{'handle': str}`` plus an id, so everything below
    this point is the same for both.

    `spoof_fn(raw_path, out_dir, seed, logger) -> Path|None` does the actual
    encoding; if omitted, real runs need one (dry-runs don't call it).

    **Encoding is deliberately serial** -- one ffmpeg at a time. Video encoding
    already saturates the CPU, and the phone flows have to share the machine, so
    running encodes in parallel would just make everything slower and flakier.

    `max_variants` caps how much work a single run takes on (default
    :data:`MAX_VARIANTS_PER_RUN`); the rest is left for the next run. The cap is
    applied between *videos*, never mid-video, so an account can never be left
    without the variant its siblings got -- meaning a run may overshoot by at
    most one video's worth of accounts. `None` disables the cap.
    """
    report = PipelineReport(dry_run=dry_run)

    if source is None:
        source = build_source(raw_root, drive_folder_id, service_account_json, logger)
    if source is None:
        logger.warning("pipeline: no raw source configured (set RAW_VIDEOS_DIR, or a Drive "
                       "folder id + service-account key); nothing to do")
        return report
    out_root = out_root or ""

    try:
        by_model = source.list_by_model()
    except Exception as exc:
        logger.error("pipeline: could not list the raw source: %s", exc)
        report.errors.append(("<source>", str(exc)))
        return report
    if not by_model:
        logger.info("pipeline: no raw videos found in the configured source")
        return report

    existing = airtable.content_pipeline_names()
    by_profile = targets == TARGETS_PROFILES
    active_by_model = (airtable.profile_targets_by_model() if by_profile
                       else airtable.active_accounts_by_model())
    model_ids = airtable.models_by_name()

    capped = False
    for model, videos in by_model.items():
        accounts = active_by_model.get(model.lower(), [])
        for video in videos:
            if video.name in existing:
                continue  # already processed on a prior run
            if not accounts:
                what = "MLX profiles" if by_profile else "active accounts"
                report.skipped.append((video.name, f"no {what} under model '{model}'"))
                continue

            # Budget check happens between videos so a video is always done in
            # full -- a partially-spoofed video would leave some accounts without
            # a variant and never be revisited (its name is already recorded).
            if max_variants is not None and report.variants_created >= max_variants:
                report.skipped.append((video.name, f"run cap of {max_variants} variant(s) reached"))
                capped = True
                continue

            if dry_run:
                report.processed_videos.append(video.name)
                report.variants_created += len(accounts)
                logger.info("[DRY-RUN] would spoof %s for %s %s under %s",
                            video.name, len(accounts),
                            "profile(s)" if by_profile else "account(s)", model)
                continue

            if spoof_fn is None:
                report.errors.append((video.name, "no spoof_fn provided for an --apply run"))
                continue

            # Fetch the raw file (a no-op locally; a download for a remote source).
            try:
                local_raw = source.resolve(video)
            except Exception as exc:
                logger.warning("could not fetch raw video %s: %s", video.name, exc)
                local_raw = None
            if not local_raw:
                report.errors.append((video.name, "could not fetch the raw video"))
                continue

            cp_id = airtable.create_content_pipeline(video.name, model_ids.get(model.lower()), video.raw_link)
            if not cp_id:
                report.errors.append((video.name, "failed to create Content Pipeline row"))
                source.release(video, local_raw)
                continue
            report.processed_videos.append(video.name)

            # One run folder per raw video, shared by every account under the
            # model: <out_root>/<Model>/run<N>/<source>__<handle>.mp4
            run_dir = next_run_dir(out_root, model)
            logger.info("pipeline: %s -> %s", video.name, run_dir)

            any_failed = False
            try:
                for acct in accounts:
                    handle = acct["handle"]
                    try:
                        produced = spoof_fn(local_raw, str(run_dir), _seed_for(video.name, handle), logger)
                    except Exception as exc:  # a bad encode shouldn't kill the batch
                        logger.warning("spoof error for %s/%s: %s", model, handle, exc)
                        produced = None
                    if not produced:
                        any_failed = True
                        report.errors.append((f"{video.name} -> {handle}", "spoof produced no file"))
                        continue
                    # Rename before the next account runs -- see finalize_variant().
                    # A failure here must not be recorded: the un-renamed file
                    # would be overwritten by the next account and the row would
                    # point at somebody else's video.
                    try:
                        variant_path = finalize_variant(Path(produced), video.name, handle)
                    except OSError as exc:
                        logger.warning("could not name the variant for %s/%s: %s", model, handle, exc)
                        any_failed = True
                        report.errors.append((f"{video.name} -> {handle}", f"could not name the variant: {exc}"))
                        continue
                    airtable.create_spoof_variant(
                        cp_id,
                        None if by_profile else acct["account_id"],
                        str(variant_path),
                        method=SPOOF_METHOD,
                        target_profile_id=acct["profile_id"] if by_profile else None,
                    )
                    report.variants_created += 1
            finally:
                source.release(video, local_raw)

            airtable.set_content_pipeline_spoofed(cp_id, failed=any_failed)

    if capped:
        logger.info("pipeline: stopped at the %s-variant run cap; remaining videos "
                    "will be picked up next run", max_variants)
    logger.info("pipeline: %s", report.summary())
    return report
