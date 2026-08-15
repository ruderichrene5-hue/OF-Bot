"""MultiLogin -> Airtable profile sync (checklist loop #4).

MultiLogin is the source of truth for which mobile profiles exist *and* for
what each one is called, which folder it sits in and how it is tagged. Airtable
only learns any of that when this loop diffs the two and writes the difference.
It never talks to a phone and never touches Instagram -- it's pure data
plumbing meant to run unattended, every few hours.

Design:
- normalize each MLX `list` item into a small, tolerant dataclass;
- diff against the existing Profiles (Cloning) rows, keyed by the human
  `serial_no` (`MultiLogin Profile ID`), which is the stable match key --
  NOT the 18-digit `id`, which is the launch key we *store* but don't match on,
  and not the name, which is the thing that moves;
- for a profile MLX has and Airtable doesn't: create a Device, a Proxy, and a
  Profile (Cloning) row, linked together (and to its Model when the name
  matches an existing Models row);
- for a profile Airtable already has, reconcile it: `Profile Name`, `MLX
  Folder` and `MLX Tags` are made to match MultiLogin, and `MLX API ID` /
  `Time Zone` are backfilled when blank.

**Reconcile vs backfill.** The launch key and time zone are *backfilled* --
written only into an empty field, never over a value -- because they are
machine keys somebody may have corrected by hand. Name, folder and tags are
*reconciled*: MLX wins, every run. That asymmetry is the whole point of the
loop. Before it, an existing row only ever had blanks filled, so this pass
reported `unchanged` for a profile that had been renamed and moved to another
model's folder months earlier, and 63 phones sat in Airtable under names like
`Blank (24)` that MultiLogin had not used since.

**`Status` is never synced, in either direction.** MLX `status=2` only means
the phone is enabled; Airtable's `Status` is the human park switch, and a
banned or challenged account is parked here while MLX still calls it active.
Syncing it would un-park exactly the accounts somebody parked on purpose.

The planning half (`normalize_mlx_item`, `plan_sync`) is pure and unit-tested.
The apply half (`apply_sync`) does the Airtable writes and is guarded behind an
explicit `dry_run=False`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from adb_bot.clients import airtable as at

# MLX `status` codes: 2 == active. Anything else we treat as inactive.
MLX_STATUS_ACTIVE = 2

# These MLX profiles are cloud Android phones running native Instagram.
IG_PACKAGE = "com.instagram.android"
PROXY_PROVIDER = "Multilogin"

# MLX folder names that are generic buckets, not a real model. A profile in one
# of these gets synced but without a Model link. (MLX's per-item `group.name` is
# the workspace GUID, NOT the model -- the model is the *folder* name.)
_STAGING_FOLDERS = {"default folder", "unnamed", ""}

# Trailing tokens stripped when deriving a model name from a serial_name like
# "Nikki 1", "Luisa Link", or "Blank 1 (6)" -> "Nikki" / "Luisa" / "Blank".
# Used only as a fallback when no folder map is supplied.
_TRAILING_TOKEN = re.compile(r"[\s_-]*(?:\(\d+\)|\d+|link)$", re.IGNORECASE)


@dataclass
class NormalizedProfile:
    """The subset of an MLX profile the sync cares about, already flattened."""

    serial_no: str
    api_id: str
    name: str
    status_active: bool
    # The MLX folder verbatim, staging buckets included ("Default folder",
    # "Banned", "logged out"). `model_name` is the same string *filtered* down
    # to the folders that name a real model -- they are two questions, and
    # "which folder is this phone in" must still be answerable for a phone
    # sitting in none of them.
    folder_name: str | None = None
    model_name: str | None = None
    time_zone: str | None = None
    phone_model_os: str | None = None
    sim_number: str | None = None
    proxy_endpoint: str | None = None
    proxy_location: str | None = None
    created_at: str | None = None
    # MLX's own labels for the profile ("Created", "Active / Posting",
    # "Issue", ...). The warm-up runs on the "Created" ones -- see
    # warmup_targets.py -- so this is the one field here that decides work
    # rather than describing a phone.
    tags: tuple = ()


@dataclass
class ProfilePlan:
    """One planned outcome for one MLX profile."""

    profile: NormalizedProfile
    action: str  # "create" | "update" | "unchanged"
    reason: str = ""
    record_id: str | None = None          # existing Airtable id, for updates
    updates: dict = field(default_factory=dict)  # the field patch, for updates
    # What each patched field held before, so the report can say what a change
    # replaced without re-reading Airtable or re-parsing `reason`.
    previous: dict = field(default_factory=dict)


@dataclass
class SyncPlan:
    to_create: list[ProfilePlan] = field(default_factory=list)
    to_update: list[ProfilePlan] = field(default_factory=list)
    unchanged: list[ProfilePlan] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (raw label, reason)

    def summary(self) -> str:
        return (
            f"create={len(self.to_create)} update={len(self.to_update)} "
            f"unchanged={len(self.unchanged)} skipped={len(self.skipped)}"
        )


# --- normalization --------------------------------------------------------

def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _model_from_serial_name(serial_name: str | None) -> str | None:
    """Fallback: strip trailing number / "(N)" / "Link" tokens off a serial_name
    to guess the model, e.g. "Nikki 1" -> "Nikki", "Blank 1 (6)" -> "Blank"."""
    if not serial_name:
        return None
    name = serial_name.strip()
    while True:
        stripped = _TRAILING_TOKEN.sub("", name).strip()
        if stripped == name:
            break
        name = stripped
    return name or None


def _folder_name_from(item: dict, folder_names: dict | None) -> str | None:
    """The MLX folder this profile sits in, verbatim -- no staging filter.

    Note this is `folder_id` resolved through the folders API, NOT the per-item
    `group.name`, which on this workspace is the workspace GUID and names
    nothing a person would recognise.
    """
    if not folder_names:
        return None
    folder_id = _clean(item.get("folder_id"))
    return _clean(folder_names.get(folder_id)) if folder_id else None


def _model_name_from(item: dict, folder_names: dict | None) -> str | None:
    """Resolve the profile's model from its MLX *folder* name (the authoritative
    grouping the UI uses). Generic buckets -> None. When no folder map is given,
    fall back to parsing the serial_name."""
    if folder_names:
        name = _folder_name_from(item, folder_names)
        if name and name.lower() not in _STAGING_FOLDERS:
            return name
        return None
    return _model_from_serial_name(_clean(item.get("serial_name")))


def _phone_model_os(equipment: dict) -> str | None:
    brand = _clean(equipment.get("device_brand"))
    model = _clean(equipment.get("device_model"))
    os_version = _clean(equipment.get("os_version"))
    device = " ".join(part for part in (brand, model) if part) or None
    if device and os_version:
        return f"{device} / {os_version}"
    return device or os_version


def _proxy_endpoint(proxy: dict) -> str | None:
    server = _clean(proxy.get("server"))
    port = _clean(proxy.get("port"))
    if server and port:
        return f"{server}:{port}"
    return server


def normalize_mlx_item(item: dict, folder_names: dict | None = None) -> NormalizedProfile | None:
    """Flatten one `/mobile_profiles/phone/list` item. Returns None (with the
    caller recording a skip) when the two keys the sync can't work without --
    `serial_no` and the 18-digit `id` -- are missing.

    `folder_names` maps folder_id -> folder name (from the MLX folders API); it's
    the authoritative source for a profile's model.
    """
    serial_no = _clean(item.get("serial_no"))
    api_id = _clean(item.get("id"))
    if not serial_no or not api_id:
        return None

    equipment = item.get("equipment_info") or {}
    proxy = item.get("proxy") or {}
    serial_name = _clean(item.get("serial_name"))

    return NormalizedProfile(
        serial_no=serial_no,
        api_id=api_id,
        name=serial_name or serial_no,
        status_active=(item.get("status") == MLX_STATUS_ACTIVE),
        folder_name=_folder_name_from(item, folder_names),
        model_name=_model_name_from(item, folder_names),
        time_zone=_clean(equipment.get("time_zone")),
        phone_model_os=_phone_model_os(equipment),
        sim_number=_clean(equipment.get("phone_number")),
        proxy_endpoint=_proxy_endpoint(proxy),
        proxy_location=_clean(equipment.get("country_name")),
        created_at=_clean(item.get("created_at")),
        # Sorted, and de-duplicated: this tuple is compared against Airtable's
        # to decide whether to write, so MLX returning the same set in a
        # different order must not read as a change every three hours.
        tags=tuple(sorted({t for t in (_clean(tag) for tag in (item.get("tags") or [])) if t})),
    )


# --- planning (pure) ------------------------------------------------------

def _duplicate_mlx_names(mlx_items: list[dict]) -> set:
    """Names MultiLogin itself uses more than once.

    Renaming Airtable onto one of these would put the same `Profile Name` on
    several rows, and that name is what variant files, queue rows and log lines
    are keyed by -- two rows called `Jasmin 11` would overwrite each other's
    spoofed clips. MultiLogin currently holds three `Jasmin 11` and two
    `jasmin`; that is a MultiLogin problem to fix in MultiLogin, so the sync
    leaves those Airtable names alone rather than propagating the collision.
    """
    counts: dict = {}
    for item in mlx_items or []:
        name = _clean((item or {}).get("serial_name"))
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {name for name, n in counts.items() if n > 1}


def _plan_reconcile(normalized: NormalizedProfile, existing: dict,
                    duplicate_names: set, reconcile: bool) -> tuple[dict, dict, list]:
    """The field patch that would make one existing Airtable row match MLX.

    Returns `(updates, previous, reasons)`; an empty patch means the row already
    matches. A reason with no matching entry in the patch is a *refusal* --
    something that differs and was deliberately not written -- so the log says
    why a mismatch survived instead of silently reporting the row as in sync.
    """
    updates: dict = {}
    previous: dict = {}
    reasons: list = []

    # --- backfill: write only into an empty field, never over a value -------
    if not existing.get("api_id") and normalized.api_id:
        updates[at.F_PROF_MLX_API_ID] = normalized.api_id
        reasons.append("backfill MLX API ID")
    if not existing.get("time_zone") and normalized.time_zone:
        updates[at.F_PROF_TIME_ZONE] = normalized.time_zone
        reasons.append("backfill Time Zone")

    if not reconcile:
        return updates, previous, reasons

    # --- reconcile: MLX wins ------------------------------------------------
    current_name = _clean(existing.get("name"))
    if normalized.name and normalized.name != current_name:
        if normalized.name in duplicate_names:
            reasons.append(f"name kept as {current_name!r}: MLX has several profiles "
                           f"named {normalized.name!r}")
        elif existing.get("duplicate"):
            reasons.append(f"name kept as {current_name!r}: several Airtable rows "
                           f"claim serial {normalized.serial_no}")
        else:
            updates[at.F_PROF_NAME] = normalized.name
            previous[at.F_PROF_NAME] = current_name
            reasons.append(f"rename {current_name or '-'} -> {normalized.name}")

    current_folder = _clean(existing.get("folder"))
    if normalized.folder_name != current_folder:
        # None becomes "" so a profile moved out of every folder is cleared
        # rather than keeping the folder it is no longer in.
        updates[at.F_PROF_MLX_FOLDER] = normalized.folder_name or ""
        previous[at.F_PROF_MLX_FOLDER] = current_folder
        reasons.append(f"folder {current_folder or '-'} -> {normalized.folder_name or '-'}")

    current_tags = tuple(existing.get("tags") or ())
    if normalized.tags != current_tags:
        updates[at.F_PROF_MLX_TAGS] = list(normalized.tags)
        previous[at.F_PROF_MLX_TAGS] = current_tags
        added = [f"+{t}" for t in normalized.tags if t not in current_tags]
        removed = [f"-{t}" for t in current_tags if t not in normalized.tags]
        reasons.append("tags " + (", ".join(added + removed) or "cleared"))

    return updates, previous, reasons


def plan_sync(mlx_items: list[dict], existing_by_serial: dict, folder_names: dict | None = None,
              skip_staging: bool = False, reconcile: bool = True) -> SyncPlan:
    """Diff the MLX profile list against what Airtable already has.

    `existing_by_serial`: serial_no -> {'record_id', 'name', 'api_id',
    'time_zone', 'folder', 'tags', 'duplicate'} (as returned by
    AirtableClient.profiles_by_serial()).
    `folder_names`: folder_id -> name, used to resolve each profile's folder and
    its model.
    `skip_staging`: don't sync profiles that resolve to no model (MLX's "Default
    folder" staging buckets, e.g. the unnamed "Blank" profiles). Existing rows
    are still reconciled; only *new* staging profiles are skipped.
    `reconcile`: bring `Profile Name`, `MLX Folder` and `MLX Tags` in line with
    MultiLogin. Turning it off degrades this pass to the backfill-only
    behaviour it had before -- an escape hatch for a run where MultiLogin itself
    looks wrong, not a mode anything should schedule.
    """
    plan = SyncPlan()
    seen: set[str] = set()
    duplicate_names = _duplicate_mlx_names(mlx_items) if reconcile else set()

    for item in mlx_items:
        normalized = normalize_mlx_item(item, folder_names)
        if normalized is None:
            label = _clean((item or {}).get("serial_name")) or _clean((item or {}).get("serial_no")) or "<unknown>"
            plan.skipped.append((label, "missing serial_no or 18-digit id"))
            continue

        if normalized.serial_no in seen:
            plan.skipped.append((normalized.name, f"duplicate serial_no {normalized.serial_no} in MLX response"))
            continue
        seen.add(normalized.serial_no)

        existing = existing_by_serial.get(normalized.serial_no)
        if existing is None:
            if skip_staging and not normalized.model_name:
                plan.skipped.append((normalized.name, "staging profile (no model folder)"))
                continue
            plan.to_create.append(ProfilePlan(normalized, "create", "new profile in MLX"))
            continue

        updates, previous, reasons = _plan_reconcile(normalized, existing, duplicate_names, reconcile)

        if updates:
            plan.to_update.append(
                ProfilePlan(
                    normalized,
                    "update",
                    "; ".join(reasons),
                    record_id=existing.get("record_id"),
                    updates=updates,
                    previous=previous,
                )
            )
        else:
            # `reasons` here can only hold refusals (a guarded rename), so an
            # unchanged row still carries the reason it stayed unchanged.
            plan.unchanged.append(ProfilePlan(
                normalized, "unchanged", "; ".join(reasons) or "already in sync",
                record_id=existing.get("record_id")))

    return plan


# --- field builders (pure) ------------------------------------------------

def _status_label(active: bool) -> str:
    return at.STATUS_SELECT_ACTIVE if active else at.STATUS_SELECT_INACTIVE


def build_device_fields(p: NormalizedProfile, model_id: str | None) -> dict:
    fields: dict = {at.F_DEV_DEVICE_ID: p.name, at.F_DEV_STATUS: _status_label(p.status_active)}
    if p.phone_model_os:
        fields[at.F_DEV_PHONE_MODEL_OS] = p.phone_model_os
    if p.sim_number:
        fields[at.F_DEV_SIM_NUMBER] = p.sim_number
    if model_id:
        fields[at.F_DEV_MODEL] = [model_id]
    return fields


def build_proxy_fields(p: NormalizedProfile, device_id: str) -> dict:
    fields: dict = {
        at.F_PROX_ID: p.proxy_endpoint or f"{p.name} proxy",
        at.F_PROX_PROVIDER: PROXY_PROVIDER,
        at.F_PROX_STATUS: _status_label(p.status_active),
        at.F_PROX_ASSIGNED_DEVICE: [device_id],
    }
    if p.proxy_endpoint:
        fields[at.F_PROX_ENDPOINT] = p.proxy_endpoint
    if p.proxy_location:
        fields[at.F_PROX_LOCATION] = p.proxy_location
    return fields


def build_profile_fields(p: NormalizedProfile, device_id: str) -> dict:
    fields: dict = {
        at.F_PROF_NAME: p.name,
        at.F_PROF_MLX_SERIAL: p.serial_no,
        at.F_PROF_MLX_API_ID: p.api_id,
        at.F_PROF_STATUS: _status_label(p.status_active),
        at.F_PROF_APP_PACKAGE: IG_PACKAGE,
        at.F_PROF_DEVICE: [device_id],
    }
    if p.time_zone:
        fields[at.F_PROF_TIME_ZONE] = p.time_zone
    # Set on creation as well as on reconcile, so a new row is not born stale
    # and waiting three hours for the next pass to describe it.
    if p.folder_name:
        fields[at.F_PROF_MLX_FOLDER] = p.folder_name
    if p.tags:
        fields[at.F_PROF_MLX_TAGS] = list(p.tags)
    return fields


# --- apply ----------------------------------------------------------------

@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)      # profile names created
    updated: list[str] = field(default_factory=list)      # profile names patched
    unchanged: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (name, message)
    unmatched_models: set = field(default_factory=set)     # model names with no Models row
    dry_run: bool = True
    # Broken out by kind, because "updated=62" says nothing about whether this
    # pass renamed 62 phones or only filled in 62 blank folders -- and a rename
    # is the one change here with a consequence downstream (see `renamed_model`).
    renamed: list[tuple[str, str]] = field(default_factory=list)   # (old name, new name)
    refoldered: list[tuple[str, str]] = field(default_factory=list)  # (name, folder)
    retagged: list[str] = field(default_factory=list)              # profile names
    # Renames that change the profile's *first word*. That word is the model
    # `profile_targets_by_model` routes content by, so these are the renames
    # that move a phone from one model's content to another's -- always worth a
    # line in the log even when the rename itself is correct.
    renamed_model: list[tuple[str, str]] = field(default_factory=list)
    # Mismatches the sync saw and deliberately did not write (guarded renames).
    refused: list[tuple[str, str]] = field(default_factory=list)   # (name, reason)

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        return (
            f"[{mode}] created={len(self.created)} updated={len(self.updated)} "
            f"(renamed={len(self.renamed)} refoldered={len(self.refoldered)} "
            f"retagged={len(self.retagged)}) unchanged={self.unchanged} "
            f"skipped={len(self.skipped)} errors={len(self.errors)}"
        )


def apply_sync(client, plan: SyncPlan, models_by_name: dict, dry_run: bool = True) -> SyncReport:
    """Execute (or, when dry_run, just report) a SyncPlan against Airtable.

    Creates are done Device -> Proxy -> Profile so the child rows can link to the
    Device; Airtable auto-populates the Device's reverse links. A failed create
    is recorded as an error and does not abort the rest of the run.
    """
    report = SyncReport(dry_run=dry_run)
    report.unchanged = len(plan.unchanged)
    report.skipped = list(plan.skipped)
    # A guarded rename lands in `unchanged` with its reason attached; surface it
    # rather than letting "already in sync" cover a mismatch we chose to keep.
    for item in plan.unchanged:
        if item.reason and item.reason != "already in sync":
            report.refused.append((item.profile.name, item.reason))

    for item in plan.to_create:
        p = item.profile
        model_id = None
        if p.model_name:
            model_id = models_by_name.get(p.model_name.lower())
            if not model_id:
                report.unmatched_models.add(p.model_name)

        if dry_run:
            report.created.append(p.name)
            continue

        device_id = client.create_device(build_device_fields(p, model_id))
        if not device_id:
            report.errors.append((p.name, "device create failed"))
            continue
        # Proxy links to the device; ignore a proxy failure (device+profile still useful).
        client.create_proxy(build_proxy_fields(p, device_id))
        profile_id = client.create_profile(build_profile_fields(p, device_id))
        if not profile_id:
            report.errors.append((p.name, "profile create failed (device was created)"))
            continue
        report.created.append(p.name)

    for item in plan.to_update:
        p = item.profile
        if not dry_run and not (item.record_id and client.update_profile(item.record_id, item.updates)):
            report.errors.append((p.name, "profile update failed"))
            continue
        report.updated.append(p.name)
        _record_update_kinds(report, item)

    return report


def _first_word(name: str | None) -> str:
    return (name or "").strip().split(" ")[0].lower()


def _record_update_kinds(report: SyncReport, item: ProfilePlan) -> None:
    """Tally one applied patch by what it actually changed."""
    p = item.profile
    new_name = item.updates.get(at.F_PROF_NAME)
    if new_name:
        old_name = item.previous.get(at.F_PROF_NAME) or "-"
        report.renamed.append((old_name, new_name))
        if _first_word(old_name) != _first_word(new_name):
            report.renamed_model.append((old_name, new_name))
    if at.F_PROF_MLX_FOLDER in item.updates:
        report.refoldered.append((new_name or p.name, item.updates[at.F_PROF_MLX_FOLDER] or "-"))
    if at.F_PROF_MLX_TAGS in item.updates:
        report.retagged.append(new_name or p.name)
