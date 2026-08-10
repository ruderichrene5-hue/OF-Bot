"""Cross-system model inventory: who exists where, and who is only half here.

Onboarding a model touches four systems -- a Google Drive raw folder, a
MultiLogin folder full of profiles, the `Profiles (Cloning)` names those
profiles sync into, and (optionally) a `Models` row. Nothing checked that they
agreed, so a model could be half-onboarded for days and the only symptom was a
number that did not move: on 2026-08-10 fifteen profiles across two MLX folders
("Kathi", "Katherine") had been created and warmed up for six days while being
invisible to every posting code path, because their Airtable names were still
`Blank (N)` and their raw folders were empty.

The point of this module: **a model that is present in one system and absent
from another is a finding, not silence.** It is pure -- callers hand it the four
lists, it returns `Finding`s -- so the doctor check is a thin wrapper and the
rules are testable without a token.

What it deliberately does NOT do: write anything, rename anything, or guess
which system is right. Every finding names the manual step that closes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# MLX folders that are buckets, not models. Mirrors `mlx_sync._STAGING_FOLDERS`
# (imported below so the two can never drift) plus the ones only a human would
# recognise as scratch space.
EXTRA_NON_MODEL_FOLDERS = {"caio tests", "ig i outseeker outreach"}

# Airtable profile-name prefixes that are not a model either. `blank` is MLX
# staging leaking into the target pool; it is a known condition, not news.
NON_MODEL_TARGET_KEYS = {"blank", "unnamed", ""}

# Severity, matching doctor's vocabulary so the check can map straight across.
INFO = "INFO"
WARN = "WARN"


@dataclass
class Finding:
    model: str
    kind: str           # machine-readable: mlx_folder_unnamed / no_targets / ...
    detail: str         # one line, for a log or the doctor report
    hint: str = ""      # the manual step that closes it
    severity: str = WARN


@dataclass
class Inventory:
    """Everything the diff needs, already normalised by the caller."""
    raw_folders: list = field(default_factory=list)        # Drive/local folder names
    mlx_folders: dict = field(default_factory=dict)        # folder name -> [profile names]
    target_keys: dict = field(default_factory=dict)        # model key -> target count
    model_rows: set = field(default_factory=set)           # lower-cased Models names
    aliases: dict = field(default_factory=dict)            # raw folder -> model


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _staging_folders() -> set:
    try:
        from adb_bot.automation.mlx_sync import _STAGING_FOLDERS

        staging = set(_STAGING_FOLDERS)
    except Exception:
        staging = {"default folder", "unnamed", ""}
    return staging | EXTRA_NON_MODEL_FOLDERS


def diff_models(inv: Inventory) -> list:
    """Every place the four systems disagree about which models exist.

    Four rules, each chosen to be quiet on a healthy inventory:

    1. **An MLX folder no profile inside it is named after.** This is the
       earliest and most specific signal a new model exists: the operator makes
       the folder and clones profiles into it, and the profiles keep their
       `Blank (N)` names until somebody renames them. Compared case-insensitively
       (the live workspace has a folder spelled "NIkki").
    2. **A raw folder with no targets.** Clips can be dropped in and nothing will
       ever pick them up. Fires for empty folders too -- that is the state a
       brand-new model's folder is in.
    3. **A target model with no raw folder.** Profiles exist and are eligible to
       post; no content will ever be made for them.
    4. **A model known to MLX/Drive/targets with no `Models` row.** Lowest
       severity: posting works without one (`queue_runner` treats a missing
       schedule as flexible mode), but the dashboard calls such a model "stray"
       and `mlx_sync` cannot link its Devices.
    """
    staging = _staging_folders()
    aliases = {_norm(k): str(v) for k, v in (inv.aliases or {}).items()}
    findings: list = []

    def resolved(folder: str) -> str:
        return aliases.get(_norm(folder), folder)

    # --- 1. an MLX folder whose profiles are not named after it ---------------
    for folder, profile_names in sorted((inv.mlx_folders or {}).items()):
        if _norm(folder) in staging or not profile_names:
            continue
        matching = [n for n in profile_names if _norm(str(n).split()[0] if str(n).split() else "") == _norm(folder)]
        if matching:
            continue
        findings.append(Finding(
            model=folder, kind="mlx_folder_unnamed",
            detail=(f"MultiLogin folder '{folder}' holds {len(profile_names)} profile(s), "
                    f"none named after it (e.g. {str(profile_names[0])!r})"),
            hint=(f"Rename those profiles to '{folder} 1'..'{folder} N' in MultiLogin AND on the "
                  f"matching Profiles (Cloning) rows -- mlx-sync only writes Profile Name on "
                  f"create, so a rename in MLX alone never reaches Airtable."),
        ))

    # --- 2. a raw folder with no targets -------------------------------------
    target_keys = {_norm(k): int(v or 0) for k, v in (inv.target_keys or {}).items()}
    for folder in sorted(inv.raw_folders or []):
        model = resolved(folder)
        if target_keys.get(_norm(model)):
            continue
        via = "" if _norm(model) == _norm(folder) else f" (aliased to '{model}')"
        findings.append(Finding(
            model=model, kind="raw_folder_no_targets",
            detail=f"raw folder '{folder}'{via} has no active profile to spoof for",
            hint=(f"Either no profile is named '{model} N' with Status Active in "
                  f"Profiles (Cloning), or the folder is named for something else -- set "
                  f"RAW_FOLDER_MODEL_ALIASES=\"{_norm(folder)}=<Model>\" if so."),
        ))

    # --- 3. targets with no raw folder ---------------------------------------
    raw_models = {_norm(resolved(f)) for f in (inv.raw_folders or [])}
    for key, count in sorted(target_keys.items()):
        if not count or key in NON_MODEL_TARGET_KEYS or key in raw_models:
            continue
        findings.append(Finding(
            model=key, kind="targets_no_raw_folder",
            detail=f"{count} active profile(s) under '{key}' but no raw video folder",
            hint=(f"Create 01_Raw_Videos/{key.capitalize()}/ (or add an alias if its clips "
                  f"live in a differently-named folder). Until then this model can never "
                  f"be given content."),
        ))

    # --- 4. no Models row ----------------------------------------------------
    rows = {_norm(m) for m in (inv.model_rows or set())}
    seen = set()
    for name in ([resolved(f) for f in (inv.raw_folders or [])]
                 + [f for f in (inv.mlx_folders or {}) if _norm(f) not in staging]
                 + [k for k, c in target_keys.items() if c and k not in NON_MODEL_TARGET_KEYS]):
        key = _norm(name)
        if not key or key in rows or key in seen:
            continue
        seen.add(key)
        findings.append(Finding(
            model=str(name), kind="no_models_row",
            detail=f"'{name}' has profiles and/or raw content but no Models row",
            severity=INFO,
            hint=("Posting still works (a model with no Models row runs in flexible mode), "
                  "but the dashboard reports it as stray and mlx-sync cannot link its Device."),
        ))

    return findings


def summarise(findings: list) -> str:
    """One line per finding kind, for a doctor detail string."""
    if not findings:
        return "every model lines up across Drive, MultiLogin and Airtable"
    warns = [f for f in findings if f.severity == WARN]
    parts = [f"{f.model} ({f.kind})" for f in (warns or findings)]
    return f"{len(warns)} gap(s), {len(findings) - len(warns)} note(s): " + ", ".join(parts)


def collect(airtable=None, mlx_profiles=None, mlx_folders=None,
            raw_source=None, aliases=None) -> Inventory:
    """Build an :class:`Inventory` from live clients, tolerating missing ones.

    Every source is optional: a box with no MLX token still gets the Drive vs
    Airtable half of the answer rather than no answer.
    """
    from adb_bot.automation import spoof_pipeline

    inv = Inventory(aliases=dict(aliases or spoof_pipeline.raw_folder_model_aliases()))

    if raw_source is not None:
        lister = getattr(raw_source, "list_folder_names", None)
        if callable(lister):
            inv.raw_folders = list(lister())
        else:  # a source predating list_folder_names: folders with work only
            inv.raw_folders = list(raw_source.list_by_model().keys())

    if mlx_profiles is not None:
        names = {str(f.get("folder_id")): str(f.get("name") or "")
                 for f in (mlx_folders or []) if f.get("folder_id")}
        grouped: dict = {}
        for item in mlx_profiles:
            folder = names.get(str(item.get("folder_id")), "")
            if not folder:
                continue
            # `serial_name` is the MLX profile name ("Kathi 3", "Blank (1)");
            # `name` is only a fallback for already-normalised inputs.
            grouped.setdefault(folder, []).append(
                str(item.get("serial_name") or item.get("name") or ""))
        inv.mlx_folders = grouped

    if airtable is not None:
        inv.target_keys = {key: len(targets)
                           for key, targets in (airtable.profile_targets_by_model() or {}).items()}
        inv.model_rows = set((airtable.models_by_name() or {}).keys())

    return inv
