"""Slot creation: fill the Posting Queue from Ready Spoof Variants (the missing
link between the spoof pipeline and the posting loop).

The chain is ``pipeline (spoof) -> [this] -> posting -> recheck``. The pipeline
writes Spoof Variants rows at Status Ready; the posting loop consumes Posting
Queue rows that are Pending and due. Nothing created those queue rows: the five
Airtable automations meant to do it are all undeployed, and their createRecord
node writes only Target Account + Scheduled DateTime + Post Status -- no Spoof
Variant link -- so every row they *would* create is thrown out by the posting
planner with "no Spoof Variant video path".

They are replaced with code because Airtable cannot see the two things that
decide what may be queued: the local post ledger, and the profile-driven
targeting path (models with real phones but no Accounts row, whose variants link
Profiles (Cloning) instead of Accounts).

For each eligible target this creates one queue row per slot that has already
come round today and isn't filled yet, each carrying an unused Ready variant
belonging to that target.

:func:`plan_slot_rows` is pure -- it takes pre-fetched rows (the shapes the
AirtableClient returns) and returns the rows it *would* create, so the whole
double-booking guard is testable without Airtable. :func:`run_queue_slots` is
the runner: it fetches, plans, and (outside dry-run) writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from adb_bot.automation.posting_planner import _first_link, _parse_dt
from adb_bot.clients import airtable as at

# The five daily slots, mirroring the Airtable automations this replaces.
DEFAULT_SLOT_TIMES = ("09:00", "12:00", "15:00", "18:00", "21:00")

# Slots are wall-clock times for the audience, not for the server: the same
# 09:00 has to mean 09:00 in Berlin whether the box runs on UTC or local time.
DEFAULT_TIMEZONE = "Europe/Berlin"

# Target kinds. A variant carries exactly one of the two links, and the queue
# row it produces must carry the SAME one -- see plan_slot_rows().
TARGET_ACCOUNT = "account"
TARGET_PROFILE = "profile"

# A variant that already has a queue row belongs to that row, in EVERY status,
# so it must never be handed to a second one. Pending = waiting to post,
# Verifying = sent but unproven (the recheck may still turn it into Posted).
# Posted is belt-and-braces: the variant should already be Used, but if that
# write failed this set is the only thing standing between us and posting the
# same clip twice.
#
# Failed is in here for a subtler reason. Recovering a failed row is the retry
# pass's job -- it re-queues the SAME row after consulting the ledger. If this
# loop could also draw that row's variant into a fresh slot, both would act on
# one clip and produce two Pending rows for it. The ledger still prevents the
# second post, so nothing goes out twice, but it costs a phone launch and files
# a confusing Skipped row. One clip, one owner: retry owns recovery, this loop
# only ever draws genuinely fresh variants.
VARIANT_HELD_BY = (at.POST_STATUS_PENDING, at.POST_STATUS_VERIFYING,
                   at.POST_STATUS_POSTED, at.POST_STATUS_FAILED)


@dataclass
class SlotTarget:
    """One thing that gets posts scheduled for it: an Account or an MLX profile."""
    kind: str          # TARGET_ACCOUNT | TARGET_PROFILE
    record_id: str
    name: str          # handle / profile name -- for logs and the row's Name

    @property
    def key(self) -> tuple:
        return (self.kind, self.record_id)


@dataclass
class PlannedRow:
    """A Posting Queue row that should exist. `scheduled` is UTC ISO (what the
    dateTime field stores); `slot` is the local label, for logs only."""
    target: SlotTarget
    slot: str
    scheduled: str
    variant_id: str

    @property
    def name(self) -> str:
        return f"{self.target.name} / {self.slot}"


@dataclass
class QueueReport:
    planned: list = field(default_factory=list)     # PlannedRow, in creation order
    rows_created: int = 0                           # written (or would-be, on a dry run)
    targets: int = 0
    slots_due: int = 0
    skipped: list = field(default_factory=list)     # (target/variant name, reason)
    errors: list = field(default_factory=list)      # (name, message)
    dry_run: bool = True

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        return (f"[{mode}] targets={self.targets} slots_due={self.slots_due} "
                f"rows={self.rows_created} skipped={len(self.skipped)} errors={len(self.errors)}")


def _zone(name: str, logger=None):
    """The slot timezone, falling back to the machine's local zone.

    Windows ships no tz database, so ``ZoneInfo('Europe/Berlin')`` raises there
    unless `tzdata` is installed. Falling back keeps slot creation running on a
    dev box instead of failing the whole loop over a missing data file.
    """
    try:
        return ZoneInfo(name)
    except Exception as exc:
        if logger:
            logger.warning("queue: timezone %s unavailable (%s); using the local zone", name, exc)
        return datetime.now().astimezone().tzinfo


def parse_slot_times(values) -> list:
    """``['09:00', '12:00']`` (or `time` objects) -> sorted `time` objects.
    Unparseable entries are dropped rather than raising -- a typo in one slot
    must not cost the other four."""
    out: list = []
    for value in values or ():
        if isinstance(value, time):
            out.append(value)
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            hour, _, minute = text.partition(":")
            out.append(time(int(hour), int(minute or 0)))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def due_slots(now: datetime, slot_times, tz) -> list:
    """Today's slots that have already come round, as ``[(label, datetime)]``.

    "Today" is the local day in `tz`, so the run's own timezone is irrelevant.
    Slots still ahead of `now` are deliberately left out: creating them early
    would make them due immediately for the posting loop (its only gate is
    ``Scheduled DateTime <= now``, and a slot written as "later today" that the
    row's own timestamp says is now would post all five at once).
    """
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    out: list = []
    for slot in parse_slot_times(slot_times):
        moment = local_now.replace(hour=slot.hour, minute=slot.minute,
                                   second=0, microsecond=0)
        if moment <= local_now:
            out.append((slot.strftime("%H:%M"), moment))
    return out


def _slot_key(value) -> str | None:
    """A minute-resolution UTC key for a scheduled time, so an existing row and
    the slot it came from compare equal regardless of seconds or offset."""
    moment = value if isinstance(value, datetime) else _parse_dt(value)
    if moment is None:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M")


def _variant_target_key(variant: dict) -> tuple | None:
    """Which target a Spoof Variant belongs to. Profile link wins if both are
    set (same precedence as create_spoof_variant / the posting planner)."""
    if variant.get("profile_id"):
        return (TARGET_PROFILE, variant["profile_id"])
    if variant.get("account_id"):
        return (TARGET_ACCOUNT, variant["account_id"])
    return None


def plan_slot_rows(targets, variants, queue_rows, now: datetime | None = None,
                   slot_times=DEFAULT_SLOT_TIMES, tz=None) -> QueueReport:
    """Decide which Posting Queue rows should exist right now. Pure: no writes.

    - `targets`: [SlotTarget] -- already filtered for eligibility by the caller
    - `variants`: [{'id', 'account_id', 'profile_id', 'status'}] -- Ready ones
    - `queue_rows`: existing Posting Queue rows ({'id', 'fields'}), **all
      statuses** -- see the two guards below

    Two things must never happen, and both are decided here:

    1. **A variant queued twice.** Same clip posted twice on the same account is
       the worst failure this bot has. A variant is excluded if it is not Ready
       (a Used one was already posted) or if any existing queue row in
       :data:`VARIANT_HELD_BY` links it. Within one run a variant is popped from
       the pool as it is assigned, so it cannot be handed to two slots either.
    2. **A slot filled twice.** A (target, slot) that already has a row in *any*
       status is skipped -- including Posted and Failed, because a re-filled slot
       is a second post at a time the schedule says has been served.
    """
    tz = tz or _zone(DEFAULT_TIMEZONE)
    now = now or datetime.now(tz)
    report = QueueReport(dry_run=True)
    report.targets = len(targets)

    due = due_slots(now, slot_times, tz)
    report.slots_due = len(due)
    if not due:
        return report

    # --- guard state from the existing queue -------------------------------
    held_variants: dict = {}
    filled: set = set()
    for row in queue_rows or []:
        fields = row.get("fields", {}) or {}
        status = at._select_name(fields.get(at.F_PQ_POST_STATUS))
        variant_id = _first_link(fields, at.F_PQ_SPOOF_VARIANT)
        if variant_id and status in VARIANT_HELD_BY:
            held_variants[variant_id] = status
        account_id = _first_link(fields, at.F_PQ_TARGET_ACCOUNT)
        profile_id = _first_link(fields, at.F_PQ_TARGET_PROFILE)
        key = ((TARGET_PROFILE, profile_id) if profile_id
               else (TARGET_ACCOUNT, account_id) if account_id else None)
        slot_key = _slot_key(fields.get(at.F_PQ_SCHEDULED))
        if key and slot_key:
            filled.add((key, slot_key))

    # --- the pool of variants each target may draw from --------------------
    # Oldest first: the queue works through the content backlog in the order it
    # was spoofed instead of leaving early clips stranded forever.
    pools: dict = {}
    targets_by_key = {t.key: t for t in targets}
    stranded: set = set()
    # What a target's variants are tied up in, so "nothing to post" can say
    # WHICH kind of nothing: no media spoofed yet (the pipeline owes us one) vs
    # media that exists but belongs to a row already in flight. Those need
    # opposite responses, and one shared skip line would hide the difference.
    tied_up: dict = {}
    for variant in sorted(variants or [],
                          key=lambda v: (str(v.get("created") or ""), str(v.get("id") or ""))):
        variant_id = variant.get("id")
        if not variant_id:
            continue
        if variant_id in held_variants:
            key = _variant_target_key(variant)
            if key is not None:
                tied_up.setdefault(key, set()).add(held_variants[variant_id])
            continue
        status = variant.get("status")
        if status is not None and status != at.SV_STATUS_READY:
            continue
        key = _variant_target_key(variant)
        if key is None:
            continue
        if key not in targets_by_key:
            # The variant's target isn't eligible (paused/banned account, or a
            # profile that isn't a posting target). Report it once per target,
            # not once per variant -- a paused account can hold dozens.
            if key not in stranded:
                stranded.add(key)
                report.skipped.append((str(key[1]), f"Ready variant(s) for a {key[0]} that is not an eligible target"))
            continue
        pools.setdefault(key, []).append(variant_id)

    # --- one row per unfilled due slot -------------------------------------
    for target in sorted(targets, key=lambda t: (t.name or "", t.record_id)):
        pool = pools.get(target.key, [])
        for label, moment in due:
            slot_key = _slot_key(moment)
            if (target.key, slot_key) in filled:
                continue
            if not pool:
                # One skip per target, not per slot: a target waiting on the
                # spoof pipeline would otherwise add five identical lines to
                # every run's log and bury the skips that mean something.
                holders = tied_up.get(target.key)
                if holders:
                    # Not "no media" -- media that an existing row still owns.
                    # A Failed holder is the retry pass's to recover or to give
                    # up on; queueing a fresh row for the same clip would race it.
                    reason = ("its Ready variant(s) belong to an existing "
                              f"{'/'.join(sorted(holders))} row (from the {label} slot on)")
                else:
                    reason = f"no unused Ready Spoof Variant (from the {label} slot on)"
                report.skipped.append((target.name, reason))
                break
            report.planned.append(PlannedRow(
                target=target,
                slot=label,
                # Stored as UTC: Airtable's dateTime is UTC and the posting
                # planner compares it against `now` as an instant.
                scheduled=moment.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
                variant_id=pool.pop(0),
            ))

    return report


def collect_targets(airtable, include_profiles: bool = True) -> list:
    """The targets a slot may be created for.

    Accounts come from ``active_accounts_by_model()``, which applies exactly the
    guards the posting planner would apply later (Lifecycle Stage Active, not
    Paused, not Needs Human Verification) -- queuing a row the planner will only
    throw away is just noise in the base.

    Profiles come from ``profile_targets_by_model()`` (MLX inventory, link
    profiles and profiles without an MLX API ID excluded). They carry no health
    guards: there is no Accounts row to hold that state.
    """
    targets: list = []
    for entries in (airtable.active_accounts_by_model() or {}).values():
        for entry in entries:
            targets.append(SlotTarget(TARGET_ACCOUNT, entry["account_id"], entry.get("handle") or entry["account_id"]))
    if include_profiles:
        for entries in (airtable.profile_targets_by_model() or {}).values():
            for entry in entries:
                targets.append(SlotTarget(TARGET_PROFILE, entry["profile_id"], entry.get("handle") or entry["profile_id"]))
    return targets


def run_queue_slots(airtable, logger, slot_times=DEFAULT_SLOT_TIMES,
                    timezone_name: str = DEFAULT_TIMEZONE, now: datetime | None = None,
                    dry_run: bool = True, include_profiles: bool = True) -> QueueReport:
    """Create the Posting Queue rows for today's slots that have come round.

    Entry point for the loop (`run_loop` wires the CLI). Dry-run is the default
    and writes nothing -- it reports exactly the rows an --apply run would make.

    Caption is deliberately left unset: the posting planner treats it as
    optional, and inventing a caption rotation here would put text on posts that
    nobody chose. Rows carry Post Status Pending, the slot's Scheduled DateTime,
    the Spoof Variant, and whichever ONE target link the variant carries.
    """
    tz = _zone(timezone_name, logger)
    now = now or datetime.now(tz)

    try:
        targets = collect_targets(airtable, include_profiles=include_profiles)
        variants = airtable.list_ready_variants()
        # Every status, not just Pending: a slot whose post already succeeded
        # (or failed) has been served, and re-filling it means posting twice at
        # a time the schedule says is done.
        queue_rows = airtable.list_queue_rows()
    except Exception as exc:
        logger.error("queue: could not read Airtable: %s", exc)
        report = QueueReport(dry_run=dry_run)
        report.errors.append(("<airtable>", str(exc)))
        return report

    report = plan_slot_rows(targets, variants, queue_rows, now=now,
                            slot_times=slot_times, tz=tz)
    report.dry_run = dry_run

    for skip in report.skipped:
        logger.info("queue: skipping %s: %s", skip[0], skip[1])

    for row in report.planned:
        if dry_run:
            report.rows_created += 1
            logger.info("[DRY-RUN] would queue %s at %s (variant %s)",
                        row.name, row.scheduled, row.variant_id)
            continue
        record_id = airtable.create_posting_queue(
            row.scheduled,
            row.variant_id,
            target_account_id=row.target.record_id if row.target.kind == TARGET_ACCOUNT else None,
            target_profile_id=row.target.record_id if row.target.kind == TARGET_PROFILE else None,
            name=row.name,
        )
        if not record_id:
            report.errors.append((row.name, "failed to create the Posting Queue row"))
            continue
        report.rows_created += 1
        logger.info("queue: %s at %s -> %s", row.name, row.scheduled, record_id)

    logger.info("queue: %s", report.summary())
    return report
