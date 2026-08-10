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
  to run unless two independent Spoof Variants listings agree exactly -- the
  only check here that actually detects a short listing.
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

# Last-ditch magnitude backstop for the orphan sweep. Read this before arming
# the sweep, because it does far less than its name suggests:
#
# It catches a listing that came back *severely* truncated -- one short enough
# that most of the folder suddenly looks unreferenced. It does NOT catch a
# merely truncated one. Worked example with the real folder size: 402 files on
# disk and a listing that returns ~70% of the rows yields ~120 candidates, which
# is 30% of the folder -- comfortably under this 50% guard. Armed, that pass
# deletes ~120 files, and because the missing rows are missing at every status,
# `Ready` variants backing already-scheduled posts are among them. That is
# precisely the failure this module exists to prevent, and this constant would
# not have stopped it.
#
# The check that actually detects a short listing is the two-listing comparison
# in `purge_orphan_variants`: any disagreement between two independent reads
# aborts the sweep. This fraction is only a backstop for the case where both
# reads are short in the same way.
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
    listing: dict | None = None                      # the Spoof Variants read this sweep used

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
    except (OSError, ValueError):
        # ValueError: `stat` on a path with an embedded NUL. Unreadable age is
        # never "old enough to delete".
        return False


def _delete(path: Path, report: PurgeReport, dry_run: bool) -> None:
    try:
        size = path.stat().st_size
    except (OSError, ValueError):
        size = 0
    if dry_run:
        report.deleted.append(str(path))
        report.freed_bytes += size
        return
    try:
        path.unlink()
        report.deleted.append(str(path))
        report.freed_bytes += size
    except (OSError, ValueError) as exc:
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

    The listing this sweep read is kept on `report.listing` so the orphan sweep
    can use it as one half of its completeness check instead of paying for a
    third read of the table.
    """
    report = PurgeReport(dry_run=dry_run)
    try:
        variants = airtable.variants_by_id()
    except Exception as exc:
        report.errors.append(("<airtable>", str(exc)))
        if logger:
            logger.warning("retention: could not read Spoof Variants: %s", exc)
        return report

    report.listing = variants or {}
    for _rec_id, info in (variants or {}).items():
        path_text = (info or {}).get("file_path")
        status = (info or {}).get("status")
        if not path_text:
            continue
        if status != at.SV_STATUS_USED:
            report.kept += 1          # still needed by a pending post
            continue
        try:
            path = Path(path_text)
        except (TypeError, ValueError) as exc:
            # A File Path cell that isn't a usable path (embedded NUL, a list
            # from a mis-typed field). Record it and keep sweeping; one bad row
            # must not stop the sweep that frees the disk.
            report.errors.append((str(path_text), f"unusable File Path: {exc}"))
            continue
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
        except (OSError, ValueError, TypeError):
            # ValueError: an embedded NUL. TypeError: the cell wasn't a string.
            # Either way keep the raw text in the reference set -- an unreadable
            # row must make files *more* protected, never less.
            known.add(str(text))
    return known


def purge_orphan_variants(airtable, out_root: str | None,
                          orphan_age_days: float = DEFAULT_ORPHAN_AGE_DAYS,
                          dry_run: bool = True, logger=None, now: float | None = None,
                          max_delete_fraction: float = DEFAULT_ORPHAN_MAX_FRACTION,
                          variants=None) -> PurgeReport:
    """Delete spoofed files in `out_root` that **no** Spoof Variant row mentions.

    The status-driven sweep can only see files Airtable already knows about. A
    variant whose row was never created is invisible to it forever:
    `spoof_pipeline` finalizes the `.mp4` first and then calls
    `create_spoof_variant`, ignoring the result, so a failed write leaves a file
    nothing will ever reclaim.

    This is the only sweep that deletes without a positive `Used`, so it is
    hedged four ways, all required:

    1. The reference set is built from **every row at every status** -- a Ready
       or Pending variant is referenced and therefore never an orphan.
    2. The sweep **aborts entirely** if either listing raised or came back
       empty. One API blip would otherwise make every file look unreferenced.
    3. A short listing is caught by **comparing two independent listings**: the
       one `purge_used_variants` already read (passed in as `variants`) and a
       second one taken here. Any difference in record ids at all -- a row in
       one and not the other, in either direction -- aborts the sweep, because a
       truncated read is indistinguishable from rows legitimately appearing
       while the pipeline runs. This is the only check that actually detects a
       partial listing. The reference set is the **union** of the two, so a file
       either read knows about is protected even on the paths that continue.
    4. `max_delete_fraction` is a magnitude backstop, and a weak one -- it only
       fires on a *severely* short listing. See the note on
       `DEFAULT_ORPHAN_MAX_FRACTION`: a listing at ~70% completeness against 402
       files produces ~30% candidates and sails straight under it, deleting
       Ready variants that scheduled posts still need. Do not read a pass that
       cleared this guard as a pass that verified the listing; guard 3 is what
       does that.

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

    def _list():
        return airtable.variants_by_id() or {}

    try:
        first = _list() if variants is None else (variants or {})
        second = _list()
    except Exception as exc:
        report.aborted = "Airtable listing failed"
        report.errors.append(("<airtable>", str(exc)))
        if logger:
            logger.warning("retention (orphans): aborted, could not read Spoof Variants: %s", exc)
        return report

    if not first or not second:
        # Guard 2. Never treat "Airtable told me about nothing" as "nothing is
        # referenced" -- that reading empties the whole spoofed folder.
        report.aborted = "Spoof Variants listing was empty"
        if logger:
            logger.warning("retention (orphans): aborted, Spoof Variants listing came back "
                           "empty -- refusing to treat every file as unreferenced")
        return report

    # Guard 3, the real completeness check. Two reads of a table that is not
    # being written to must return the same record ids; if they do not, at least
    # one of them is short (or the table moved under us, which is the same
    # problem for this sweep's purposes). Either way the answer is "do not
    # delete tonight", which costs nothing -- these files are already 7+ days
    # old and the sweep runs daily.
    only_first = set(first) - set(second)
    only_second = set(second) - set(first)
    if only_first or only_second:
        report.aborted = (f"two Spoof Variants listings disagreed "
                          f"({len(first)} vs {len(second)} rows; "
                          f"{len(only_first)} only in the first, "
                          f"{len(only_second)} only in the second); listing not trusted")
        report.listing = {**second, **first}
        if logger:
            logger.warning("retention (orphans): aborted, %s", report.aborted)
        return report

    report.listing = second
    # Union, not either one: a path any read mentions is referenced.
    known = _known_variant_paths({**second, **first})

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
        # Guard 4, the magnitude backstop. Only a severely short listing gets
        # this far past guard 3, and only a severely short listing trips it.
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
        try:
            path = Path(path_text)
        except (TypeError, ValueError):
            continue          # unusable File Path cell; it counts as nothing
        if not path.is_file():
            continue
        out.files += 1
        out.profiles.add(profile_id)
        try:
            out.bytes += path.stat().st_size
        except (OSError, ValueError):
            pass

    if logger and out.files:
        logger.info("retention: %s -- left in place on purpose (re-activating a "
                    "profile must not find its media gone)", out.summary())
    return out


def prune_empty_dirs(root: str | None, logger=None, dry_run: bool = True,
                     label: str | None = None) -> int:
    """Remove directories left empty after a purge. Returns how many were (or
    would be) removed. The root itself is kept.

    `label` names the tree in the log line, and is not optional in practice:
    this runs over more than one root per cleanup, and an unlabelled
    "N empty folder(s) found" is unreadable in the journal -- the count from the
    Drive scratch tree under /tmp is indistinguishable from the count from the
    spoofed output tree, which is the one an operator assumes they are reading.
    The root itself is logged too, so the line is unambiguous either way.
    """
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
        logger.info("retention (%s): %s empty folder(s) %s under %s",
                    label or "empty dirs", removed,
                    "found" if dry_run else "removed", base)
    return removed
