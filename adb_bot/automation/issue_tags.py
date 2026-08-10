"""Mirror the Airtable "this profile needs a person" flag onto a MultiLogin tag.

Airtable already knows which profiles are in trouble: `retry_runner` and
`stale_profiles` tick `Needs Human Check` and stamp `Issue Reason` / `Flagged
At`, and a person unticks the box when they have dealt with it. But the person
doing the dealing works in the MultiLogin workspace, where none of that is
visible -- they see 170 phones and no way to tell which twenty are waiting on
them. This puts the flag where they are looking.

A **reconciler**, in the shape of `warmup_state`: it reads both systems and
makes MultiLogin agree with Airtable, rather than being a callback on the
moment of flagging. That is what makes it idempotent, two-way (the tag goes on
when the box is ticked and comes off when it is cleared), and self-healing after
a tick that died halfway.

Three decisions worth writing down, because each one is a way this could go
wrong quietly:

**The trigger is `Needs Human Check`, and nothing else.** Not `Status=Inactive`:
on the live base 39 profiles are Inactive and 26 of them carry no flag at all --
they are blanks, `Link` rows and retired numbers a person parked deliberately.
Tagging those would be 26 wrong tags on the first apply. Not "`Flagged At` is
non-blank" either: that is the un-flagged-but-not-yet-cleaned window
(`profiles_awaiting_recovery`), and ORing it in would hold the tag for up to
fifteen minutes after somebody unticked the box -- the exact opposite of what
"the flag is gone, so the tag should go" means. The checkbox is what the rest of
the system keys on too (`posting_planner` skips on it before it reads Status;
the dashboard's flagged list is literally `{Needs Human Check}=1`).

**`Issue` is not the bot's tag, and this module does not seize it.** It is a
real tag in the workspace with 33 hand-applied uses, most of them on parked
profiles that were never flagged in Airtable. `warmup_state.owned_tags()` gets
to say "I own this name, so I may remove it anywhere" because nobody else writes
`Warmup Day 2 Done`. Here that reasoning would delete 26 of somebody's tags on
the first `--apply`. So ownership is per *profile*, not per name: a small ledger
(`issue_tag_state.json`, alongside `second_account_watch.json`) records the
profiles this pass put the tag on, and removal is limited to those. A hand-
applied `Issue` is never touched. If the ledger is lost, the bot's own tags
become un-removable by the bot until the profile is flagged and cleared again --
which is the cheap failure, and the reason the file is small and boring.

**Removal is doubly fenced.** `OWNED_TAGS` is checked first (so nothing outside
it can ever reach an unassign call, whatever the ledger says), and the ledger is
checked second. `Created` selects the warm-up population and the `Warmup Day N`
tags are `warmup_state`'s; stripping either would silently drop profiles out of
the warm-up. Both fences are tested.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

# The tag as it already exists in the workspace: id
# de2aef69-b8c1-4547-90a4-0875b1d85361, purple, 33 uses. Matched
# case-insensitively on assignment (`tag_ids_by_name` lower-cases), so the
# colour below is only ever consulted if the tag had to be created -- which on
# this workspace it does not.
ISSUE_TAG = "Issue"
ISSUE_TAG_COLOR = "purple"

# Every tag this module may write, and therefore the *only* tag it may remove.
# Deliberately a set of one. Nothing outside it can reach an unassign call: see
# `tag_changes`, which intersects the profile's held tags with this before it
# considers the ledger at all.
OWNED_TAGS = (ISSUE_TAG,)

STATE_FILENAME = "issue_tag_state.json"
STATE_VERSION = 1

# A MultiLogin outage shows up as every profile's tag call failing in turn.
# Rather than walk the whole fleet making failing HTTP calls, give up after this
# many consecutive failures and report it. Not a threshold on *total* errors: a
# handful of dead profile ids scattered through a good sweep should not stop it.
MAX_CONSECUTIVE_FAILURES = 5


def owned_tags() -> list:
    """Every tag this module writes -- and therefore every tag it may remove."""
    return list(OWNED_TAGS)


# ----------------------------------------------------------------- the ledger


def _state_path(app_dir=None) -> Path:
    return Path(app_dir or get_app_data_dir()) / STATE_FILENAME


def load_ledger(app_dir=None) -> dict:
    """``{MLX API ID: {"name": ..., "at": ...}}`` -- the profiles this pass tagged.

    A missing or unreadable file reads as empty, which is the safe direction:
    the bot then owns nothing and can only ever add.
    """
    try:
        raw = json.loads(_state_path(app_dir).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    tagged = raw.get("tagged")
    return dict(tagged) if isinstance(tagged, dict) else {}


def save_ledger(tagged: dict, app_dir=None) -> bool:
    try:
        path = _state_path(app_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"version": STATE_VERSION, "tagged": dict(tagged)},
            indent=2, sort_keys=True), encoding="utf-8")
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ planning


@dataclass
class ProfileIssue:
    """One Airtable row, and what MultiLogin currently says about it."""
    record_id: str
    name: str
    launch_id: str
    flagged: bool
    reason: str = ""
    current_tags: tuple = ()
    # False when the row's MLX API ID is not in the fleet inventory -- the
    # profile was deleted in MultiLogin but the Airtable row outlived it. Kept
    # as a field rather than inferred at call time so the report can count them.
    in_mlx: bool = True

    def tag_changes(self, owned_by_bot: bool) -> tuple:
        """``(add, remove)`` tag names, both empty when MultiLogin already agrees.

        `owned_by_bot` is the ledger's answer for this profile: True only if a
        previous pass of *this* module put the tag on. A hand-applied `Issue`
        arrives here with `owned_by_bot=False` and comes out untouched.
        """
        held = {str(t).strip() for t in (self.current_tags or ()) if str(t).strip()}
        has_issue = any(t.lower() == ISSUE_TAG.lower() for t in held)

        if self.flagged:
            return ([] if has_issue else [ISSUE_TAG]), []

        # Not flagged. The only tag that may come off is one this module owns
        # by name *and* put there itself. Both fences, in that order.
        removable = [t for t in held
                     if t.lower() in {o.lower() for o in OWNED_TAGS}]
        return [], (removable if (has_issue and owned_by_bot) else [])


@dataclass
class IssueTagReport:
    checked: int = 0
    tagged: int = 0            # tag added
    untagged: int = 0          # tag removed
    unchanged: int = 0
    no_launch_id: int = 0      # Airtable row with no MLX API ID
    missing_in_mlx: int = 0    # API ID that the workspace no longer knows
    skipped_not_ours: int = 0  # unflagged, carries a hand-applied `Issue`
    errors: list = dc_field(default_factory=list)
    changes: list = dc_field(default_factory=list)
    stale: list = dc_field(default_factory=list)

    def summary(self) -> str:
        return (f"checked={self.checked} tagged={self.tagged} "
                f"untagged={self.untagged} unchanged={self.unchanged} "
                f"no-id={self.no_launch_id} missing-in-mlx={self.missing_in_mlx} "
                f"hand-tagged={self.skipped_not_ours} errors={len(self.errors)}")


def build_states(profiles, tags_by_api_id: dict | None = None) -> list:
    """`airtable.posting_profiles()` output -> one :class:`ProfileIssue` each.

    Joined to MultiLogin on `MLX API ID` -- the 18-digit launch key, which is
    what the tag endpoints take as `profile_id`. Never the human serial
    (`MultiLogin Profile ID`), which those endpoints reject.

    `reason` is read if the row carries one (`profile_overview` rows do) and left
    blank otherwise; it only ever decorates a log line, so `posting_profiles`
    staying lean is worth more than having it.
    """
    inventory = tags_by_api_id or {}
    states = []
    for row in profiles or []:
        launch_id = str(row.get("launch_id") or "").strip()
        states.append(ProfileIssue(
            record_id=str(row.get("record_id") or ""),
            name=str(row.get("name") or "").strip() or str(row.get("record_id") or ""),
            launch_id=launch_id,
            flagged=bool(row.get("needs_human")),
            reason=str(row.get("reason") or "").strip(),
            current_tags=tuple(inventory.get(launch_id) or ()),
            in_mlx=bool(launch_id) and launch_id in inventory,
        ))
    return states


# -------------------------------------------------------------------- reconcile


def sync_issue_tags(airtable, tag_client=None, mlx_items=None, dry_run: bool = True,
                    logger=None, app_dir=None, adopt_existing: bool = False,
                    now=None, profiles=None) -> IssueTagReport:
    """Make the MultiLogin `Issue` tag agree with Airtable's flag. Idempotent.

    `tag_client` is optional and `mlx_items` may be empty, for the same reason
    `warmup_state` allows it: MultiLogin being down should cost this pass, not
    the caller. Both cases return an empty report with the reason in `errors`,
    having made no calls.

    `adopt_existing` is the escape hatch for the 33 hand-applied `Issue` tags:
    with it on, every profile currently carrying the tag counts as bot-owned and
    an unflagged one will therefore have it **stripped**. It is off by default
    and should only ever be turned on by a person who has decided those tags are
    stale, because the information is not recoverable from Airtable.
    """
    report = IssueTagReport()
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    if profiles is None:
        try:
            profiles = airtable.posting_profiles()
        except Exception as exc:
            report.errors.append(f"Airtable profiles: {type(exc).__name__}: {exc}")
            _log(logger, report)
            return report

    if tag_client is None:
        report.errors.append("no MultiLogin tag client; nothing read or written")
        _log(logger, report)
        return report
    if not mlx_items:
        # An empty inventory is indistinguishable from "MultiLogin answered but
        # is not reachable", and reconciling against it would read every profile
        # as carrying no tags. Refuse rather than guess.
        report.errors.append("empty MultiLogin inventory; skipping the tag pass")
        _log(logger, report)
        return report

    from adb_bot.automation.warmup_state import tags_by_launch_id

    states = build_states(profiles, tags_by_launch_id(mlx_items))
    ledger = load_ledger(app_dir)

    # Resolved once per sweep, not once per profile -- one name, ~150 profiles.
    # `ensure_tag` is a read (`tag/search`) that only writes if the workspace has
    # not got the tag; under dry-run it is skipped entirely so a plan-only run
    # cannot create anything.
    tag_id = None
    if not dry_run:
        try:
            tag_id = tag_client.ensure_tag(ISSUE_TAG, ISSUE_TAG_COLOR)
        except Exception as exc:
            report.errors.append(f"resolving tag {ISSUE_TAG!r}: {type(exc).__name__}: {exc}")
            _log(logger, report)
            return report
        if not tag_id:
            report.errors.append(f"MultiLogin returned no id for tag {ISSUE_TAG!r}")
            _log(logger, report)
            return report

    ledger_dirty = False
    consecutive_failures = 0

    for state in states:
        report.checked += 1

        if not state.launch_id:
            # Zero rows on the live base today, but a row can be created before
            # its profile is linked. Skip and count; never fall back to the
            # serial, which the tag endpoints do not accept.
            report.no_launch_id += 1
            continue
        if not state.in_mlx:
            # The Airtable row points at a profile MultiLogin no longer has.
            # Attempting it would 400, and `tags._post` raises -- one stale id
            # would abort the rest of the sweep. Worth a person's attention when
            # the row is also flagged, so it is reported by name.
            report.missing_in_mlx += 1
            if state.flagged:
                report.stale.append(f"{state.name} [{state.launch_id}] flagged, not in MultiLogin")
            continue

        owned_by_bot = adopt_existing or (state.launch_id in ledger)
        add, remove = state.tag_changes(owned_by_bot)

        if not add and not remove:
            report.unchanged += 1
            # An unflagged profile wearing somebody's hand-applied `Issue` lands
            # here. Counted separately so "unchanged" does not quietly hide the
            # population this module is deliberately not managing.
            if not state.flagged and any(
                    t.lower() == ISSUE_TAG.lower() for t in state.current_tags):
                report.skipped_not_ours += 1
            continue

        verb = "tag" if add else "untag"
        report.changes.append(
            f"{verb} {state.name} [{state.launch_id}]"
            + (f" -- {state.reason}" if (add and state.reason) else ""))

        if dry_run:
            report.tagged += 1 if add else 0
            report.untagged += 1 if remove else 0
            continue

        try:
            # One tag id per call, well inside `tags.MAX_TAGS_PER_CALL`; the
            # client chunks anyway.
            if add:
                tag_client.assign(state.launch_id, [tag_id])
                report.tagged += 1
                ledger[state.launch_id] = {"name": state.name, "at": stamp,
                                           "reason": state.reason}
                ledger_dirty = True
            else:
                tag_client.unassign(state.launch_id, [tag_id])
                report.untagged += 1
                if ledger.pop(state.launch_id, None) is not None:
                    ledger_dirty = True
            consecutive_failures = 0
        except Exception as exc:
            report.errors.append(f"{state.name}: MLX {type(exc).__name__}: {exc}")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                report.errors.append(
                    f"{consecutive_failures} MultiLogin failures in a row; stopping this sweep")
                break

    if ledger_dirty and not save_ledger(ledger, app_dir):
        # Non-fatal, but it means the tags just written are not yet owned and a
        # later clear will leave them on. Say so loudly.
        report.errors.append(f"could not write {STATE_FILENAME}; "
                             "tags written this pass are not recorded as ours")

    _log(logger, report)
    return report


def _log(logger, report: IssueTagReport) -> None:
    if not logger:
        return
    logger.info("issue tags: %s", report.summary())
    for line in report.changes[:20]:
        logger.info("  %s", line)
    for line in report.stale[:10]:
        logger.warning("  %s", line)
    for line in report.errors[:10]:
        logger.warning("  %s", line)
