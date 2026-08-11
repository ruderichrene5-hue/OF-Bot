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
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from adb_bot.automation.posting_planner import _first_link, _parse_dt
from adb_bot.automation.caption_rotation import CaptionRotation
from adb_bot.clients import airtable as at

# The daily slots. Every eligible target gets one row per slot, so this grid is
# per-model spacing: two hours apart across the 09:00-21:00 posting day, seven
# reels per model per day. (Was five slots three hours apart, mirroring the
# Airtable automations this replaces.)
#
# This is the FALLBACK grid now: when Airtable carries per-model times
# (Models.Reel Post Times), each model brings its own -- see ModelSchedule.
DEFAULT_SLOT_TIMES = ("09:00", "11:00", "13:00", "15:00", "17:00", "19:00", "21:00")

# A model with no times picked is not "off" -- it posts whenever a spoofed video
# is available. Two bounds keep that from emptying the whole variant pool in an
# afternoon, because the queue loop runs every 15 minutes and the posting loop
# takes any row whose Scheduled DateTime has passed:
#
# - a minimum gap between one flexible post and the next, matching the two hours
#   the standing grid puts between slots;
# - a daily cap, defaulting to the number of slots that grid has, so a flexible
#   model posts the same volume per day as a scheduled one, just at times the
#   bot chooses. Models.Reels Per Day overrides it per model.
DEFAULT_ANYTIME_GAP_MINUTES = 120
DEFAULT_ANYTIME_MAX_PER_DAY = len(DEFAULT_SLOT_TIMES)

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


@dataclass(frozen=True)
class ModelSchedule:
    """When one model's reels go out, as chosen in Airtable.

    `times` empty is the flexible mode ("anytime a video is available"), not an
    off switch -- an empty Reel Post Times is the default state of every model
    row, and a model nobody has scheduled yet should still post.
    """
    times: tuple = ()               # ('09:00', '13:00'), local wall clock
    per_day: int | None = None      # flexible mode only; None = the caller's default

    @property
    def is_flexible(self) -> bool:
        return not self.times


def schedules_from_airtable(raw) -> dict | None:
    """`AirtableClient.reel_schedules_by_model()` -> {model key: ModelSchedule}.

    Passes None straight through: that is the client saying the base has no
    per-model times at all, and the caller must keep its single global grid
    rather than treat every model as flexible.
    """
    if raw is None:
        return None
    out: dict = {}
    for model_key, entry in (raw or {}).items():
        entry = entry or {}
        # Through parse_slot_times so a hand-typed or malformed choice is dropped
        # the same way `--slots` handles one, instead of crashing the loop.
        times = tuple(t.strftime("%H:%M") for t in parse_slot_times(entry.get("times") or ()))
        per_day = entry.get("per_day")
        try:
            per_day = int(per_day) if per_day is not None else None
        except (TypeError, ValueError):
            per_day = None
        out[str(model_key).strip().lower()] = ModelSchedule(times=times, per_day=per_day)
    return out


@dataclass
class SlotTarget:
    """One thing that gets posts scheduled for it: an Account, an MLX profile,
    or -- on a phone carrying two Instagram accounts -- one account on a profile.

    The last case is why `key` is not just (kind, record_id). Both accounts of a
    two-account phone share a Profiles row, so keyed on the record alone they
    would be one target: the second account would find every slot already
    "filled" by the first and never post at all. The slot is what makes them two.
    """
    kind: str          # TARGET_ACCOUNT | TARGET_PROFILE
    record_id: str
    name: str          # handle / profile name -- for logs and the row's Name
    model_key: str = ""  # lower-cased model name, to find this target's schedule
    slot: str = at.SLOT_PRIMARY   # which account on the phone
    ig_handle: str | None = None  # the account to switch to; None = whoever is signed in
    profile_name: str = ""        # the phone this target sits on, when they differ

    @property
    def key(self) -> tuple:
        return (self.kind, self.record_id, self.slot or at.SLOT_PRIMARY)

    @property
    def label(self) -> str:
        """What a queue row calls this target.

        For a second account it is ``"Jil 5 (jiji.ll12)"`` rather than the bare
        handle: the row Name is the only place a queue row says which target it
        belongs to without resolving a link, and the report reads the model off
        its first word. A row called "jiji.ll12 / 09:00" would file itself under
        a model of that name.
        """
        if self.profile_name and self.profile_name != self.name:
            return f"{self.profile_name} ({self.name})"
        return self.name


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
        return f"{self.target.label} / {self.slot}"


@dataclass
class QueueReport:
    planned: list = field(default_factory=list)     # PlannedRow, in creation order
    rows_created: int = 0                           # written (or would-be, on a dry run)
    targets: int = 0
    slots_due: int = 0
    flexible_targets: int = 0                       # targets whose model picked no times
    skipped: list = field(default_factory=list)     # (target/variant name, reason)
    errors: list = field(default_factory=list)      # (name, message)
    dry_run: bool = True

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        flexible = f" flexible={self.flexible_targets}" if self.flexible_targets else ""
        return (f"[{mode}] targets={self.targets} slots_due={self.slots_due}{flexible} "
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
    must not cost the other four.

    A bare string is split on commas first. `--slots` is documented as
    comma-separated and was handed straight in, but a string is iterable, so it
    was consumed one character at a time: '09:00,11:00' parsed as '0', '9', ':',
    '0'... and yielded slots at 00:00/01:00/09:00 instead of failing loudly.
    """
    if isinstance(values, str):
        values = values.split(",")
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


def _slot_label_from_name(value) -> str | None:
    """The ``HH:MM`` slot a row was created for, read back off its Name.

    Rows are named ``"<target> / HH:MM"`` by :attr:`PlannedRow.name`, and nothing
    rewrites the Name afterwards -- unlike Scheduled DateTime, which the retry
    pass moves. Returns None for a name that does not end in a slot label, so a
    hand-made row simply falls back to the timestamp guard.
    """
    text = str(value or "").strip()
    if "/" not in text:
        return None
    tail = text.rsplit("/", 1)[1].strip()
    try:
        hour, _, minute = tail.partition(":")
        return time(int(hour), int(minute or 0)).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def _as_utc(moment, tz):
    """A datetime as an aware UTC instant; naive input is read as local `tz`."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(timezone.utc)


def _row_day(row: dict, fields: dict, tz) -> str | None:
    """The local day a queue row belongs to, ``YYYY-MM-DD``.

    Airtable's `createdTime` is preferred over Scheduled DateTime because the
    retry pass moves the schedule and nothing moves the creation stamp. It is
    what makes the guards below per-day: keyed on the slot label alone, a
    ``Jil 1 / 09:00`` row created today blocked the 09:00 slot on every future
    day as well, because the queue reads the Posting Queue in full and nothing
    deletes yesterday's rows -- so each target would have posted each slot once,
    ever. Rows made before this (and rows in tests) carry no createdTime, so the
    scheduled day stands in.
    """
    moment = _parse_dt(row.get("createdTime")) or _parse_dt(fields.get(at.F_PQ_SCHEDULED))
    moment = _as_utc(moment, tz)
    return moment.astimezone(tz).strftime("%Y-%m-%d") if moment else None


def _slot_name(value) -> str:
    """An Account Slot as a key: anything unset reads as Primary.

    Every queue row and every variant made before two-account phones existed
    has no slot, and all of them belong to the account the phone signs in as.
    Defaulting here (rather than at each call site) is what lets those rows keep
    matching the targets they have always matched.
    """
    return at._select_name(value) or at.SLOT_PRIMARY


def _variant_target_key(variant: dict) -> tuple | None:
    """Which target a Spoof Variant belongs to. Profile link wins if both are
    set (same precedence as create_spoof_variant / the posting planner).

    The slot is part of the key for profile-linked variants: on a two-account
    phone both accounts link the same Profiles row, and without it the primary
    account would drain the pool the second account's clips are sitting in --
    then post them, on the wrong account."""
    if variant.get("profile_id"):
        return (TARGET_PROFILE, variant["profile_id"], _slot_name(variant.get("slot")))
    if variant.get("account_id"):
        # An Accounts row IS one Instagram account, so it has no second slot.
        return (TARGET_ACCOUNT, variant["account_id"], at.SLOT_PRIMARY)
    return None


def plan_slot_rows(targets, variants, queue_rows, now: datetime | None = None,
                   slot_times=DEFAULT_SLOT_TIMES, tz=None, schedules=None,
                   anytime_gap_minutes: int = DEFAULT_ANYTIME_GAP_MINUTES,
                   anytime_max_per_day: int = DEFAULT_ANYTIME_MAX_PER_DAY) -> QueueReport:
    """Decide which Posting Queue rows should exist right now. Pure: no writes.

    - `targets`: [SlotTarget] -- already filtered for eligibility by the caller
    - `variants`: [{'id', 'account_id', 'profile_id', 'status'}] -- Ready ones
    - `queue_rows`: existing Posting Queue rows ({'id', 'fields'}), **all
      statuses** -- see the two guards below
    - `schedules`: {model key: ModelSchedule} from Airtable, or **None** when the
      base has no per-model times and every target runs on `slot_times`

    Each target's day comes from its model (`schedules`), so two models can post
    at completely different times:

    - **times picked** -> one row per picked time that has come round today.
    - **no times picked** -> flexible: one row scheduled *now* whenever a Ready
      variant is free, at most `anytime_max_per_day` a day (or the model's own
      `per_day`) and never inside `anytime_gap_minutes` of that target's last
      scheduled post. This is the "post it whenever there is a video" mode, and
      those two bounds are the only thing pacing it -- the posting loop takes any
      row whose Scheduled DateTime has passed, so an unbounded flexible target
      would drain its whole variant pool within an hour.

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
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    now_utc = _as_utc(now, tz)
    today = local_now.strftime("%Y-%m-%d")
    report = QueueReport(dry_run=True)
    report.targets = len(targets)

    due_cache: dict = {}

    def due_for(times) -> list:
        """Today's due slots for one set of times, computed once per set."""
        key = tuple(times)
        if key not in due_cache:
            due_cache[key] = due_slots(now, times, tz)
        return due_cache[key]

    if schedules is None:
        # One grid for every target: what this loop did before per-model times,
        # and what a base without the Reel Post Times field keeps doing.
        report.slots_due = len(due_for(slot_times))
        if not report.slots_due:
            return report

    # --- guard state from the existing queue -------------------------------
    held_variants: dict = {}
    filled: set = set()
    filled_labels: set = set()
    rows_on_day: dict = {}       # (target key, YYYY-MM-DD) -> rows -- the daily cap
    last_scheduled: dict = {}    # target key -> latest scheduled instant -- the gap
    for row in queue_rows or []:
        fields = row.get("fields", {}) or {}
        status = at._select_name(fields.get(at.F_PQ_POST_STATUS))
        variant_id = _first_link(fields, at.F_PQ_SPOOF_VARIANT)
        if variant_id and status in VARIANT_HELD_BY:
            held_variants[variant_id] = status
        account_id = _first_link(fields, at.F_PQ_TARGET_ACCOUNT)
        profile_id = _first_link(fields, at.F_PQ_TARGET_PROFILE)
        row_slot = _slot_name(fields.get(at.F_PQ_ACCOUNT_SLOT))
        key = ((TARGET_PROFILE, profile_id, row_slot) if profile_id
               else (TARGET_ACCOUNT, account_id, at.SLOT_PRIMARY) if account_id else None)
        slot_key = _slot_key(fields.get(at.F_PQ_SCHEDULED))
        if key and slot_key:
            filled.add((key, slot_key))
        if key:
            day = _row_day(row, fields, tz)
            if day:
                rows_on_day[(key, day)] = rows_on_day.get((key, day), 0) + 1
            scheduled = _as_utc(_parse_dt(fields.get(at.F_PQ_SCHEDULED)), tz)
            if scheduled and scheduled > last_scheduled.get(key, scheduled - timedelta(seconds=1)):
                last_scheduled[key] = scheduled
        # ...and again by the slot LABEL the row was created for. Scheduled
        # DateTime is not stable: the retry pass re-queues a failed row at
        # now+backoff, which moves it off its slot's timestamp. Keyed only on
        # that timestamp, this loop then saw the slot as unserved and created a
        # SECOND row for it, drawing a different variant -- so a profile whose
        # post failed got the retry AND a fresh post for the same slot. The ledger
        # never caught it because the clips differ. The Name ("<target> / HH:MM")
        # is written once at creation and never rewritten, so it still says which
        # slot the row belongs to. Found live 2026-08-04: six such duplicates.
        #
        # Keyed with the row's DAY as well (see _row_day): the label alone is the
        # same string every day, so yesterday's "Jil 1 / 09:00" made today's
        # 09:00 slot look served.
        label = _slot_label_from_name(fields.get(at.F_PQ_NAME))
        if key and label:
            day = _row_day(row, fields, tz)
            if day:
                filled_labels.add((key, day, label))

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

    def no_content_reason(target, when: str) -> str:
        """Why a target that is owed a row gets none. Two different problems:
        nothing spoofed yet (the pipeline owes us one) versus media an existing
        row still owns -- a Failed holder is the retry pass's to recover or to
        give up on, and queueing a fresh row for the same clip would race it."""
        holders = tied_up.get(target.key)
        if holders:
            return ("its Ready variant(s) belong to an existing "
                    f"{'/'.join(sorted(holders))} row ({when})")
        return f"no unused Ready Spoof Variant ({when})"

    gap = timedelta(minutes=max(0, int(anytime_gap_minutes or 0)))

    # --- one row per unfilled due slot -------------------------------------
    for target in sorted(targets, key=lambda t: (t.name or "", t.record_id)):
        pool = pools.get(target.key, [])
        schedule = schedules.get(target.model_key or "") if schedules is not None else None

        # A model with no row in `schedules` is treated exactly like one whose
        # times are empty: a target nobody has scheduled still posts.
        if schedules is not None and (schedule is None or schedule.is_flexible):
            report.flexible_targets += 1
            cap = schedule.per_day if (schedule and schedule.per_day) else anytime_max_per_day
            if cap and rows_on_day.get((target.key, today), 0) >= cap:
                report.skipped.append((target.name, f"no fixed post times, and today's {cap} post(s) are queued already"))
                continue
            last = last_scheduled.get(target.key)
            if last is not None and now_utc - last < gap:
                wait = int((gap - (now_utc - last)).total_seconds() // 60) + 1
                report.skipped.append((target.name, "no fixed post times; the last post is too "
                                                    f"recent (next one in ~{wait} min)"))
                continue
            if not pool:
                report.skipped.append((target.name, no_content_reason(target, "no fixed post times, nothing ready to post now")))
                continue
            # Scheduled for now, so the posting loop takes it on its next tick --
            # "whenever a video is available" is the whole point of this mode.
            moment = local_now.replace(second=0, microsecond=0)
            report.planned.append(PlannedRow(
                target=target,
                slot=moment.strftime("%H:%M"),
                scheduled=moment.astimezone(timezone.utc).isoformat(),
                variant_id=pool.pop(0),
            ))
            continue

        for label, moment in due_for(schedule.times if schedule is not None else slot_times):
            slot_key = _slot_key(moment)
            if (target.key, slot_key) in filled or (target.key, today, label) in filled_labels:
                continue
            if not pool:
                # One skip per target, not per slot: a target waiting on the
                # spoof pipeline would otherwise add five identical lines to
                # every run's log and bury the skips that mean something.
                report.skipped.append((target.name, no_content_reason(target, f"from the {label} slot on")))
                break
            report.planned.append(PlannedRow(
                target=target,
                slot=label,
                # Stored as UTC: Airtable's dateTime is UTC and the posting
                # planner compares it against `now` as an instant.
                scheduled=moment.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
                variant_id=pool.pop(0),
            ))

    if schedules is not None:
        # Every distinct time that came round today across the models' own grids.
        report.slots_due = len({label for due in due_cache.values() for label, _ in due})

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

    A phone with two Instagram accounts yields **two** profile targets on one
    record id, distinguished by their slot -- so the second account gets its own
    slots at the same times as the first, and the posting loop (which runs a
    phone's items one after another) posts them back to back on one launch.

    Both are keyed by lower-cased model name, and that key is kept on the target:
    it is how a row finds its model's Reel Post Times later.
    """
    targets: list = []
    for model_key, entries in (airtable.active_accounts_by_model() or {}).items():
        for entry in entries:
            targets.append(SlotTarget(TARGET_ACCOUNT, entry["account_id"],
                                      entry.get("handle") or entry["account_id"], model_key))
    if include_profiles:
        for model_key, entries in (airtable.profile_targets_by_model() or {}).items():
            for entry in entries:
                targets.append(SlotTarget(
                    TARGET_PROFILE, entry["profile_id"],
                    entry.get("handle") or entry["profile_id"], model_key,
                    # A two-account phone arrives here as two entries sharing a
                    # profile_id; the slot is what keeps them apart from here on.
                    slot=entry.get("slot") or at.SLOT_PRIMARY,
                    ig_handle=entry.get("ig_handle"),
                    profile_name=entry.get("profile_name") or "",
                ))
    return targets


def run_queue_slots(airtable, logger, slot_times=DEFAULT_SLOT_TIMES,
                    timezone_name: str = DEFAULT_TIMEZONE, now: datetime | None = None,
                    dry_run: bool = True, include_profiles: bool = True,
                    use_model_times: bool = True,
                    anytime_gap_minutes: int = DEFAULT_ANYTIME_GAP_MINUTES,
                    anytime_max_per_day: int = DEFAULT_ANYTIME_MAX_PER_DAY,
                    caption_rotation=None) -> QueueReport:
    """Create the Posting Queue rows for today's slots that have come round.

    Entry point for the loop (`run_loop` wires the CLI). Dry-run is the default
    and writes nothing -- it reports exactly the rows an --apply run would make.

    Times come from each model's `Reel Post Times` in Airtable, so the people
    running the base decide when a model posts without touching a timer or a
    command line. `slot_times` is the fallback for a base that has no such field,
    and `use_model_times=False` forces that fallback for one run.

    Each row gets a Caption from the Caption Pool, rotating per target so an
    account walks the whole pool before repeating and no two accounts are in
    step (see `caption_rotation`). This replaces the never-deployed "Caption
    Rotation" Airtable automation, which could only ever have served
    account-targeted rows -- it reads `Accounts.Next Caption ID`, and a
    profile-targeted row has no Accounts record to read it from.

    Pass `caption_rotation=False` to go back to captionless rows. Rows carry
    Post Status Pending, the slot's Scheduled DateTime, the Spoof Variant, and
    whichever ONE target link the variant carries.
    """
    tz = _zone(timezone_name, logger)
    now = now or datetime.now(tz)

    if caption_rotation is None:
        caption_rotation = CaptionRotation()
    elif caption_rotation is False:
        caption_rotation = None

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

    schedules = None
    if use_model_times:
        reader = getattr(airtable, "reel_schedules_by_model", None)
        schedules = schedules_from_airtable(reader() if reader else None)
    if schedules is None:
        logger.info("queue: no per-model reel times in this base; using the %s slot grid",
                    ",".join(str(s) for s in slot_times))
    else:
        scheduled_models = sorted(k for k, s in schedules.items() if s.times)
        logger.info("queue: per-model reel times for %d model(s)%s; the rest post whenever "
                    "a video is ready (max %s/day, %s min apart)",
                    len(scheduled_models),
                    f" ({', '.join(scheduled_models)})" if scheduled_models else "",
                    anytime_max_per_day, anytime_gap_minutes)

    report = plan_slot_rows(targets, variants, queue_rows, now=now,
                            slot_times=slot_times, tz=tz, schedules=schedules,
                            anytime_gap_minutes=anytime_gap_minutes,
                            anytime_max_per_day=anytime_max_per_day)
    report.dry_run = dry_run

    for skip in report.skipped:
        logger.info("queue: skipping %s: %s", skip[0], skip[1])

    # One read for the whole run. An empty or unreadable pool is not fatal: rows
    # go out captionless, exactly as they did before this existed.
    pool = []
    if caption_rotation is not None:
        # getattr, like the per-model schedules above: a client (or a base)
        # without a Caption Pool queues captionless rather than failing.
        reader = getattr(airtable, "caption_pool", None)
        try:
            pool = reader() if reader else []
        except Exception as exc:
            logger.warning("queue: could not read the Caption Pool (%s); "
                           "queueing without captions", exc)
        if not pool:
            logger.warning("queue: no active captions in the pool; "
                           "queueing without captions")
        else:
            logger.info("queue: rotating %d active caption(s) across targets", len(pool))

    for row in report.planned:
        caption = None
        if pool:
            rotation_key = ":".join(str(part) for part in row.target.key)
            caption = (caption_rotation.peek_for(rotation_key, pool) if dry_run
                       else caption_rotation.next_for(rotation_key, pool))

        if dry_run:
            report.rows_created += 1
            logger.info("[DRY-RUN] would queue %s at %s (variant %s, caption %s)",
                        row.name, row.scheduled, row.variant_id,
                        (caption or {}).get("caption_id") or "none")
            continue
        record_id = airtable.create_posting_queue(
            row.scheduled,
            row.variant_id,
            target_account_id=row.target.record_id if row.target.kind == TARGET_ACCOUNT else None,
            target_profile_id=row.target.record_id if row.target.kind == TARGET_PROFILE else None,
            name=row.name,
            # Which Instagram account on the phone this row posts as. Empty for
            # every single-account phone, which is what the flow already assumes.
            target_handle=row.target.ig_handle,
            account_slot=row.target.slot,
            caption_id=(caption or {}).get("record_id"),
        )
        if not record_id:
            # The rotation advanced for a row that was never created. Rewinding
            # is not worth it: the cost is one skipped caption out of 500, and a
            # rewind that raced another loop would hand the same caption twice.
            report.errors.append((row.name, "failed to create the Posting Queue row"))
            continue
        report.rows_created += 1
        # Persist per row, not once at the end: a crash mid-run would otherwise
        # replay the same captions onto rows that already exist.
        if caption and caption_rotation is not None:
            caption_rotation.save()
        logger.info("queue: %s at %s -> %s (caption %s)", row.name, row.scheduled,
                    record_id, (caption or {}).get("caption_id") or "none")

    logger.info("queue: %s", report.summary())
    return report
