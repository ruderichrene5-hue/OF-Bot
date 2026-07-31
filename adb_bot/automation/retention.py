"""Delete media the bot has finished with, so the server's disk doesn't fill.

Two things pile up on a long-running server:

1. **Used input videos** -- the `used/` folder the media queue moves a clip into
   after it has been posted.
2. **Spoofed variants that have already been posted** -- one file per account per
   raw video adds up fast.

Both are only removed once they are provably finished with, and only after a
grace period (default 2 days) in case something needs checking.

The safety rule for variants: a file is deleted **only when its Airtable Spoof
Variant row says `Used`**. A `Ready`/`Pending` variant is still waiting for a
scheduled post -- deleting it would break that post -- so it is never touched, no
matter how old it is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from adb_bot.clients import airtable as at

DEFAULT_MAX_AGE_DAYS = 2
USED_FOLDER_NAME = "used"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


@dataclass
class PurgeReport:
    deleted: list = field(default_factory=list)      # paths removed
    kept: int = 0                                    # too new, or still needed
    errors: list = field(default_factory=list)       # (path, message)
    dry_run: bool = True
    freed_bytes: int = 0

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        return (f"[{mode}] deleted={len(self.deleted)} kept={self.kept} "
                f"errors={len(self.errors)} freed={self.freed_bytes / 1_048_576:.1f} MB")


def is_older_than(path: Path, max_age_days: float, now: float | None = None) -> bool:
    """True when `path`'s last modification is further back than `max_age_days`."""
    now = time.time() if now is None else now
    try:
        return (now - path.stat().st_mtime) > (max_age_days * 86400)
    except OSError:
        return False


def _delete(path: Path, report: PurgeReport, dry_run: bool) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if dry_run:
        report.deleted.append(str(path))
        report.freed_bytes += size
        return
    try:
        path.unlink()
        report.deleted.append(str(path))
        report.freed_bytes += size
    except OSError as exc:
        report.errors.append((str(path), str(exc)))


def purge_used_inputs(media_roots, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                      dry_run: bool = True, logger=None, now: float | None = None) -> PurgeReport:
    """Delete videos in each root's `used/` folder older than `max_age_days`.

    Only the `used/` subfolder is touched -- clips still waiting to be posted sit
    in the parent folder and are never considered.
    """
    report = PurgeReport(dry_run=dry_run)
    for root in media_roots or []:
        if not root:
            continue
        used_dir = Path(root) / USED_FOLDER_NAME
        if not used_dir.is_dir():
            continue
        for item in sorted(used_dir.iterdir()):
            if not item.is_file() or item.suffix.lower() not in VIDEO_EXTS:
                continue
            if is_older_than(item, max_age_days, now):
                _delete(item, report, dry_run)
            else:
                report.kept += 1
    if logger:
        logger.info("retention (used inputs): %s", report.summary())
    return report


def purge_used_variants(airtable, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
                        dry_run: bool = True, logger=None, now: float | None = None) -> PurgeReport:
    """Delete spoofed variant files whose Airtable row is marked `Used` and whose
    file is older than `max_age_days`.

    Variants that are still Ready/Pending are left alone whatever their age --
    they are the media a scheduled post is going to use.
    """
    report = PurgeReport(dry_run=dry_run)
    try:
        variants = airtable.variants_by_id()
    except Exception as exc:
        report.errors.append(("<airtable>", str(exc)))
        if logger:
            logger.warning("retention: could not read Spoof Variants: %s", exc)
        return report

    for _rec_id, info in (variants or {}).items():
        path_text = (info or {}).get("file_path")
        status = (info or {}).get("status")
        if not path_text:
            continue
        if status != at.SV_STATUS_USED:
            report.kept += 1          # still needed by a pending post
            continue
        path = Path(path_text)
        if not path.is_file():
            continue                   # already gone; nothing to do
        if is_older_than(path, max_age_days, now):
            _delete(path, report, dry_run)
        else:
            report.kept += 1
    if logger:
        logger.info("retention (used variants): %s", report.summary())
    return report


def prune_empty_dirs(root: str | None, logger=None, dry_run: bool = True) -> int:
    """Remove directories left empty after a purge. Returns how many were (or
    would be) removed. The root itself is kept."""
    if not root:
        return 0
    base = Path(root)
    if not base.is_dir():
        return 0
    removed = 0
    # Deepest first, so a parent emptied by its children is caught in one pass.
    for path in sorted((p for p in base.rglob("*") if p.is_dir()),
                       key=lambda p: len(p.parts), reverse=True):
        try:
            if any(path.iterdir()):
                continue
            removed += 1
            if not dry_run:
                path.rmdir()
        except OSError:
            continue
    if logger and removed:
        logger.info("retention: %s empty folder(s) %s", removed, "found" if dry_run else "removed")
    return removed
