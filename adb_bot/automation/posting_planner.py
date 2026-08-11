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
from adb_bot.automation.caption_probe import target_key as caption_probe_key


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
    # Which Instagram account on the phone to post as. None = whoever is signed
    # in, which is every single-account phone. When set, the flow proves the
    # account switcher is on this handle before it touches the composer.
    target_handle: str | None = None
    account_slot: str | None = None
    # Set when the caption probe withheld a caption this row actually carries.
    # The text is dropped from `caption` (so the flow cannot type it) but kept
    # here, so the write-back can say which caption was skipped rather than
    # leaving a bare post that looks like a row someone forgot to fill in.
    caption_withheld: str | None = None


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
    caption_probe=None,
) -> PostingPlan:
    """Build the list of due posts.

    - `accounts_by_id`: account record_id -> Airtable fields dict
    - `profiles_by_recid`: profile record_id -> {'launch_id', 'name'} (profile_launch_map)
    - `variants_by_id`: variant record_id -> {'file_path', 'status'}
    - `captions_by_id`: caption record_id -> text
    - `selected_launch_ids`: restrict to these launch ids (None = all)
    - `caption_probe`: optional `CaptionProbe`. When an account owes bare
      attempts (three caption-bearing failures in a row), the caption is dropped
      from the item here rather than at the flow, so exactly one place decides
      it and the plan itself shows what will be sent. None disables the
      experiment entirely and posts behave as they always did.
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

        info = (profiles_by_recid.get(profile_id) or {}) if profile_id else {}

        # --- profile health guards ---
        # Re-checked here and not only where rows are created, because a phone
        # can be parked *after* its row went Pending: the queue filter cannot
        # reach a row that already exists, so without this the phone keeps its
        # outstanding slots and spends a launch and a boot on every one. That is
        # what let 22 flagged profiles keep posting through 2026-08-06 -- Laila 3
        # burned 17 launches for 0 posts in a day, and Viktoria 3 was launched
        # while flagged `Banned / Blocked`.
        #
        # A property of the *phone*, not of one account on it: an Instagram
        # challenge is against the device, so dropping the profile here drops
        # every account that posts from it -- both accounts of a two-account
        # phone, deliberately.
        if info.get("needs_human"):
            plan.skipped.append(SkippedPost(account_name, "profile needs a human check"))
            continue
        profile_status = info.get("status")
        if profile_status is not None and profile_status != at.STATUS_SELECT_ACTIVE:
            plan.skipped.append(SkippedPost(account_name, f"profile status {profile_status}"))
            continue

        # --- the hand-off ---
        # A phone that came off the warm-up is not a posting target until a
        # person has given it a bio, a picture and one post made by hand. An
        # account whose first ever post is an automated reel is the one
        # Instagram acts on, and the warm-up exists precisely so that does not
        # happen -- posting the moment day 4 completes would throw that away on
        # the last step.
        #
        # Keyed on `Warm-up Started`, so it applies to exactly the cohort that
        # went through the warm-up and to nobody else: every profile posting
        # today predates it and has no start date, so this cannot park a
        # working account. `Status = Inactive` remains the way to hold back
        # anything else.
        outstanding = info.get("handoff_outstanding")
        if info.get("warmup_started") and outstanding:
            plan.skipped.append(SkippedPost(
                account_name, f"waiting on a person: {', '.join(outstanding)}"))
            continue

        launch_id = info.get("launch_id")
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

        # A two-account phone posts as whichever account the row names, and the
        # name follows suit: two rows for one profile would otherwise be
        # indistinguishable in the log and in the Run Log.
        target_handle = at._handle(fields.get(at.F_PQ_TARGET_HANDLE))
        if target_handle and direct_profile_id and not account_id:
            account_name = target_handle

        # --- the caption experiment ---
        # An account that has just failed three caption-bearing posts in a row
        # owes two attempts with the text withheld, to show whether the caption
        # was the cause. Decided here, on the same identity the write-back will
        # use, so the plan and the result cannot disagree about which account
        # was being probed.
        caption_withheld = None
        if caption and caption_probe is not None:
            probe_key = caption_probe_key(
                account_id=account_id, target_handle=target_handle, launch_id=launch_id)
            if caption_probe.should_drop_caption(probe_key):
                caption_withheld, caption = caption, None

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
            target_handle=target_handle,
            account_slot=at._select_name(fields.get(at.F_PQ_ACCOUNT_SLOT)),
            caption_withheld=caption_withheld,
        ))

    return plan
