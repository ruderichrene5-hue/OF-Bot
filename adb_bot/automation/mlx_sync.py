"""MultiLogin -> Airtable profile sync (checklist loop #4).

MultiLogin is the source of truth for which mobile profiles exist; Airtable only
learns about a new profile when this loop diffs the two and writes the missing
rows. It never talks to a phone and never touches Instagram -- it's pure data
plumbing meant to run unattended (Windows Task Scheduler) about once a day.

Design:
- normalize each MLX `list` item into a small, tolerant dataclass;
- diff against the existing Profiles (Cloning) rows, keyed by the human
  `serial_no` (`MultiLogin Profile ID`), which is the stable match key --
  NOT the 18-digit `id`, which is the launch key we *store* but don't match on;
- for a profile MLX has and Airtable doesn't: create a Device, a Proxy, and a
  Profile (Cloning) row, linked together (and to its Model when the name
  matches an existing Models row);
- for a profile Airtable already has but that's missing its 18-digit `MLX API
  ID` or `Time Zone`: fill those in (never overwrite an existing value);
- and keep `MLX Folder` in step with MultiLogin, which unlike the two above
  is a mirror rather than a backfill: a profile moved from the staging
  bucket into a model's folder should stop claiming to be in staging.

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
    model_name: str | None = None
    # The MLX folder verbatim, staging buckets included. `model_name` above
    # is derived from it and is None for those buckets, so the two are not
    # interchangeable.
    folder_name: str | None = None
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
    updates: dict = field(default_factory=dict)  # fill-only field patch, for updates


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
    """The profile's MLX folder name, verbatim, or None if it cannot be resolved.

    The difference from `_model_name_from` below is the whole point of storing
    both: that one answers "which model is this?" and deliberately returns None
    for a staging bucket, because a `Blank` profile in "Default folder" has no
    model. This one answers "where does this phone live in MultiLogin?", and
    "Default folder" is a perfectly good answer to that -- in fact it is the
    interesting one, since it is where the 60-odd unfinished profiles sit.
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
        folder_id = _clean(item.get("folder_id"))
        name = _clean(folder_names.get(folder_id)) if folder_id else None
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
        model_name=_model_name_from(item, folder_names),
        folder_name=_folder_name_from(item, folder_names),
        time_zone=_clean(equipment.get("time_zone")),
        phone_model_os=_phone_model_os(equipment),
        sim_number=_clean(equipment.get("phone_number")),
        proxy_endpoint=_proxy_endpoint(proxy),
        proxy_location=_clean(equipment.get("country_name")),
        created_at=_clean(item.get("created_at")),
        tags=tuple(t for t in (_clean(tag) for tag in (item.get("tags") or [])) if t),
    )


# --- planning (pure) ------------------------------------------------------

def plan_sync(mlx_items: list[dict], existing_by_serial: dict, folder_names: dict | None = None,
              skip_staging: bool = False) -> SyncPlan:
    """Diff the MLX profile list against what Airtable already has.

    `existing_by_serial`: serial_no -> {'record_id', 'api_id', 'time_zone', ...}
    (as returned by AirtableClient.profiles_by_serial()).
    `folder_names`: folder_id -> name, used to resolve each profile's model.
    `skip_staging`: don't sync profiles that resolve to no model (MLX's "Default
    folder" staging buckets, e.g. the unnamed "Blank" profiles). Existing rows
    are still backfilled; only *new* staging profiles are skipped.
    """
    plan = SyncPlan()
    seen: set[str] = set()

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

        # Fill-only backfill of the launch key / time zone on an existing row.
        updates: dict = {}
        if not existing.get("api_id") and normalized.api_id:
            updates[at.F_PROF_MLX_API_ID] = normalized.api_id
        if not existing.get("time_zone") and normalized.time_zone:
            updates[at.F_PROF_TIME_ZONE] = normalized.time_zone
        # The folder tracks *changes*, unlike the two above, and the difference is
        # deliberate. Those two are fill-only because a value already in Airtable
        # may have been put there by a person and is not ours to overwrite. The
        # folder is not like that: it is a mirror of where the phone sits in
        # MultiLogin, nobody maintains it by hand, and profiles genuinely move --
        # "Default folder" -> "Nikki" is what onboarding a staging phone looks
        # like, and a column that could show the old folder forever would be
        # worse than no column. Only a folder we could actually resolve counts;
        # a missing folder map must not blank a good value.
        if normalized.folder_name and existing.get("folder") != normalized.folder_name:
            updates[at.F_PROF_MLX_FOLDER] = normalized.folder_name

        if updates:
            plan.to_update.append(
                ProfilePlan(
                    normalized,
                    "update",
                    "backfill " + ", ".join(sorted(updates)),
                    record_id=existing.get("record_id"),
                    updates=updates,
                )
            )
        else:
            plan.unchanged.append(ProfilePlan(normalized, "unchanged", "already in sync", record_id=existing.get("record_id")))

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
    if p.folder_name:
        fields[at.F_PROF_MLX_FOLDER] = p.folder_name
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

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLIED"
        return (
            f"[{mode}] created={len(self.created)} updated={len(self.updated)} "
            f"unchanged={self.unchanged} skipped={len(self.skipped)} errors={len(self.errors)}"
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
        if dry_run:
            report.updated.append(p.name)
            continue
        if item.record_id and client.update_profile(item.record_id, item.updates):
            report.updated.append(p.name)
        else:
            report.errors.append((p.name, "profile update failed"))

    return report
