"""Turn the Airtable Posting Queue into a list of posts to run (checklist #1).

Airtable fills the Posting Queue on its own 5x/day schedule; the bot's job is to
consume the rows that are **Pending and due** (`Scheduled DateTime <= now`),
resolve each row's linked Target Account -> Profile (launch key), Spoof Variant
(video file) and Caption (text), apply the account health guards, and hand the
runner a flat list of postable items.

Pure planning: no launching, no device work, no Airtable writes -- so it's easy
to test. It takes pre-fetched lookup dicts (the same shape the AirtableClient
returns) rather than calling the client itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime

from adb_bot.clients import airtable as at


@dataclass
class PostingItem:
    queue_id: str
    account_id: str
    account_name: str
    launch_id: str            # 18-digit MLX API ID -- the launch/ADB key
    video_path: str
    caption: str | None
    variant_id: str | None
    scheduled: str | None
    retry_count: int = 0
    # Which Instagram account on that phone to post as. Empty for the ~115
    # single-account phones, which post as whoever is signed in.
    ig_handle: str = ""


@dataclass
class SkippedPost:
    name: str
    reason: str


@dataclass
class PostingPlan:
    to_post: list = dc_field(default_factory=list)
    skipped: list = dc_field(default_factory=list)

    def summary(self) -> str:
        return f"due={len(self.to_post)} skipped={len(self.skipped)}"


def _parse_dt(value):
    """Parse an Airtable ISO dateTime (usually UTC 'Z'); None if unparseable."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _is_due(scheduled_raw, now: datetime) -> bool:
    """True if the row is due (no schedule = due immediately)."""
    scheduled = _parse_dt(scheduled_raw)
    if scheduled is None:
        return True
    # Compare in a tz-consistent way: if the row carries a tz and `now` doesn't
    # (or vice-versa), fall back to naive comparison on the wall clock.
    if scheduled.tzinfo is not None and now.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=None)
    elif scheduled.tzinfo is None and now.tzinfo is not None:
        scheduled = scheduled.replace(tzinfo=now.tzinfo)
    return scheduled <= now


def _first_link(fields: dict, key: str):
    links = fields.get(key) or []
    return links[0] if links else None


def plan_posting_queue(
    queue_rows: list,
    accounts_by_id: dict,
    profiles_by_recid: dict,
    variants_by_id: dict,
    captions_by_id: dict,
    now: datetime | None = None,
    selected_launch_ids=None,
) -> PostingPlan:
    """Build the list of due posts.

    - `accounts_by_id`: account record_id -> Airtable fields dict
    - `profiles_by_recid`: profile record_id -> {'launch_id', 'name'} (profile_launch_map)
    - `variants_by_id`: variant record_id -> {'file_path', 'status'}
    - `captions_by_id`: caption record_id -> text
    - `selected_launch_ids`: restrict to these launch ids (None = all)
    """
    now = now or datetime.now()
    plan = PostingPlan()

    for row in queue_rows:
        fields = row.get("fields", {}) or {}
        queue_id = row.get("id")
        name = str(fields.get(at.F_PQ_NAME) or queue_id or "").strip()

        # Only Pending + due rows. (The client already filters Pending; we still
        # guard here so the planner is correct on any input.)
        if at._select_name(fields.get(at.F_PQ_POST_STATUS)) not in (None, at.POST_STATUS_PENDING):
            continue
        if not _is_due(fields.get(at.F_PQ_SCHEDULED), now):
            continue

        account_id = _first_link(fields, at.F_PQ_TARGET_ACCOUNT)
        direct_profile_id = _first_link(fields, at.F_PQ_TARGET_PROFILE)

        if account_id and account_id in accounts_by_id:
            account = accounts_by_id[account_id]
            account_name = str(account.get(at.F_ACC_NAME) or account_id).strip()

            # --- account health guards (checklist: skip flagged accounts) ---
            if bool(account.get(at.F_ACC_NEEDS_VERIFICATION)):
                plan.skipped.append(SkippedPost(account_name, "needs human verification"))
                continue
            if at._select_name(account.get(at.F_ACC_AUTOMATION_MODE)) == at.MODE_PAUSED:
                plan.skipped.append(SkippedPost(account_name, "automation mode paused"))
                continue
            stage = at._select_name(account.get(at.F_ACC_LIFECYCLE_STAGE))
            if stage in (at.STAGE_PAUSED, at.STAGE_BANNED):
                plan.skipped.append(SkippedPost(account_name, f"lifecycle stage {stage}"))
                continue

            # --- resolve the launch key (Account -> Profile -> MLX API ID) ---
            profile_id = _first_link(account, at.F_ACC_PROFILE)
        elif direct_profile_id:
            # Profile-driven row: no Accounts row exists, so there are no account
            # health guards to apply. The profile itself is the target, and its
            # name stands in for the handle in logs and Airtable write-back.
            account_id = None
            profile_id = direct_profile_id
            account_name = str((profiles_by_recid.get(direct_profile_id) or {}).get("name")
                               or direct_profile_id).strip()
        else:
            plan.skipped.append(SkippedPost(name, "no linked Target Account or Target Profile"))
            continue

        launch_id = None
        profile_info: dict = {}
        if profile_id:
            profile_info = profiles_by_recid.get(profile_id) or {}
            launch_id = profile_info.get("launch_id")

        # --- the phone-level guard -------------------------------------------
        # A profile the bot flagged for a person does not post, whatever its rows
        # say. Checked here as well as in `profile_targets_by_model` because that
        # only stops NEW rows being created: rows queued before the flag landed
        # are already Pending and due, and without this they keep posting from a
        # phone somebody has been told to go and look at.
        #
        # It deliberately keys off the profile rather than the row's handle. When
        # Instagram challenges an account it is reacting to the device, so the
        # other account on a two-account phone is in the same trouble -- posting
        # from it while its twin sits flagged is how one warning becomes two.
        if profile_info.get("needs_human"):
            reason = profile_info.get("issue_reason") or "flagged for a human"
            plan.skipped.append(SkippedPost(
                account_name,
                f"profile needs a human check ({reason}); not posting until it is cleared"))
            continue

        if not launch_id:
            plan.skipped.append(SkippedPost(account_name, "no MLX API ID on linked profile"))
            continue

        if selected_launch_ids and launch_id not in selected_launch_ids:
            continue

        # --- resolve the video (Spoof Variant -> file path) ---
        variant_id = _first_link(fields, at.F_PQ_SPOOF_VARIANT)
        variant = variants_by_id.get(variant_id) if variant_id else None
        video_path = (variant or {}).get("file_path")
        if not video_path:
            plan.skipped.append(SkippedPost(account_name, "no Spoof Variant video path"))
            continue

        # --- resolve the caption (optional) ---
        caption_id = _first_link(fields, at.F_PQ_CAPTION)
        caption = captions_by_id.get(caption_id) if caption_id else None

        try:
            retry = int(fields.get(at.F_PQ_RETRY_COUNT) or 0)
        except (TypeError, ValueError):
            retry = 0

        ig_handle = str(fields.get(at.F_PQ_IG_HANDLE) or "").strip().lstrip("@").strip().lower()
        # A two-account row is only meaningful with the handle on it; the name
        # says which account a person meant, so carry it into the logs too.
        if ig_handle:
            account_name = f"{account_name} [{ig_handle}]"

        plan.to_post.append(PostingItem(
            queue_id=queue_id,
            account_id=account_id,
            account_name=account_name,
            launch_id=launch_id,
            video_path=video_path,
            caption=caption,
            variant_id=variant_id,
            scheduled=fields.get(at.F_PQ_SCHEDULED),
            retry_count=retry,
            ig_handle=ig_handle,
        ))

    return plan
