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

Two later additions keep that rule intact rather than widening it:

* :func:`purge_orphan_variants` is the one sweep that can delete a file Airtable
  says nothing about -- a variant finalized on disk whose `create_spoof_variant`
  write failed, which no status-driven sweep can ever reach. Because "no row"
  is not the same proof as "row says Used", it is **opt-in** (see
  ``run_loop --orphan-sweep``), runs on its own much longer window, and refuses
  to run at all if the Airtable listing looks incomplete.
* :func:`report_stranded_ready` only *counts*. Ready variants aimed at a parked
  profile never drain, but re-activating a profile is one Airtable edit and the
  post would then have no media, so they are reported and never removed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from adb_bot.clients import airtable as at

DEFAULT_MAX_AGE_DAYS = 2
USED_FOLDER_NAME = "used"

# Orphans get their own, far longer window. A variant is written to disk a
# moment before its Airtable row is created, so anything near the 2-day mark
# could simply be a row that had not landed yet when the listing was taken.
DEFAULT_ORPHAN_AGE_DAYS = 7

# Circuit breaker for the orphan sweep. The zero-row guard catches a listing
# that failed outright; this catches the nastier case -- a *truncated* listing
# (a pagination error swallowed upstream) which returns real rows but not all of
# them, and would make most of the folder look unreferenced. If more than this
# fraction of the files on disk look like orphans, the answer is "the listing is
# wrong", not "delete the folder".
DEFAULT_ORPHAN_MAX_FRACTION = 0.5

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


@dataclass
class PurgeReport:
    deleted: list = field(default_factory=list)      # paths removed
    kept: int = 0                                    # too new, or still needed
    errors: list = field(default_factory=list)       # (path, message)
    dry_run: bool = True
    freed_bytes: int = 0
    aborted: str | None = None                       # why the sweep refused to run

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        if self.aborted:
            mode = "ABORTED"
        head = (f"[{mode}] deleted={len(self.deleted)} kept={self.kept} "
                f"errors={len(self.errors)} freed={self.freed_bytes / 1_048_576:.1f} MB")
        return f"{head} ({self.aborted})" if self.aborted else head


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


def has_used_inbox(media_roots) -> bool:
    """True when at least one root actually has a `used/` folder.

    The reel/spoof path never makes one -- only the story-media queue does
    (`flows/story_media.py`) -- and the raw videos now come from Drive, so on
    this server the input sweep has nothing to look at. Without this the sweep
    logs a healthy-looking `deleted=0 kept=0`, which reads like "checked, found
    nothing" instead of "there was nowhere to check".
    """
    for root in media_roots or []:
        if root and (Path(root) / USED_FOLDER_NAME).is_dir():
            return True
    return False


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


def _known_variant_paths(variants) -> set:
    """Every file path any Spoof Variant row points at, at *every* status,
    resolved so a symlinked root can't make a referenced file look unknown."""
    known: set = set()
    for _rec_id, info in (variants or {}).items():
        text = (info or {}).get("file_path")
        if not text:
            continue
        try:
            known.add(str(Path(text).resolve()))
        except OSError:
            known.add(str(text))
    return known


def purge_orphan_variants(airtable, out_root: str | None,
                          orphan_age_days: float = DEFAULT_ORPHAN_AGE_DAYS,
                          dry_run: bool = True, logger=None, now: float | None = None,
                          max_delete_fraction: float = DEFAULT_ORPHAN_MAX_FRACTION) -> PurgeReport:
    """Delete spoofed files in `out_root` that **no** Spoof Variant row mentions.

    The status-driven sweep can only see files Airtable already knows about. A
    variant whose row was never created is invisible to it forever:
    `spoof_pipeline` finalizes the `.mp4` first and then calls
    `create_spoof_variant`, ignoring the result, so a failed write leaves a file
    nothing will ever reclaim.

    This is the only sweep that deletes without a positive `Used`, so it is
    hedged three ways, all required:

    1. The reference set is built from **every row at every status** -- a Ready
       or Pending variant is referenced and therefore never an orphan.
    2. The sweep **aborts entirely** if the listing raised or came back empty.
       One API blip would otherwise make every file look unreferenced.
    3. A **truncated** listing is caught by `max_delete_fraction`: if more than
       that share of the files on disk look orphaned, the listing is treated as
       untrustworthy and nothing is deleted.

    On top of that the window is `orphan_age_days` (default 7), not the 2-day
    variant window, so a file written seconds before its row lands is never in
    scope. Callers keep `dry_run=True` unless the operator opted in explicitly.
    """
    report = PurgeReport(dry_run=dry_run)
    if not out_root:
        return report
    root = Path(out_root)
    if not root.is_dir():
        return report

    try:
        variants = airtable.variants_by_id()
    except Exception as exc:
        report.aborted = "Airtable listing failed"
        report.errors.append(("<airtable>", str(exc)))
        if logger:
            logger.warning("retention (orphans): aborted, could not read Spoof Variants: %s", exc)
        return report

    if not variants:
        # Guard 2. Never treat "Airtable told me about nothing" as "nothing is
        # referenced" -- that reading empties the whole spoofed folder.
        report.aborted = "Spoof Variants listing was empty"
        if logger:
            logger.warning("retention (orphans): aborted, Spoof Variants listing came back "
                           "empty -- refusing to treat every file as unreferenced")
        return report

    known = _known_variant_paths(variants)

    candidates: list = []
    on_disk = 0
    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.suffix.lower() not in VIDEO_EXTS:
            continue
        on_disk += 1
        try:
            resolved = str(item.resolve())
        except OSError:
            resolved = str(item)
        if resolved in known or str(item) in known:
            report.kept += 1
            continue
        if is_older_than(item, orphan_age_days, now):
            candidates.append(item)
        else:
            report.kept += 1              # too new: its row may still be landing

    if candidates and on_disk and (len(candidates) / on_disk) > max_delete_fraction:
        # Guard 3. A partial listing looks exactly like this.
        report.aborted = (f"{len(candidates)}/{on_disk} files looked unreferenced "
                          f"(> {max_delete_fraction:.0%}); listing not trusted")
        report.kept += len(candidates)
        if logger:
            logger.warning("retention (orphans): aborted, %s", report.aborted)
        return report

    for item in candidates:
        _delete(item, report, dry_run)

    if logger:
        logger.info("retention (orphan variants, older than %s day(s)): %s",
                    orphan_age_days, report.summary())
    return report


@dataclass
class StrandedReport:
    """Ready variants that will never drain because their target is parked."""
    files: int = 0
    bytes: int = 0
    profiles: set = field(default_factory=set)
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"unavailable ({self.error})"
        return (f"{self.files} Ready variant(s), {self.bytes / 1_048_576:.1f} MB, "
                f"across {len(self.profiles)} parked profile(s)")


def report_stranded_ready(airtable, logger=None) -> StrandedReport:
    """Count -- and never delete -- Ready variants whose target profile is parked.

    The queue drains Ready oldest-first, so these are not slow, they are stuck:
    a parked profile is skipped by the planner, and its media sits forever. They
    are deliberately left on disk. Re-activating a profile is a single Airtable
    edit, and if the media had been purged the post would silently have nothing
    to send -- the one failure mode this whole module exists to avoid.

    Reporting them makes the leak visible so a person can decide, which is the
    only safe form this can take.
    """
    out = StrandedReport()
    try:
        variants = airtable.list_ready_variants()
        profiles = airtable.posting_profiles()
    except Exception as exc:
        out.error = str(exc)
        if logger:
            logger.warning("retention: could not measure stranded Ready variants: %s", exc)
        return out

    # `posting_profiles` already reads an empty Status as Active, matching the
    # planner, so anything not Active here is genuinely parked.
    parked = {p.get("record_id") for p in (profiles or [])
              if p.get("status") != at.STATUS_SELECT_ACTIVE}
    if not parked:
        return out

    for variant in variants or []:
        profile_id = variant.get("profile_id")
        if not profile_id or profile_id not in parked:
            continue
        path_text = variant.get("file_path")
        if not path_text:
            continue
        path = Path(path_text)
        if not path.is_file():
            continue
        out.files += 1
        out.profiles.add(profile_id)
        try:
            out.bytes += path.stat().st_size
        except OSError:
            pass

    if logger and out.files:
        logger.info("retention: %s -- left in place on purpose (re-activating a "
                    "profile must not find its media gone)", out.summary())
    return out


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
