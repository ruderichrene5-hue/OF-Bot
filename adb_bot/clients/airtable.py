from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

# Airtable field names the app reads/writes. Kept here so a base with slightly
# different column names only needs editing in one place.
FIELD_PROFILE_ID = "Multilogin Profile ID"
FIELD_ACCOUNT = "Account"
FIELD_FLOW = "Flow"
FIELD_BIO = "Bio"
FIELD_CAPTION = "Caption"
FIELD_STATUS = "Status"
FIELD_LAST_RESULT = "Last Result"
FIELD_LAST_RUN = "Last Run"
FIELD_NOTES = "Notes"

# Status values (the control field).
STATUS_READY = "Ready"
STATUS_RUNNING = "Running"
STATUS_DONE = "Done"
STATUS_SKIPPED = "Skipped"
STATUS_FAILED = "Failed"


# --- Normalized base (OFM Agency OS + its test clone) --------------------
# Table names are stable across the production base and its clone, so the
# lifecycle orchestrator keys off names rather than base-specific table IDs.
API_BASE = "https://api.airtable.com/v0"

TABLE_ACCOUNTS = "Accounts"
TABLE_PROFILES = "Profiles (Cloning)"
TABLE_DEVICES = "Devices"
TABLE_PROXIES = "Proxies"
TABLE_MODELS = "Models"
TABLE_RUN_LOG = "Run Log"
TABLE_BAN_HISTORY = "Ban & Flag History"
TABLE_POSTING_QUEUE = "Posting Queue"

# Accounts
F_ACC_NAME = "Name"
F_ACC_AUTOMATION_MODE = "Automation Mode"
F_ACC_LIFECYCLE_STAGE = "Lifecycle Stage"
F_ACC_CREATION_DATE = "Creation Date"
F_ACC_NEEDS_VERIFICATION = "Needs Human Verification"
F_ACC_PROFILE = "Profile"
F_ACC_BIO = "Bio"
F_ACC_PROFILE_PICTURE = "Profile Picture"   # multipleAttachments
F_ACC_BAN_NOTES = "Ban / Flag Notes"
F_ACC_LAST_RUN = "Last Run"
F_ACC_LAST_RESULT = "Last Result"

# Ban & Flag History (one row per ban/flag/verification incident)
F_BAN_DATE = "Date"
F_BAN_EVENT_TYPE = "Event Type"
F_BAN_NOTES = "Notes"
F_BAN_RESOLVED = "Resolved"
F_BAN_ACCOUNT = "Account"

# Ban & Flag History Event Type values
EVENT_SHADOWBAN = "Shadowban"
EVENT_FULL_BAN = "Full Ban"
EVENT_ACTION_BLOCK = "Action Block"
EVENT_WARNING = "Warning"

# Posting Queue (the main posting loop, checklist #1)
F_PQ_NAME = "Name"
F_PQ_SCHEDULED = "Scheduled DateTime"
F_PQ_ISSUE_TYPE = "Issue Type"
F_PQ_POST_STATUS = "Post Status"
F_PQ_RETRY_COUNT = "Retry Count"
F_PQ_CAPTION = "Caption"                 # link -> Caption Pool
F_PQ_SPOOF_VARIANT = "Spoof Variant"     # link -> Spoof Variants
F_PQ_TARGET_ACCOUNT = "Target Account"   # link -> Accounts

# Posting Queue Issue Type / Post Status values
ISSUE_NONE = "None"
ISSUE_NEEDS_RETRY = "Failed - Needs Retry"
ISSUE_BANNED_BLOCKED = "Banned / Blocked"
ISSUE_HUMAN_VERIFICATION = "Human Verification Required"
ISSUE_OTHER = "Other"
F_PQ_NOTES = "Notes"
F_PQ_RECHECK_AFTER = "Recheck After"     # dateTime; when the deferred pass may look

POST_STATUS_PENDING = "Pending"
POST_STATUS_POSTED = "Posted"
POST_STATUS_FAILED = "Failed"
# Share was tapped but the post could not be proven inside the run's budget.
# Deliberately NOT Failed: an unproven post is very often a live one, and
# marking it Failed is what got the same reel posted twice. The row waits here
# until Recheck After passes and the deferred pass resolves it either way.
POST_STATUS_VERIFYING = "Verifying"

# How long an unproven post waits before the deferred check looks at it. Long
# enough that Instagram has finished processing and the profile counter has
# refreshed, so the recheck gets a clean answer rather than racing the upload.
RECHECK_DELAY_SECONDS = 15 * 60

# Caption Pool
TABLE_CAPTION_POOL = "Caption Pool"
F_CAP_TEXT = "Caption Text"

# Spoof Variants (one spoofed video per source content + target account)
TABLE_SPOOF_VARIANTS = "Spoof Variants"
F_SV_VARIANT_ID = "Variant ID"
F_SV_FILE_PATH = "Spoofed File Path"
F_SV_METHOD = "Spoof Method / Script Version"
F_SV_STATUS = "Status"
F_SV_CREATED_DATE = "Created Date"
F_SV_SOURCE_CONTENT = "Source Content"   # link -> Content Pipeline
F_SV_TARGET_ACCOUNT = "Target Account"   # link -> Accounts
SV_STATUS_PENDING = "Pending"
SV_STATUS_READY = "Ready"
SV_STATUS_USED = "Used"
SV_STATUS_FAILED = "Failed"

# Content Pipeline (raw videos per model, spoof status)
TABLE_CONTENT_PIPELINE = "Content Pipeline"
F_CP_NAME = "Name"
F_CP_STATUS = "Status"
F_CP_RAW_LINK = "Raw Drive Link"
F_CP_SPOOF_STATUS = "Spoof Status"
F_CP_MODEL = "Model"                     # link -> Models
CP_STATUS_DONE = "Done"
CP_SPOOF_NEEDS = "Needs Spoofing"
CP_SPOOF_SPOOFED = "Spoofed"
CP_SPOOF_FAILED = "Failed"

# Accounts (extra links used by the pipeline)
F_ACC_MODEL = "Model"                    # link -> Models
STAGE_ACTIVE = "Active"

# Profiles (Cloning)
F_PROF_NAME = "Profile Name"
F_PROF_MLX_API_ID = "MLX API ID"          # 18-digit launch/ADB key
F_PROF_MLX_SERIAL = "MultiLogin Profile ID"  # human serial (not the launch key)
F_PROF_TIME_ZONE = "Time Zone"            # equipment_info.time_zone, e.g. Europe/Berlin
F_PROF_STATUS = "Status"                  # singleSelect: Active / Inactive
F_PROF_APP_PACKAGE = "App Package Name"   # constant com.instagram.android for these
F_PROF_DEVICE = "Device"                  # link -> Devices

# Devices
F_DEV_DEVICE_ID = "Device ID"
F_DEV_PHONE_MODEL_OS = "Phone Model / OS"
F_DEV_SIM_NUMBER = "SIM Number"
F_DEV_STATUS = "Status"                   # singleSelect: Active / Inactive / Burned
F_DEV_MODEL = "Model"                     # link -> Models

# Proxies
F_PROX_ID = "Proxy ID"
F_PROX_PROVIDER = "Provider"
F_PROX_ENDPOINT = "IP / Endpoint"
F_PROX_LOCATION = "Location / Geo"
F_PROX_STATUS = "Status"                  # singleSelect: Active / Rotated / Inactive
F_PROX_ASSIGNED_DEVICE = "Assigned Device"  # link -> Devices

# Models
F_MODEL_NAME = "Model Name"

# singleSelect status labels shared across the three synced tables
STATUS_SELECT_ACTIVE = "Active"
STATUS_SELECT_INACTIVE = "Inactive"

# Run Log
F_RUN_NAME = "Name"
F_RUN_ACCOUNT = "Account"
F_RUN_FLOW = "Flow"
F_RUN_RESULT = "Result"
F_RUN_AT = "Run At"
F_RUN_NOTES = "Notes"

# Guard values
MODE_PAUSED = "Paused"
STAGE_PAUSED = "Paused"
STAGE_BANNED = "Banned"

# Run Log Result values
RESULT_RUNNING = "Running"
RESULT_DONE = "Done"
RESULT_SKIPPED = "Skipped"
RESULT_FAILED = "Failed"
# Sent, outcome unknown. Kept distinct from both Done and Failed so the Run Log
# shows honestly how often we post blind -- if this dominates, verification is
# broken, which a Failed/Done split would hide.
RESULT_UNVERIFIED = "Unverified"


class AirtableClient:
    """Minimal Airtable Web API client for reading the profile queue and writing
    results back. Auth is a Personal Access Token (PAT)."""

    def __init__(self, token: str, base_id: str, table_name: str) -> None:
        self.token = token
        self.base_id = base_id
        self.table_name = table_name

    @property
    def _table_url(self) -> str:
        # The table name goes in the path and may contain spaces/specials.
        return f"https://api.airtable.com/v0/{self.base_id}/{urllib.parse.quote(self.table_name)}"

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def list_ready_records(self) -> list[dict]:
        """Return all records whose Status is Ready as [{'id', 'fields'}, ...]."""
        formula = f"{{{FIELD_STATUS}}}='{STATUS_READY}'"
        params = {"filterByFormula": formula, "pageSize": 100}
        records: list[dict] = []
        offset = None
        while True:
            if offset:
                params["offset"] = offset
            print(f"[HTTP] GET {self._table_url} filter={formula}")
            response = requests.get(self._table_url, headers=self._headers, params=params, timeout=30)
            print(f"[HTTP] Status: {response.status_code}")
            response.raise_for_status()
            payload = response.json()
            records.extend(payload.get("records", []) or [])
            offset = payload.get("offset")
            if not offset:
                break
        return records

    def update_record(self, record_id: str, fields: dict) -> bool:
        """PATCH a single record's fields. Returns True on success; never raises
        (a write failure must not abort an in-progress automation run)."""
        url = f"{self._table_url}/{record_id}"
        try:
            print(f"[HTTP] PATCH {url} fields={fields}")
            response = requests.patch(url, headers=self._headers, json={"fields": fields}, timeout=30)
            print(f"[HTTP] Status: {response.status_code}")
            response.raise_for_status()
            return True
        except Exception as exc:  # pragma: no cover - network/diagnostic path
            print(f"[-] Airtable update_record failed for {record_id}: {exc}")
            return False

    # ------------------------------------------------------------------
    # Normalized-schema access (used by the lifecycle orchestrator). The
    # flat list_ready_records/update_record above are legacy; these read
    # Accounts + Profiles (Cloning) and write a Run Log + per-account
    # Last Run / Last Result.
    # ------------------------------------------------------------------
    def _url_for_table(self, table: str) -> str:
        return f"{API_BASE}/{self.base_id}/{urllib.parse.quote(table)}"

    def _list_table(self, table: str, fields: list | None = None, filter_formula: str | None = None, page_size: int = 100) -> list:
        url = self._url_for_table(table)
        params: dict = {"pageSize": page_size}
        if fields:
            params["fields[]"] = fields
        if filter_formula:
            params["filterByFormula"] = filter_formula
        records: list = []
        offset = None
        while True:
            if offset:
                params["offset"] = offset
            response = requests.get(url, headers=self._headers, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            records.extend(payload.get("records", []) or [])
            offset = payload.get("offset")
            if not offset:
                break
        return records

    def _create_in(self, table: str, fields: dict, typecast: bool = True) -> str | None:
        url = self._url_for_table(table)
        try:
            response = requests.post(url, headers=self._headers, json={"fields": fields, "typecast": typecast}, timeout=30)
            response.raise_for_status()
            return response.json().get("id")
        except Exception as exc:  # pragma: no cover - network/diagnostic path
            print(f"[-] Airtable create failed on {table}: {exc}")
            return None

    def _patch_in(self, table: str, record_id: str, fields: dict, typecast: bool = True) -> bool:
        url = f"{self._url_for_table(table)}/{record_id}"
        try:
            response = requests.patch(url, headers=self._headers, json={"fields": fields, "typecast": typecast}, timeout=30)
            response.raise_for_status()
            return True
        except Exception as exc:  # pragma: no cover - network/diagnostic path
            print(f"[-] Airtable patch failed on {table}/{record_id}: {exc}")
            return False

    def list_accounts(self) -> list:
        """All account rows with the fields the planner needs."""
        return self._list_table(
            TABLE_ACCOUNTS,
            fields=[
                F_ACC_NAME, F_ACC_AUTOMATION_MODE, F_ACC_LIFECYCLE_STAGE,
                F_ACC_CREATION_DATE, F_ACC_NEEDS_VERIFICATION, F_ACC_PROFILE, F_ACC_BIO,
                F_ACC_PROFILE_PICTURE,
            ],
        )

    def profile_launch_map(self) -> dict:
        """record_id -> {'name', 'launch_id', 'serial'} for Profiles (Cloning).
        `launch_id` is the 18-digit MLX API ID the launcher/ADB actually need."""
        out: dict = {}
        for record in self._list_table(TABLE_PROFILES, fields=[F_PROF_NAME, F_PROF_MLX_API_ID, F_PROF_MLX_SERIAL]):
            fields = record.get("fields", {}) or {}
            out[record.get("id")] = {
                "name": fields.get(F_PROF_NAME),
                "launch_id": (str(fields.get(F_PROF_MLX_API_ID) or "").strip() or None),
                "serial": fields.get(F_PROF_MLX_SERIAL),
            }
        return out

    # ------------------------------------------------------------------
    # MultiLogin -> Airtable sync (Devices / Proxies / Profiles (Cloning)).
    # These read the current inventory keyed by the MLX serial and create
    # new rows for profiles MLX has that Airtable doesn't. See mlx_sync.py.
    # ------------------------------------------------------------------
    def profiles_by_serial(self) -> dict:
        """MLX serial_no -> {'record_id', 'name', 'api_id', 'time_zone'} for the
        existing Profiles (Cloning) rows. The serial is the sync's match key."""
        out: dict = {}
        rows = self._list_table(
            TABLE_PROFILES,
            fields=[F_PROF_NAME, F_PROF_MLX_SERIAL, F_PROF_MLX_API_ID, F_PROF_TIME_ZONE],
        )
        for record in rows:
            fields = record.get("fields", {}) or {}
            serial = str(fields.get(F_PROF_MLX_SERIAL) or "").strip()
            if not serial:
                continue
            out[serial] = {
                "record_id": record.get("id"),
                "name": fields.get(F_PROF_NAME),
                "api_id": (str(fields.get(F_PROF_MLX_API_ID) or "").strip() or None),
                "time_zone": (str(fields.get(F_PROF_TIME_ZONE) or "").strip() or None),
            }
        return out

    def models_by_name(self) -> dict:
        """Lower-cased Model Name -> record_id, so a synced Device can link to its
        Model when MLX's group/folder name matches an existing Models row."""
        out: dict = {}
        for record in self._list_table(TABLE_MODELS, fields=[F_MODEL_NAME]):
            name = str((record.get("fields", {}) or {}).get(F_MODEL_NAME) or "").strip()
            if name:
                out[name.lower()] = record.get("id")
        return out

    def create_device(self, fields: dict) -> str | None:
        return self._create_in(TABLE_DEVICES, fields)

    def create_proxy(self, fields: dict) -> str | None:
        return self._create_in(TABLE_PROXIES, fields)

    def create_profile(self, fields: dict) -> str | None:
        return self._create_in(TABLE_PROFILES, fields)

    def update_profile(self, record_id: str, fields: dict) -> bool:
        return self._patch_in(TABLE_PROFILES, record_id, fields)

    def todays_completed_runs(self) -> set:
        """Set of (account_record_id, flow_name) that already ran to Done/Running
        today -- makes a repeated button press idempotent."""
        formula = f"IS_SAME({{{F_RUN_AT}}}, TODAY(), 'day')"
        done: set = set()
        try:
            rows = self._list_table(TABLE_RUN_LOG, fields=[F_RUN_ACCOUNT, F_RUN_FLOW, F_RUN_RESULT, F_RUN_AT], filter_formula=formula)
        except Exception as exc:  # pragma: no cover
            print(f"[-] Airtable todays_completed_runs failed: {exc}")
            return done
        for record in rows:
            fields = record.get("fields", {}) or {}
            if _select_name(fields.get(F_RUN_RESULT)) not in (RESULT_DONE, RESULT_RUNNING):
                continue
            flow = _select_name(fields.get(F_RUN_FLOW))
            for account_id in (fields.get(F_RUN_ACCOUNT) or []):
                done.add((account_id, flow))
        return done

    def create_run_log(self, account_id: str, account_name: str, flow: str, result: str, notes: str | None = None) -> str | None:
        fields: dict = {
            F_RUN_NAME: f"{account_name} / {flow} / {_now_local_label()}",
            F_RUN_ACCOUNT: [account_id],
            F_RUN_FLOW: flow,
            F_RUN_RESULT: result,
            F_RUN_AT: _now_iso(),
        }
        if notes:
            fields[F_RUN_NOTES] = notes
        return self._create_in(TABLE_RUN_LOG, fields)

    def update_run_log(self, run_log_id: str, result: str, notes: str | None = None) -> bool:
        fields: dict = {F_RUN_RESULT: result}
        if notes:
            fields[F_RUN_NOTES] = notes
        return self._patch_in(TABLE_RUN_LOG, run_log_id, fields)

    def set_account_result(self, account_id: str, last_result: str, needs_verification: bool | None = None) -> bool:
        fields: dict = {F_ACC_LAST_RUN: _now_iso(), F_ACC_LAST_RESULT: last_result}
        if needs_verification is not None:
            fields[F_ACC_NEEDS_VERIFICATION] = bool(needs_verification)
        return self._patch_in(TABLE_ACCOUNTS, account_id, fields)

    # ------------------------------------------------------------------
    # Ban / verification incident write-back (checklist section 5). Called
    # when a real flow hits an IG ban/block/verification screen. See
    # incidents.apply_account_incident() for the kind -> fields mapping.
    # ------------------------------------------------------------------
    def flag_account(self, account_id: str, *, lifecycle_stage: str | None = None,
                     needs_verification: bool | None = None, ban_notes: str | None = None) -> bool:
        """Set the incident fields on an Account row. Only the arguments given are
        written, so a verification flag doesn't clobber the lifecycle stage."""
        fields: dict = {}
        if lifecycle_stage is not None:
            fields[F_ACC_LIFECYCLE_STAGE] = lifecycle_stage
        if needs_verification is not None:
            fields[F_ACC_NEEDS_VERIFICATION] = bool(needs_verification)
        if ban_notes is not None:
            fields[F_ACC_BAN_NOTES] = ban_notes
        if not fields:
            return True
        return self._patch_in(TABLE_ACCOUNTS, account_id, fields)

    def create_ban_flag_history(self, account_id: str, event_type: str, notes: str | None = None) -> str | None:
        """Append a Ban & Flag History row (Resolved defaults to unchecked)."""
        fields: dict = {
            F_BAN_DATE: _now_date(),
            F_BAN_EVENT_TYPE: event_type,
            F_BAN_ACCOUNT: [account_id],
            F_BAN_RESOLVED: False,
        }
        if notes:
            fields[F_BAN_NOTES] = notes
        return self._create_in(TABLE_BAN_HISTORY, fields)

    def set_posting_queue_issue(self, queue_record_id: str, issue_type: str,
                                post_status: str = POST_STATUS_FAILED) -> bool:
        """Mark a Posting Queue row's Issue Type / Post Status when a post fails
        mid-flight. Used by the posting loop (loop #1) once it's queue-driven."""
        return self._patch_in(
            TABLE_POSTING_QUEUE, queue_record_id,
            {F_PQ_ISSUE_TYPE: issue_type, F_PQ_POST_STATUS: post_status},
        )

    # ------------------------------------------------------------------
    # Posting Queue loop (checklist #1). The bot consumes Pending rows that
    # are due; Airtable fills the queue on its own 5x/day schedule.
    # ------------------------------------------------------------------
    def list_pending_posts(self) -> list:
        """Posting Queue rows still Pending, with the fields the planner needs.
        The due-time filter (Scheduled DateTime <= now) is applied in the planner
        so it stays testable."""
        formula = f"{{{F_PQ_POST_STATUS}}}='{POST_STATUS_PENDING}'"
        return self._list_table(
            TABLE_POSTING_QUEUE,
            fields=[
                F_PQ_NAME, F_PQ_SCHEDULED, F_PQ_POST_STATUS, F_PQ_RETRY_COUNT,
                F_PQ_CAPTION, F_PQ_SPOOF_VARIANT, F_PQ_TARGET_ACCOUNT,
            ],
            filter_formula=formula,
        )

    def accounts_by_id(self) -> dict:
        """record_id -> fields dict, for resolving a Posting Queue row's Target
        Account (guards + linked Profile)."""
        out: dict = {}
        for record in self.list_accounts():
            out[record.get("id")] = record.get("fields", {}) or {}
        return out

    def captions_by_id(self) -> dict:
        """Caption Pool record_id -> caption text."""
        out: dict = {}
        for record in self._list_table(TABLE_CAPTION_POOL, fields=[F_CAP_TEXT]):
            text = (record.get("fields", {}) or {}).get(F_CAP_TEXT)
            out[record.get("id")] = (str(text).strip() if text else None)
        return out

    def variants_by_id(self) -> dict:
        """Spoof Variants record_id -> {'file_path', 'status'}."""
        out: dict = {}
        for record in self._list_table(TABLE_SPOOF_VARIANTS, fields=[F_SV_FILE_PATH, F_SV_STATUS]):
            fields = record.get("fields", {}) or {}
            out[record.get("id")] = {
                "file_path": (str(fields.get(F_SV_FILE_PATH) or "").strip() or None),
                "status": _select_name(fields.get(F_SV_STATUS)),
            }
        return out

    def mark_post_result(self, queue_record_id: str, post_status: str,
                         issue_type: str | None = None, retry_count: int | None = None) -> bool:
        """Write a Posting Queue row's outcome: Post Status, and (on failure)
        Issue Type + Retry Count."""
        fields: dict = {F_PQ_POST_STATUS: post_status}
        if issue_type is not None:
            fields[F_PQ_ISSUE_TYPE] = issue_type
        if retry_count is not None:
            fields[F_PQ_RETRY_COUNT] = retry_count
        return self._patch_in(TABLE_POSTING_QUEUE, queue_record_id, fields)

    def mark_post_pending_verification(self, queue_record_id: str,
                                       delay_seconds: float = RECHECK_DELAY_SECONDS,
                                       note: str | None = None) -> bool:
        """Park a row that was sent but not proven: Post Status=Verifying and a
        Recheck After stamp `delay_seconds` out.

        Deliberately does NOT touch Retry Count or Issue Type. The post is not a
        failure -- it is an open question, and burning a retry on it would both
        misreport the account's health and push the row towards a re-send that
        the ledger would then have to block."""
        fields: dict = {
            F_PQ_POST_STATUS: POST_STATUS_VERIFYING,
            F_PQ_RECHECK_AFTER: _iso_in(delay_seconds),
        }
        if note:
            fields[F_PQ_NOTES] = note[:1000]
        return self._patch_in(TABLE_POSTING_QUEUE, queue_record_id, fields)

    def list_posts_awaiting_recheck(self) -> list:
        """Verifying rows whose Recheck After has passed -- the deferred pass's
        work queue. Rows still inside their wait are filtered out by Airtable so
        we don't pull the whole table every tick."""
        formula = (
            f"AND({{{F_PQ_POST_STATUS}}}='{POST_STATUS_VERIFYING}',"
            f"{{{F_PQ_RECHECK_AFTER}}}!='',"
            f"IS_BEFORE({{{F_PQ_RECHECK_AFTER}}}, NOW()))"
        )
        return self._list_table(
            TABLE_POSTING_QUEUE,
            fields=[
                F_PQ_NAME, F_PQ_SCHEDULED, F_PQ_POST_STATUS, F_PQ_RETRY_COUNT,
                F_PQ_CAPTION, F_PQ_SPOOF_VARIANT, F_PQ_TARGET_ACCOUNT, F_PQ_RECHECK_AFTER,
            ],
            filter_formula=formula,
        )

    def mark_variant_used(self, variant_record_id: str) -> bool:
        """Flag a Spoof Variant as Used so it isn't reused on another post."""
        return self._patch_in(TABLE_SPOOF_VARIANTS, variant_record_id, {F_SV_STATUS: SV_STATUS_USED})

    # ------------------------------------------------------------------
    # Spoofing pipeline (checklist #3): raw video -> Content Pipeline row ->
    # one Spoof Variant per active account under that model.
    # ------------------------------------------------------------------
    def models_by_recid(self) -> dict:
        """Models record_id -> Model Name (to resolve an Account's Model link)."""
        out: dict = {}
        for record in self._list_table(TABLE_MODELS, fields=[F_MODEL_NAME]):
            name = str((record.get("fields", {}) or {}).get(F_MODEL_NAME) or "").strip()
            if name:
                out[record.get("id")] = name
        return out

    def active_accounts_by_model(self) -> dict:
        """Lower-cased model name -> [{'account_id', 'handle'}] for accounts that
        are live (Lifecycle Stage = Active, not paused, not needs-verification).
        These are the accounts a new raw video gets spoofed for."""
        models = self.models_by_recid()
        rows = self._list_table(
            TABLE_ACCOUNTS,
            fields=[F_ACC_NAME, F_ACC_LIFECYCLE_STAGE, F_ACC_AUTOMATION_MODE, F_ACC_NEEDS_VERIFICATION, F_ACC_MODEL],
        )
        out: dict = {}
        for record in rows:
            fields = record.get("fields", {}) or {}
            if _select_name(fields.get(F_ACC_LIFECYCLE_STAGE)) != STAGE_ACTIVE:
                continue
            if _select_name(fields.get(F_ACC_AUTOMATION_MODE)) == MODE_PAUSED:
                continue
            if bool(fields.get(F_ACC_NEEDS_VERIFICATION)):
                continue
            model_links = fields.get(F_ACC_MODEL) or []
            if not model_links:
                continue
            model_name = models.get(model_links[0])
            if not model_name:
                continue
            handle = str(fields.get(F_ACC_NAME) or "").strip()
            if not handle:
                continue
            out.setdefault(model_name.lower(), []).append({"account_id": record.get("id"), "handle": handle})
        return out

    def content_pipeline_names(self) -> set:
        """Names of raw videos already recorded, so the pipeline skips them."""
        names: set = set()
        for record in self._list_table(TABLE_CONTENT_PIPELINE, fields=[F_CP_NAME]):
            name = str((record.get("fields", {}) or {}).get(F_CP_NAME) or "").strip()
            if name:
                names.add(name)
        return names

    def create_content_pipeline(self, name: str, model_id: str | None = None,
                                raw_link: str | None = None) -> str | None:
        fields: dict = {F_CP_NAME: name, F_CP_SPOOF_STATUS: CP_SPOOF_NEEDS}
        if model_id:
            fields[F_CP_MODEL] = [model_id]
        if raw_link:
            fields[F_CP_RAW_LINK] = raw_link
        return self._create_in(TABLE_CONTENT_PIPELINE, fields)

    def set_content_pipeline_spoofed(self, record_id: str, failed: bool = False) -> bool:
        spoof_status = CP_SPOOF_FAILED if failed else CP_SPOOF_SPOOFED
        fields = {F_CP_SPOOF_STATUS: spoof_status}
        if not failed:
            fields[F_CP_STATUS] = CP_STATUS_DONE
        return self._patch_in(TABLE_CONTENT_PIPELINE, record_id, fields)

    def create_spoof_variant(self, source_content_id: str, target_account_id: str,
                             file_path: str, method: str | None = None,
                             variant_id: str | None = None) -> str | None:
        fields: dict = {
            F_SV_FILE_PATH: file_path,
            F_SV_STATUS: SV_STATUS_READY,
            F_SV_CREATED_DATE: _now_date(),
            F_SV_SOURCE_CONTENT: [source_content_id],
            F_SV_TARGET_ACCOUNT: [target_account_id],
        }
        if variant_id:
            fields[F_SV_VARIANT_ID] = variant_id
        if method:
            fields[F_SV_METHOD] = method
        return self._create_in(TABLE_SPOOF_VARIANTS, fields)


def _select_name(value):
    """Airtable's REST API returns singleSelect values as the plain option-name
    string; tolerate the dict shape the metadata API uses too."""
    if isinstance(value, dict):
        return value.get("name")
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iso_in(seconds: float) -> str:
    """An ISO timestamp `seconds` from now, for Recheck After."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def _now_date() -> str:
    """Today's date (YYYY-MM-DD) for an Airtable `date` field."""
    return datetime.now(timezone.utc).date().isoformat()


def _now_local_label() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
