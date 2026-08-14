"""Mirror the Airtable "this profile needs a person" flag onto a MultiLogin tag.

Airtable already knows which profiles are in trouble: `retry_runner` and
`stale_profiles` tick `Needs Human Check` and stamp `Issue Reason` / `Flagged
At`, and a person unticks the box when they have dealt with it. But the person
doing the dealing works in the MultiLogin workspace, where none of that is
visible -- they see 170 phones and no way to tell which twenty are waiting on
them. This puts the flag where they are looking.

A **reconciler**, in the shape of `warmup_state`: it reads both systems and
makes them agree, rather than being a callback on the moment of flagging. That
is what makes it idempotent, two-way (the tag goes on when the box is ticked and
comes off when it is cleared), and self-healing after a tick that died halfway.

**Since 2026-08-11 it also carries the answer back.** Removing this pass's own
`Issue` tag in MultiLogin unticks `Needs Human Check` in Airtable, so a VA never
has to open Airtable to say they dealt with something -- they work in one
system, the one with the phones in it. `recovery_runner` then does what it has
always done with a cleared flag: sets Status back to Active and hands the
profile's dead queue rows back to the retry pass, within about fifteen minutes.

That direction is fenced by the same ledger as removal, and for the same reason:
only a tag *this module put on* means anything when it disappears. Of the ~69
`Issue` tags on this workspace, 40 are hand-applied and were never a bot flag;
somebody tidying one of those must not rewrite an Airtable row. And a flagged
profile that is missing from the inventory is not "untagged" -- it is unread, so
it is left alone. See `ProfileIssue.cleared_by_person`.

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

**The tag is never created, and this loop never installs itself.** Two rules
about writing to somebody else's system, both of which read as over-caution
until the day they do not:

* the id comes from a search, never from `ensure_tag` -- see
  `resolve_issue_tag_id`, where a soft `/tag/search` failure would otherwise
  mint a *second* `Issue` and strand the 33 real uses on the first;
* it is in `schedule_spec.MANUAL_ONLY_LOOPS`, so `install_units.sh` will not arm
  it. Everything else in the recommended set writes to systems this bot owns;
  this one writes to the workspace people work in, and the first unattended
  `--apply` of its ledger's life should not arrive as a side effect of an
  installer run somebody made for an unrelated reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

# The tag as it already exists in the workspace: id
# de2aef69-b8c1-4547-90a4-0875b1d85361, purple, 33 uses. Matched
# case-insensitively (`tag_ids_by_name` lower-cases). This module never creates
# it -- see `resolve_issue_tag_id` for why that is a rule and not an oversight.
ISSUE_TAG = "Issue"

# Every tag this module may write, and therefore the *only* tag it may remove.
# Nothing outside it can reach an unassign call: see `tag_changes`, which
# intersects the profile's held tags with this before it considers the ledger at
# all.
#
# SINGLE-VALUED, and not merely as it happens today. The sweep resolves ONE tag
# id up front and passes that one id to `assign`/`unassign`, so a second name
# here would be planned in `tag_changes` and then silently not carried out --
# the plan and the API call would disagree. Adding a name therefore means
# changing the sweep to resolve an id per name and to un/assign the ids the plan
# actually named; the guard below is what stops the constant being widened on
# its own. (Tested: `test_the_owned_set_is_single_valued_by_construction`.)
OWNED_TAGS = (ISSUE_TAG,)
if len(OWNED_TAGS) != 1:      # pragma: no cover - a guard on editing the line above
    raise RuntimeError(
        "issue_tags.OWNED_TAGS is single-valued by construction: the sweep resolves "
        "one tag id and un/assigns it. Make the sweep multi-tag before widening this.")

STATE_FILENAME = "issue_tag_state.json"
# 2: grew a `stale_seen` section next to `tagged`. A version-1 file still loads
# (the section reads as empty), which only costs one repeat of a stale warning.
STATE_VERSION = 2

# A MultiLogin outage shows up as every profile's tag call failing in turn.
# Rather than walk the whole fleet making failing HTTP calls, give up after this
# many consecutive failures and report it. Not a threshold on *total* errors: a
# handful of dead profile ids scattered through a good sweep should not stop it.
MAX_CONSECUTIVE_FAILURES = 5

# How often a flagged Airtable row whose MultiLogin profile no longer exists is
# worth a WARNING. It is a real condition needing a person (`Jasmin 9`,
# 628516863629918338, has been in this state for days) but it does not change
# between ticks, and at a 15-minute cadence warning every time is ~96 identical
# lines a day -- which is how the ones that matter stop being read. Once a day
# per profile; the count of the suppressed ones stays in the summary line.
STALE_RENOTIFY_HOURS = 24

# Stable labels for the ways a tick can fail, so the watchdog can alert on a
# *kind* of failure rather than on an error string with a profile name in it
# (which would re-alert every time the name changed). See
# `loop_watchdog.observe_issue_tags`.
ERR_AIRTABLE = "airtable-read"
ERR_NO_CLIENT = "no-mlx-client"
ERR_EMPTY_INVENTORY = "empty-mlx-inventory"
ERR_TAG_LOOKUP = "issue-tag-lookup"
ERR_MLX_WRITE = "mlx-write"
ERR_MLX_OUTAGE = "mlx-outage"
ERR_LEDGER_WRITE = "ledger-write"


def owned_tags() -> list:
    """Every tag this module writes -- and therefore every tag it may remove."""
    return list(OWNED_TAGS)


# ----------------------------------------------------------------- the ledger


def _state_path(app_dir=None) -> Path:
    return Path(app_dir or get_app_data_dir()) / STATE_FILENAME


def _read_state(app_dir=None) -> dict:
    """The whole state document, or ``{}`` if it is missing or unreadable."""
    try:
        raw = json.loads(_state_path(app_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def load_ledger(app_dir=None) -> dict:
    """``{MLX API ID: {"name": ..., "at": ...}}`` -- the profiles this pass tagged.

    A missing or unreadable file reads as empty, which is the safe direction:
    the bot then owns nothing and can only ever add.
    """
    tagged = _read_state(app_dir).get("tagged")
    return dict(tagged) if isinstance(tagged, dict) else {}


def load_stale_notices(app_dir=None) -> dict:
    """``{MLX API ID: ISO timestamp}`` -- when each dangling row was last warned
    about. Empty on a version-1 file, which costs one repeated warning."""
    seen = _read_state(app_dir).get("stale_seen")
    return dict(seen) if isinstance(seen, dict) else {}


def save_ledger(tagged: dict, app_dir=None, stale_seen=None) -> bool:
    """Write both sections. `stale_seen=None` keeps whatever is on disk, so a
    caller that only moved the ledger cannot drop the warning bookkeeping."""
    if stale_seen is None:
        stale_seen = load_stale_notices(app_dir)
    try:
        path = _state_path(app_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"version": STATE_VERSION, "tagged": dict(tagged),
             "stale_seen": dict(stale_seen)},
            indent=2, sort_keys=True), encoding="utf-8")
        return True
    except Exception:
        return False


def _hours_since(stamp: str, now: datetime) -> float:
    """Hours between an ISO stamp and `now`; a stamp we cannot read is treated
    as long ago, so an unparseable file re-warns rather than staying silent."""
    try:
        seen = datetime.fromisoformat(str(stamp))
        # A caller's `now` and a stored stamp can differ in awareness; treat a
        # naive one as UTC rather than letting the subtraction raise.
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - seen).total_seconds() / 3600.0
    except Exception:
        return float("inf")


# ------------------------------------------------------------------ planning


@dataclass
class ProfileIssue:
    """One Airtable row, and what MultiLogin currently says about it."""
    record_id: str
    name: str
    launch_id: str
    flagged: bool
    reason: str = ""
    # Airtable's park switch. Read only by `raised_by_person`, which refuses to
    # flag a profile a person has deliberately taken out of service.
    status: str = ""
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
            # A tag *this module put on* that is no longer there is not drift to
            # correct. It is a person, in the workspace they work in, saying they
            # have dealt with it -- see `cleared_by_person`. Re-asserting it would
            # overwrite the only signal they can give, every fifteen minutes.
            if not has_issue and owned_by_bot:
                return [], []
            return ([] if has_issue else [ISSUE_TAG]), []

        # Not flagged. The only tag that may come off is one this module owns
        # by name *and* put there itself. Both fences, in that order.
        removable = [t for t in held
                     if t.lower() in {o.lower() for o in OWNED_TAGS}]
        return [], (removable if (has_issue and owned_by_bot) else [])

    def raised_by_person(self, owned_by_bot: bool) -> bool:
        """True when a person put the `Issue` tag on a profile nothing has flagged.

        The other half of "the VA works in one system". Until now a hand-applied
        tag reached nothing at all: it did not flag the profile, did not stop the
        warm-up, and did not stop posting -- on 2026-08-11, 40 tags were in that
        state, fourteen of them on phones the warm-up was still driving.

        `status` is the one exception, and it is not a fudge. A profile a person
        has set `Inactive` is already held out of service by an explicit switch;
        flagging it adds nothing, and it would matter later, because clearing a
        flag hands the profile to `recovery_runner`, which sets Status back to
        **Active**. Raising a flag on a deliberately parked phone would therefore
        un-park it the moment somebody tidied the tag -- ten `Link` rows, blanks
        and retired numbers on this workspace are exactly that.
        """
        if self.flagged or not self.in_mlx:
            return False
        # A tag this pass put on is not a person's statement, and an unflagged
        # profile still wearing one means the opposite of a raise: somebody
        # cleared the flag in Airtable and the tag has not caught up yet. Raising
        # here would re-flag it every fifteen minutes and the Airtable route --
        # which still has to work -- would become impossible to use.
        if owned_by_bot:
            return False
        if str(self.status or "").strip().lower() == "inactive":
            return False
        held = {str(t).strip() for t in (self.current_tags or ()) if str(t).strip()}
        return any(t.lower() == ISSUE_TAG.lower() for t in held)

    def cleared_by_person(self, owned_by_bot: bool) -> bool:
        """True when a person took this pass's own `Issue` tag off in MultiLogin.

        The mirror used to run one way, so the only way to say "I have dealt with
        this" was to untick a checkbox in Airtable -- a system the people doing
        the dealing do not otherwise open. This is the same statement, made where
        they already are: the tag goes on when the bot flags, and taking it off
        says a person looked.

        Every clause is load-bearing:

        * `flagged` -- there is a flag to clear. An unflagged profile losing a
          tag is the mirror's own removal, one tick later.
        * `owned_by_bot` -- the ledger says *this module* put the tag there. A
          hand-applied `Issue` was never a bot flag, so its removal clears
          nothing; the 40 profiles wearing one on this workspace must not have
          Airtable rows rewritten because somebody tidied a tag.
        * `in_mlx` -- the profile is in the inventory we just read. "Not in the
          listing" and "listed without the tag" are the same shape and opposite
          meanings, and only one of them is a person.

        The caller's own guard matters as much: `sync_issue_tags` refuses the
        whole pass on an empty inventory, so a MultiLogin outage cannot read as
        the whole fleet being resolved at once.
        """
        if not (self.flagged and self.in_mlx and owned_by_bot):
            return False
        held = {str(t).strip() for t in (self.current_tags or ()) if str(t).strip()}
        return not any(t.lower() == ISSUE_TAG.lower() for t in held)


@dataclass
class IssueTagReport:
    checked: int = 0
    tagged: int = 0            # tag added
    untagged: int = 0          # tag removed
    resolved: int = 0          # person removed the tag; the flag came off
    raised: int = 0            # person added the tag; the flag went on
    parked_not_raised: int = 0 # hand-tagged, but Inactive: left alone
    unchanged: int = 0
    no_launch_id: int = 0      # Airtable row with no MLX API ID
    missing_in_mlx: int = 0    # API ID that the workspace no longer knows
    skipped_not_ours: int = 0  # unflagged, carries a hand-applied `Issue`
    duplicate_rows: int = 0    # extra Airtable rows sharing one MLX API ID
    stale_quiet: int = 0       # dangling rows warned about within the last day
    errors: list = dc_field(default_factory=list)
    error_kinds: list = dc_field(default_factory=list)
    changes: list = dc_field(default_factory=list)
    stale: list = dc_field(default_factory=list)

    def fail(self, kind: str, message: str) -> None:
        """Record one failure under a stable `kind` as well as in words."""
        self.errors.append(message)
        if kind not in self.error_kinds:
            self.error_kinds.append(kind)

    def summary(self) -> str:
        extra = ""
        if self.duplicate_rows:
            extra += f" dup-rows={self.duplicate_rows}"
        if self.stale_quiet:
            extra += f" stale-quiet={self.stale_quiet}"
        return (f"checked={self.checked} tagged={self.tagged} "
                f"untagged={self.untagged} resolved={self.resolved} "
                f"raised={self.raised} "
                f"unchanged={self.unchanged} "
                f"no-id={self.no_launch_id} missing-in-mlx={self.missing_in_mlx} "
                f"hand-tagged={self.skipped_not_ours}{extra} "
                f"errors={len(self.errors)}")


def build_states(profiles, tags_by_api_id: dict | None = None) -> list:
    """`airtable.posting_profiles()` output -> one :class:`ProfileIssue` each.

    Joined to MultiLogin on `MLX API ID` -- the 18-digit launch key, which is
    what the tag endpoints take as `profile_id`. Never the human serial
    (`MultiLogin Profile ID`), which those endpoints reject.

    `reason` is Airtable's `Issue Reason` (`posting_profiles` returns it): the
    single-select `retry_runner` / `stale_profiles` stamp when they tick the box
    -- `Human Verification Required`, `Retries Exhausted`, `Banned / Blocked`,
    `No Recent Success`. It decorates the log line, which is the difference
    between "24 profiles need a person" and knowing that 19 of them are one
    Instagram checkpoint and 2 are dead accounts.

    **De-duplicated on the API ID.** Two Airtable rows can point at one
    MultiLogin profile (a re-created row, a hand-copied API ID), and the sweep
    is one call per state: duplicates would mean two identical assigns a tick,
    and -- worse -- an unflagged duplicate could plan a removal right after the
    flagged one planned an add, so the profile's tag would depend on row order.
    One profile, one decision: flagged anywhere wins, and the first reason given
    for it is the one reported.
    """
    inventory = tags_by_api_id or {}
    states: list = []
    by_id: dict = {}
    for row in profiles or []:
        launch_id = str(row.get("launch_id") or "").strip()
        state = ProfileIssue(
            record_id=str(row.get("record_id") or ""),
            name=str(row.get("name") or "").strip() or str(row.get("record_id") or ""),
            launch_id=launch_id,
            flagged=bool(row.get("needs_human")),
            reason=str(row.get("reason") or "").strip(),
            status=str(row.get("status") or "").strip(),
            current_tags=tuple(inventory.get(launch_id) or ()),
            in_mlx=bool(launch_id) and launch_id in inventory,
        )
        # Rows with no API ID are all "" and are not each other's duplicates:
        # they are separate rows to count and report, so they are never merged.
        if not launch_id:
            states.append(state)
            continue
        first = by_id.get(launch_id)
        if first is None:
            by_id[launch_id] = state
            states.append(state)
            continue
        if state.flagged and not first.flagged:
            first.flagged = True
        if state.flagged and not first.reason:
            first.reason = state.reason
    return states


# -------------------------------------------------------------------- reconcile


def resolve_issue_tag_id(tag_client) -> str:
    """The id of the workspace's existing `Issue` tag. **Search only.**

    Deliberately not `tag_client.ensure_tag`, which creates the tag when
    `tag_ids_by_name()` does not return it (tags.py:105-108). Three facts make
    that creation unacceptable on an unattended tick:

    * the workspace *has* the tag -- de2aef69-b8c1-4547-90a4-0875b1d85361, with
      ~33 hand-applied uses -- so "not found" here is never really "not there";
    * two tags may share a name in this workspace (tags.py:81-89), so nothing at
      the far end would reject the duplicate;
    * a soft `/tag/search` failure (an empty page, a truncated answer, a 200
      with no `data`) is indistinguishable from an absent tag, and would mint a
      *second* `Issue`, assign its id, and leave the 33 real uses on the first
      one -- so the person filtering the workspace on `Issue` would see only
      what the bot had tagged since, and the fix would be a manual merge.

    Missing is therefore an error the pass reports and stops on. Creating the
    tag is a person's job in the MultiLogin UI, once.
    """
    ids = tag_client.tag_ids_by_name() or {}
    return str(ids.get(ISSUE_TAG.strip().lower()) or "")


def sync_issue_tags(airtable, tag_client=None, mlx_items=None, dry_run: bool = True,
                    logger=None, app_dir=None, adopt_existing: bool = False,
                    now=None, profiles=None, notifier=None) -> IssueTagReport:
    """Make the MultiLogin `Issue` tag agree with Airtable's flag. Idempotent.

    `tag_client` is optional and `mlx_items` may be empty, for the same reason
    `warmup_state` allows it: MultiLogin being down should cost this pass, not
    the caller. Both cases return an empty report with the reason in `errors`,
    having made no calls -- Airtable included, which is why those two guards run
    before the profile read rather than after it.

    `adopt_existing` is the escape hatch for the 33 hand-applied `Issue` tags:
    with it on, every profile currently carrying the tag counts as bot-owned and
    an unflagged one will therefore have it **stripped**. It is off by default
    and should only ever be turned on by a person who has decided those tags are
    stale, because the information is not recoverable from Airtable.
    """
    report = IssueTagReport()
    moment = now or datetime.now(timezone.utc)
    stamp = moment.isoformat(timespec="seconds")

    # The MultiLogin guards come FIRST, before Airtable is read. Either one ends
    # the pass having done nothing, and `posting_profiles` is a full scan of the
    # 151-row Profiles table -- so reading it first meant every tick of an MLX
    # outage still cost a full table read (96 a day) to throw the answer away.
    if tag_client is None:
        report.fail(ERR_NO_CLIENT, "no MultiLogin tag client; nothing read or written")
        _log(logger, report, dry_run)
        return report
    if not mlx_items:
        # An empty inventory is indistinguishable from "MultiLogin answered but
        # is not reachable", and reconciling against it would read every profile
        # as carrying no tags. Refuse rather than guess.
        report.fail(ERR_EMPTY_INVENTORY, "empty MultiLogin inventory; skipping the tag pass")
        _log(logger, report, dry_run)
        return report

    # Resolved once per sweep, not once per profile -- one name, ~150 profiles.
    # Search-only (`resolve_issue_tag_id`), and before the Airtable read for the
    # same reason as the guards above: it is the last thing that can stop the
    # pass without a row being needed. Skipped under dry-run, which therefore
    # makes no MultiLogin call at all.
    tag_id = None
    if not dry_run:
        try:
            tag_id = resolve_issue_tag_id(tag_client)
        except Exception as exc:
            report.fail(ERR_TAG_LOOKUP,
                        f"resolving tag {ISSUE_TAG!r}: {type(exc).__name__}: {exc}")
            _log(logger, report, dry_run)
            return report
        if not tag_id:
            report.fail(ERR_TAG_LOOKUP,
                        f"MultiLogin has no tag named {ISSUE_TAG!r} (this pass will not "
                        "create one -- add it in the workspace, or check /tag/search)")
            _log(logger, report, dry_run)
            return report

    if profiles is None:
        try:
            profiles = airtable.posting_profiles()
        except Exception as exc:
            report.fail(ERR_AIRTABLE, f"Airtable profiles: {type(exc).__name__}: {exc}")
            _log(logger, report, dry_run)
            return report

    from adb_bot.automation.warmup_state import tags_by_launch_id

    states = build_states(profiles, tags_by_launch_id(mlx_items))
    linked = sum(1 for row in (profiles or []) if str(row.get("launch_id") or "").strip())
    report.duplicate_rows = max(0, linked - sum(1 for s in states if s.launch_id))
    ledger = load_ledger(app_dir)
    # Dry runs never touch the state file (a plan must leave nothing behind), so
    # they always warn about a dangling row rather than reading the bookkeeping.
    stale_seen = {} if dry_run else load_stale_notices(app_dir)
    stale_now: dict = {}

    ledger_dirty = False
    consecutive_failures = 0
    newly_flagged: list = []

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
                # Worth a person's attention, but it does not change between
                # ticks: warn once a day per profile and count the rest, or the
                # one profile in this state (`Jasmin 9`) writes ~96 identical
                # WARNING lines a day into the log people read for real ones.
                last = stale_seen.get(state.launch_id)
                if last is not None and _hours_since(last, moment) < STALE_RENOTIFY_HOURS:
                    report.stale_quiet += 1
                    stale_now[state.launch_id] = last
                else:
                    report.stale.append(
                        f"{state.name} [{state.launch_id}] flagged, not in MultiLogin")
                    stale_now[state.launch_id] = stamp
            continue

        owned_by_bot = adopt_existing or (state.launch_id in ledger)

        # The other direction: a person removed the tag, so the flag comes off.
        # Only `Needs Human Check` is written -- `recovery_runner` is what then
        # sets Status back to Active and re-queues what was stuck, and it finds
        # profiles by the pair "unchecked but `Flagged At` still stamped". See
        # `AirtableClient.clear_human_flag`.
        if state.cleared_by_person(owned_by_bot):
            report.changes.append(
                f"unflag {state.name} [{state.launch_id}] -- {ISSUE_TAG} tag removed")
            if dry_run:
                report.resolved += 1
                continue
            try:
                if airtable.clear_human_flag(
                        state.record_id,
                        note=f"{ISSUE_TAG} tag removed in MultiLogin: somebody looked at this."):
                    report.resolved += 1
                else:
                    # The box was already clear -- the two systems simply agree
                    # ahead of the ledger. Drop the entry so this stops being
                    # reconsidered every quarter of an hour.
                    report.unchanged += 1
            except Exception as exc:
                report.fail(ERR_AIRTABLE,
                            f"{state.name}: clearing the flag: {type(exc).__name__}: {exc}")
                continue
            if ledger.pop(state.launch_id, None) is not None:
                ledger_dirty = True
            continue

        # And the direction that starts the whole thing: a person put the tag on
        # a profile nothing had flagged. Recorded in the ledger like a tag this
        # pass applied itself, because from here on the pair is managed -- which
        # is what lets taking the tag off again clear the flag.
        if state.raised_by_person(owned_by_bot):
            report.changes.append(
                f"flag {state.name} [{state.launch_id}] -- {ISSUE_TAG} tag added by hand")
            if dry_run:
                report.raised += 1
                continue
            try:
                if airtable.flag_profile_from_tag(
                        state.record_id,
                        note=f"{ISSUE_TAG} tag applied in MultiLogin."):
                    report.raised += 1
                else:
                    report.unchanged += 1
            except Exception as exc:
                report.fail(ERR_AIRTABLE,
                            f"{state.name}: raising the flag: {type(exc).__name__}: {exc}")
                continue
            ledger[state.launch_id] = {"name": state.name, "at": stamp,
                                       "reason": f"hand-applied {ISSUE_TAG} tag"}
            ledger_dirty = True
            newly_flagged.append((state.name, f"{ISSUE_TAG} tag applied in MultiLogin"))
            continue

        # Hand-tagged but parked: counted so the population stays visible rather
        # than disappearing into `unchanged`. See `raised_by_person` for why an
        # Inactive profile is left alone.
        if (not state.flagged
                and str(state.status or "").strip().lower() == "inactive"
                and any(t.lower() == ISSUE_TAG.lower() for t in state.current_tags)):
            report.parked_not_raised += 1

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
                # The transition into flagged, and the only place it is known
                # exactly once: the ledger write above is what makes the next
                # sweep call this profile "unchanged". Collect here, send one
                # message at the end -- ten profiles failing in the same sweep
                # is one notification, not ten.
                newly_flagged.append((state.name, state.reason))
            else:
                tag_client.unassign(state.launch_id, [tag_id])
                report.untagged += 1
                if ledger.pop(state.launch_id, None) is not None:
                    ledger_dirty = True
            consecutive_failures = 0
        except Exception as exc:
            report.fail(ERR_MLX_WRITE, f"{state.name}: MLX {type(exc).__name__}: {exc}")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                report.fail(ERR_MLX_OUTAGE,
                            f"{consecutive_failures} MultiLogin failures in a row; "
                            "stopping this sweep")
                break

    # One writer for both sections. The stale bookkeeping changes on its own
    # (nothing was tagged, a dangling row was warned about), so the write is not
    # conditional on the ledger having moved.
    if not dry_run and (ledger_dirty or stale_now != stale_seen):
        if not save_ledger(ledger, app_dir, stale_seen=stale_now):
            report.fail(ERR_LEDGER_WRITE, f"could not write {STATE_FILENAME}; " + (
                "tags written this pass are not recorded as ours" if ledger_dirty
                else "stale-row warnings will repeat next tick"))

    if not dry_run and newly_flagged:
        notify_newly_flagged(newly_flagged, logger=logger, notifier=notifier)

    _log(logger, report, dry_run)
    return report


def notify_newly_flagged(flagged: list, logger=None, notifier=None) -> bool:
    """Tell the group chat which profiles just started needing a person.

    Sent on the *transition* only -- the caller collects these at the moment the
    ledger records a tag, so a profile that has been flagged all night produces
    one message, not one every fifteen minutes. That is the same discipline
    `loop_watchdog` applies to stall alerts, and for the same reason: a channel
    that repeats itself stops being read.

    Silent and harmless when Telegram is not configured, which is the state of
    every box that has not been given a token.
    """
    if not flagged:
        return False
    if notifier is None:
        from adb_bot.clients.telegram import TelegramNotifier
        notifier = TelegramNotifier()
    if not notifier.configured:
        return False

    n = len(flagged)
    lines = [f"⚠️ <b>{n} profile{'' if n == 1 else 's'} "
             f"need{'s' if n == 1 else ''} a person</b>", ""]
    for name, reason in flagged:
        lines.append(f"• <b>{name}</b>" + (f" — {reason}" if reason else ""))
    lines += [
        "",
        f"They are tagged <code>{ISSUE_TAG}</code> in MultiLogin — filter on it "
        f"to find them.",
        "When it is fixed, untick <b>Needs Human Check</b> on that profile in "
        "Airtable → Profiles (Cloning). The tag comes off and the profile starts "
        "posting again on its own; nothing else to do.",
    ]
    sent = notifier.send("\n".join(lines), logger=logger)
    if logger is not None:
        logger.info("issue tags: %s the group about %d newly flagged profile(s)",
                    "notified" if sent else "could not notify", len(flagged))
    return sent


def _log(logger, report: IssueTagReport, dry_run: bool = False) -> None:
    """The one place this pass prints itself.

    Deliberately the *only* one: the caller used to re-print every change line
    under `--apply`-less runs, so a dry run listed each planned change twice.
    """
    if not logger:
        return
    prefix = "[DRY-RUN] " if dry_run else ""
    verb = "would " if dry_run else ""
    logger.info("%sissue tags: %s", prefix, report.summary())
    for line in report.changes[:20]:
        logger.info("  %s%s", verb, line)
    if len(report.changes) > 20:
        logger.info("  ... and %d more", len(report.changes) - 20)
    for line in report.stale[:10]:
        logger.warning("  %s", line)
    if report.stale_quiet:
        logger.info("  (%d more row(s) flagged with no MultiLogin profile, "
                    "already reported in the last %dh)", report.stale_quiet,
                    STALE_RENOTIFY_HOURS)
    for line in report.errors[:10]:
        logger.warning("  %s", line)
