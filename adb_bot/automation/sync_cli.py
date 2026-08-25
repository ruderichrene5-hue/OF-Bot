"""Headless entrypoint for the MultiLogin -> Airtable profile sync (loop #4).

Runs unattended every 3 hours (`adbbot-mlx-sync.timer`). Dry-run by default --
it prints exactly what it *would* write and touches nothing until `--apply` is
passed, matching the "dry-run first" guidance in MLX_SYNC_FINDINGS.md.

It creates a row for a profile Airtable has never seen, and reconciles the ones
it has: `Profile Name`, `MLX Folder` and `MLX Tags` are made to match
MultiLogin every run. `Status` is never synced -- see mlx_sync.py's header.

Usage (from the repo root, with the venv active):
    python -m adb_bot.automation.sync_cli            # dry-run, prints the plan
    python -m adb_bot.automation.sync_cli --apply    # actually writes to Airtable

Credentials are read the same way the UI reads them (saved dev settings, then
env override):
    MULTILOGIN_TOKEN / bearer_token   -- MLX token (use the workspace
                                         Automation Token for an unattended run)
    AIRTABLE_TOKEN / airtable_token   -- Airtable PAT
    AIRTABLE_BASE_ID / airtable_base_id
Or pass --mlx-token / --airtable-token / --base-id to override.
"""

from __future__ import annotations

import argparse
import os
import sys

from adb_bot.clients import airtable as at
from adb_bot.clients.airtable import AirtableClient
from adb_bot.clients.multilogin.mobile_list import MultiloginMobileListClient
from adb_bot.clients.multilogin.folders import MultiloginFolderClient
from adb_bot.config import settings
from adb_bot.automation import mlx_sync


def _resolve_mlx_token(cli_value: str | None) -> str:
    return (
        (cli_value or "").strip()
        or (os.environ.get("MULTILOGIN_TOKEN", "") or "").strip()
        or settings.get_saved_bearer_token()
    )


def _print_plan(plan: mlx_sync.SyncPlan) -> None:
    print(f"\nPlan: {plan.summary()}")
    if plan.to_create:
        print(f"\n  New profiles to create ({len(plan.to_create)}):")
        for item in plan.to_create:
            p = item.profile
            model = p.model_name or "(no model match)"
            print(f"    + {p.name}  serial={p.serial_no}  model={model}  tz={p.time_zone or '-'}")
    if plan.to_update:
        print(f"\n  Existing profiles to reconcile ({len(plan.to_update)}):")
        for item in plan.to_update:
            print(f"    ~ {item.profile.serial_no}  {item.reason}")
    refusals = [i for i in plan.unchanged if i.reason and i.reason != "already in sync"]
    if refusals:
        print(f"\n  Differences deliberately not written ({len(refusals)}):")
        for item in refusals:
            print(f"    = {item.profile.serial_no}  {item.reason}")
    if plan.skipped:
        print(f"\n  Skipped ({len(plan.skipped)}):")
        for label, reason in plan.skipped:
            print(f"    - {label}: {reason}")


def run_sync(mlx_token: str, airtable_token: str, base_id: str, dry_run: bool = True,
             skip_staging: bool = False, reconcile: bool = True) -> mlx_sync.SyncReport:
    mlx_client = MultiloginMobileListClient(mlx_token)
    airtable = AirtableClient(airtable_token, base_id, at.TABLE_PROFILES)

    print(f"[sync] Base {base_id} | mode {'DRY-RUN' if dry_run else 'APPLY'}")
    items = mlx_client.list_mobile_profiles()
    folders = MultiloginFolderClient(mlx_token).list_mobile_folders()
    folder_names = {str(f.get("folder_id")): f.get("name") for f in folders if f.get("folder_id")}
    print(f"[sync] MLX returned {len(items)} profile(s) across {len(folder_names)} folder(s)")

    existing = airtable.profiles_by_serial()
    models = airtable.models_by_name()
    print(f"[sync] Airtable has {len(existing)} profile(s) and {len(models)} model(s)")

    plan = mlx_sync.plan_sync(items, existing, folder_names, skip_staging=skip_staging,
                              reconcile=reconcile)
    _print_plan(plan)

    report = mlx_sync.apply_sync(airtable, plan, models, dry_run=dry_run)

    print(f"\n{report.summary()}")
    if report.unmatched_models:
        print(f"[warn] no Models row matched: {', '.join(sorted(report.unmatched_models))} "
              f"(devices created without a Model link)")
    if report.errors:
        print("[error] failed rows:")
        for name, message in report.errors:
            print(f"    ! {name}: {message}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync MultiLogin mobile profiles into Airtable.")
    parser.add_argument("--apply", action="store_true", help="Write to Airtable (default is a dry-run).")
    parser.add_argument("--skip-staging", action="store_true",
                        help="Don't create rows for staging profiles that belong to no model "
                             "(MLX's 'Default folder', e.g. the unnamed Blank profiles).")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="Only backfill blank fields; leave Profile Name, MLX Folder and "
                             "MLX Tags as Airtable has them. For a run where MultiLogin itself "
                             "looks wrong -- the scheduled sync should not use this.")
    parser.add_argument("--mlx-token", default=None, help="MultiLogin bearer/automation token.")
    parser.add_argument("--airtable-token", default=None, help="Airtable Personal Access Token.")
    parser.add_argument("--base-id", default=None, help="Airtable base id (defaults to saved/test base).")
    args = parser.parse_args(argv)

    mlx_token = _resolve_mlx_token(args.mlx_token)
    airtable_token = (args.airtable_token or "").strip() or settings.get_saved_airtable_token()
    base_id = (args.base_id or "").strip() or settings.get_saved_airtable_base_id()

    if not mlx_token:
        print("[fatal] No MultiLogin token (set MULTILOGIN_TOKEN, save it in dev settings, or pass --mlx-token).")
        return 2
    if not airtable_token:
        print("[fatal] No Airtable token (set AIRTABLE_TOKEN, save it in dev settings, or pass --airtable-token).")
        return 2

    try:
        report = run_sync(mlx_token, airtable_token, base_id, dry_run=not args.apply,
                          skip_staging=args.skip_staging, reconcile=not args.no_reconcile)
    except Exception as exc:  # top-level guard: an unattended run should exit non-zero, not traceback silently
        print(f"[fatal] sync failed: {exc}")
        return 1

    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
