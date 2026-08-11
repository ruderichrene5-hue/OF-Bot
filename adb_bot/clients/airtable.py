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
TABLE_WARMUP_PLAN = "Warmup Plan"

# Warmup Plan -- the client-editable day-by-day warm-up schedule. One row per
# campaign day; the planner reads the row matching an account's day number.
F_WP_DAY = "Day"
F_WP_SCROLL = "Scroll"
F_WP_FOLLOW = "Follow People"
F_WP_FEED_POSTS = "Feed Posts"
F_WP_PICTURE = "Profile Picture Update"
F_WP_BIO = "Bio Update"
F_WP_REEL = "Reel Post"
F_WP_NOTES = "Notes"

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
F_PQ_TARGET_PROFILE = "Target Profile"   # link -> Profiles (Cloning); see below

# Which Instagram account on the target phone a row posts as. Some phones carry
# two accounts in one cloned app, reachable through Instagram's own account
# switcher, and each of them is a full posting target: its own slots, its own
# spoofed variants, its own verification. The handle is what the flow checks the
# switcher against; the slot is the human-readable label for it.
#
# Empty handle = post as whoever is signed in. That is every single-account
# phone, and it is why these are additive: a base (or a row) written before this
# existed behaves exactly as it did.
F_PQ_TARGET_HANDLE = "Target IG Handle"  # singleLineText, no '@'
F_PQ_ACCOUNT_SLOT = "Account Slot"       # singleSelect: Primary | Second
SLOT_PRIMARY = "Primary"
SLOT_SECOND = "Second"

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
F_SV_TARGET_PROFILE = "Target Profile"   # link -> Profiles (Cloning); see below
# A two-account phone needs TWO variants of every raw video, not one used twice:
# the same file on both accounts is the duplicate-content problem the per-target
# variants exist to avoid. The slot is what keeps the two pools apart -- without
# it both accounts draw from one pool and one of them starves.
F_SV_ACCOUNT_SLOT = "Account Slot"       # singleSelect: Primary | Second
F_SV_TARGET_HANDLE = "Target IG Handle"  # singleLineText, no '@'
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
F_PROF_ACCOUNTS = "Accounts"              # link -> Accounts
F_PROF_POSTING_QUEUE = "Posting Queue"    # link -> Posting Queue
# Day 1 of this profile's warm-up, written by the bot on the first warm-up run
# and never moved afterwards. The campaign day counts from it. Empty = the
# warm-up has not started; clearing it starts the profile over at day 1.
F_PROF_WARMUP_STARTED = "Warm-up Started"  # date

# Where the warm-up campaign is written down, so it can be read in Airtable
# rather than only on the dashboard. Reconciled from the Run Log by
# warmup_state.sync_warmup_state -- nothing here is authoritative, and deleting
# a value only means the next sweep puts it back.
#
# `Day` and `Stage` deliberately answer different questions. Day is the calendar
# day, which advances at midnight whether or not the night's run worked; Stage
# is the furthest day actually completed, and carries the same words as the
# MultiLogin tags so both tools can be filtered the same way. A profile whose
# Day has run away from its Stage is one that has stopped moving.
F_PROF_WARMUP_DAY = "Warm-up Day"              # number, calendar day of the plan
F_PROF_WARMUP_STAGE = "Warm-up Stage"          # singleSelect, mirrors the MLX tag
F_PROF_WARMUP_RUNS_DONE = "Warm-up Runs Done"  # number of warm-up runs that landed
F_PROF_WARMUP_LAST_RUN = "Warm-up Last Run"    # dateTime of the last run, any result
F_PROF_WARMUP_LAST_RESULT = "Warm-up Last Result"  # singleSelect: Done / Failed / Running

WARMUP_STATE_FIELDS = (F_PROF_WARMUP_DAY, F_PROF_WARMUP_STAGE, F_PROF_WARMUP_RUNS_DONE,
                       F_PROF_WARMUP_LAST_RUN, F_PROF_WARMUP_LAST_RESULT)

# The hand-off. A profile coming off the warm-up is not a posting target yet:
# it has no bio, no picture and has never posted, and a fresh account whose
# first ever post is an automated reel is the one Instagram acts on. These three
# are what a person does before the bot may schedule it, and they are three
# boxes rather than one sign-off because the work is three errands that get done
# on different days -- one box would mean a VA either ticks it early or cannot
# record having done two of the three.
#
# Readiness is derived (all three ticked), deliberately: a separate "Ready"
# switch is one more thing to forget, and `Status = Inactive` already exists for
# holding a profile back for any other reason.
F_PROF_BIO_DONE = "Bio Done"                      # checkbox
F_PROF_PICTURE_DONE = "Profile Picture Done"      # checkbox
F_PROF_FIRST_POST_DONE = "First Post Done"        # checkbox

HANDOFF_FIELDS = (F_PROF_BIO_DONE, F_PROF_PICTURE_DONE, F_PROF_FIRST_POST_DONE)
HANDOFF_LABELS = {F_PROF_BIO_DONE: "bio", F_PROF_PICTURE_DONE: "profile picture",
                  F_PROF_FIRST_POST_DONE: "first post"}

# Where a profile the bot has given up on is surfaced to a person. The Accounts
# table has had `Needs Human Verification` all along, but posting is
# profile-driven -- most of the ~90 MLX profiles have no Accounts row at all --
# so a flag there reached nobody. Added 2026-08-04.
F_PROF_NEEDS_HUMAN = "Needs Human Check"  # checkbox
F_PROF_ISSUE_REASON = "Issue Reason"      # singleSelect, see PROFILE_ISSUE_*
F_PROF_ISSUE_NOTES = "Issue Notes"        # multilineText, newest entry first
F_PROF_FLAGGED_AT = "Flagged At"          # dateTime

# Two Instagram accounts logged into one cloned app, switched between with
# Instagram's own account switcher. The second account is a posting target in
# its own right -- it gets its own queue rows, its own spoofed variants and its
# own post verification -- so these three fields are what the whole second
# account path is built on.
#
# `Has Second Account` is the switch a person controls; a profile with the box
# ticked but no `Second IG Handle` is deliberately NOT treated as two accounts,
# because the flow cannot switch to an account it cannot name.
F_PROF_HAS_SECOND = "Has Second Account"    # checkbox
F_PROF_PRIMARY_HANDLE = "Primary IG Handle"  # the account the phone signs in as
F_PROF_SECOND_HANDLE = "Second IG Handle"    # the account behind the switcher
F_PROF_ACCOUNTS_CHECKED = "Accounts Checked At"   # dateTime the switcher was read

PROFILE_ISSUE_EXHAUSTED = "Retries Exhausted"
PROFILE_ISSUE_VERIFICATION = "Human Verification Required"
PROFILE_ISSUE_BANNED = "Banned / Blocked"
PROFILE_ISSUE_REPEATED = "Repeated Failures"
PROFILE_ISSUE_UNREACHABLE = "Device Unreachable"
# A profile that is still trying and no longer landing. Deliberately its own
# reason rather than folded into "Repeated Failures": those come from a row the
# retry pass gave up on and are fixed by looking at the row, while this one is
# fixed by posting from the phone by hand and watching what Instagram does. The
# remedies differ, so the label has to. Written with typecast on, so Airtable
# adds the option to the select the first time it is used.
PROFILE_ISSUE_NO_SUCCESS = "No Recent Success"

# Written to a queue row whose Retry Count hit the limit. Distinct from
# `Failed - Needs Retry`, which is the only value the retry pass re-queues: an
# exhausted row left on that value reads as "still queued for another go" when
# nothing will ever pick it up again.
ISSUE_RETRIES_EXHAUSTED = "Retries Exhausted"

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
# multipleSelects of "HH:MM" labels: when this model's reels go out. Empty means
# no fixed schedule -- the queue loop then posts for it whenever a spoofed video
# is available (see queue_runner.ModelSchedule).
F_MODEL_REEL_TIMES = "Reel Post Times"
# number: the daily cap for that flexible mode. Ignored when Reel Post Times is
# filled, where the count of selected times IS the daily number.
F_MODEL_REELS_PER_DAY = "Reels Per Day"

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

# Every flow the warm-up runner can schedule, which is the set `warmup_run_log`
# has to read the Run Log for. The names are duplicated as literals from
# `automation.lifecycle` on purpose: `clients` sits underneath `automation` and
# is imported by it, so importing back the other way would be a cycle. The
# duplication is kept honest by a guard test that asserts this tuple still
# equals `automation.warmup_completion.WARMUP_RUN_FLOWS` -- rename a flow on one
# side and that test fails rather than the warm-up tab quietly going blank.
WARMUP_RUN_FLOWS = (
    "warm_up_process",
    "instagram_scroll",
    "update_profile_picture",
    "update_bio_u2",
)

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

    def _list_table(self, table: str, fields: list | None = None, filter_formula: str | None = None,
                    page_size: int = 100, max_records: int | None = None) -> list:
        """Every record in `table`, following Airtable's offset pagination.

        `max_records` stops after that many rows and is what a caller wants when
        it only needs to know the table is *there*. Without it a small
        `page_size` is a trap rather than an optimisation: the loop below
        paginates to the end regardless, so `page_size=1` means one HTTP request
        per record. That is what made `doctor`'s reachability probe take minutes
        once the tables had grown (2026-08-05).
        """
        url = self._url_for_table(table)
        params: dict = {"pageSize": page_size}
        if max_records is not None:
            params["maxRecords"] = max_records
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
            if not offset or (max_records is not None and len(records) >= max_records):
                break
        return records[:max_records] if max_records is not None else records

    def _create_in(self, table: str, fields: dict, typecast: bool = True) -> str | None:
        url = self._url_for_table(table)
        try:
            response = requests.post(url, headers=self._headers, json={"fields": fields, "typecast": typecast}, timeout=30)
            response.raise_for_status()
            return response.json().get("id")
        except Exception as exc:  # pragma: no cover - network/diagnostic path
            print(f"[-] Airtable create failed on {table}: {exc}")
            return None

    def _get_field(self, table: str, record_id: str, field: str):
        """One field off one record. Returns None if the read fails -- callers
        use this to append to a field, and losing the history is better than
        losing the write that was the point of the call."""
        url = f"{self._url_for_table(table)}/{record_id}"
        try:
            response = requests.get(url, headers=self._headers, timeout=30)
            response.raise_for_status()
            return (response.json().get("fields") or {}).get(field)
        except Exception as exc:  # pragma: no cover - network/diagnostic path
            print(f"[-] Airtable read failed on {table}/{record_id}: {exc}")
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
        """record_id -> {'name', 'launch_id', 'serial', 'needs_human', 'status'}
        for Profiles (Cloning). `launch_id` is the 18-digit MLX API ID the
        launcher/ADB actually need.

        The flag and Status ride along because the posting planner has to
        re-check them: a row that went Pending before its phone was parked is
        already in the queue, and the target filter that creates rows cannot
        reach back to it. Without that second check a freshly flagged phone
        still burns its outstanding slots -- a launch, a two-minute boot and an
        upload each.

        `warmup_started` and `handoff_outstanding` ride along for the same
        reason: a profile that has just come off the warm-up must not post
        until a person has given it a bio, a picture and a first post.
        """
        out: dict = {}
        for record in self._list_table(
            TABLE_PROFILES,
            fields=[F_PROF_NAME, F_PROF_MLX_API_ID, F_PROF_MLX_SERIAL,
                    F_PROF_NEEDS_HUMAN, F_PROF_STATUS, F_PROF_WARMUP_STARTED,
                    *HANDOFF_FIELDS],
        ):
            fields = record.get("fields", {}) or {}
            out[record.get("id")] = {
                "name": fields.get(F_PROF_NAME),
                "launch_id": (str(fields.get(F_PROF_MLX_API_ID) or "").strip() or None),
                "serial": fields.get(F_PROF_MLX_SERIAL),
                "needs_human": bool(fields.get(F_PROF_NEEDS_HUMAN)),
                "status": _select_name(fields.get(F_PROF_STATUS)),
                "warmup_started": (str(fields.get(F_PROF_WARMUP_STARTED) or "").strip()
                                   or None),
                "handoff_outstanding": [HANDOFF_LABELS[name] for name in HANDOFF_FIELDS
                                        if not fields.get(name)],
            }
        return out

    def warmup_profiles_by_serial(self) -> dict | None:
        """MLX serial_no -> the Profiles (Cloning) row the warm-up needs.

        ``{'record_id', 'name', 'api_id', 'status', 'warmup_started'}``. The
        serial is the match key because MLX profile *names* are not unique --
        this workspace has three different profiles called "Blank (1)", created
        on different days -- so matching on the name would warm up the wrong
        phone.

        Returns None (not {}) when the base has no `Warm-up Started` field:
        Airtable answers 422 for an unknown field name, and a caller must be
        able to tell "this base isn't set up for profile warm-up" from "no
        profile has started yet".
        """
        try:
            rows = self._list_table(
                TABLE_PROFILES,
                fields=[F_PROF_NAME, F_PROF_MLX_API_ID, F_PROF_MLX_SERIAL,
                        F_PROF_STATUS, F_PROF_WARMUP_STARTED],
            )
        except Exception as exc:  # pragma: no cover - network/schema path
            print(f"[-] Airtable: no {F_PROF_WARMUP_STARTED} field ({exc})")
            return None
        out: dict = {}
        for record in rows:
            fields = record.get("fields", {}) or {}
            serial = str(fields.get(F_PROF_MLX_SERIAL) or "").strip()
            if not serial:
                continue
            out[serial] = {
                "record_id": record.get("id"),
                "name": fields.get(F_PROF_NAME),
                "api_id": (str(fields.get(F_PROF_MLX_API_ID) or "").strip() or None),
                "status": _select_name(fields.get(F_PROF_STATUS)),
                "warmup_started": (str(fields.get(F_PROF_WARMUP_STARTED) or "").strip() or None),
            }
        return out

    def set_warmup_started(self, record_id: str, day_iso: str) -> bool:
        """Stamp day 1 on a profile. Written once, on the first warm-up run."""
        return self._patch_in(TABLE_PROFILES, record_id, {F_PROF_WARMUP_STARTED: day_iso})

    def warmup_state_snapshot(self) -> dict:
        """``{record_id: {field: value}}`` for the warm-up progress fields.

        One list call so the reconciler can skip the rows that already agree.
        Without it a sweep every hour rewrites 46 identical rows and every one
        of them gets a fresh Last Modified, which makes the column useless for
        seeing which profile actually moved.

        ``None`` when the fields have not been added to the table yet -- the
        same "tell the caller, don't guess" shape as `warmup_profiles_by_serial`.
        """
        try:
            records = self._list_table(TABLE_PROFILES, fields=list(WARMUP_STATE_FIELDS))
        except Exception as exc:
            print(f"[-] Airtable: no warm-up state fields ({exc})")
            return None
        out: dict = {}
        for record in records:
            fields = record.get("fields", {}) or {}
            out[record.get("id")] = {
                F_PROF_WARMUP_DAY: fields.get(F_PROF_WARMUP_DAY),
                F_PROF_WARMUP_STAGE: _select_name(fields.get(F_PROF_WARMUP_STAGE)),
                F_PROF_WARMUP_RUNS_DONE: fields.get(F_PROF_WARMUP_RUNS_DONE),
                F_PROF_WARMUP_LAST_RUN: fields.get(F_PROF_WARMUP_LAST_RUN),
                F_PROF_WARMUP_LAST_RESULT: _select_name(fields.get(F_PROF_WARMUP_LAST_RESULT)),
            }
        return out

    def profile_overview(self) -> list:
        """Every profile row, with everything the dashboard's people-facing tabs
        need, in one list call.

        The Needs-human tab and the Profiles tab ask different questions of the
        same 151 rows -- who is waiting on a person, and how each folder's
        phones are distributed. Reading the table twice would double the cost
        and let the two tabs disagree about the same profile mid-refresh.
        """
        out = []
        for record in self._list_table(
                TABLE_PROFILES,
                fields=[F_PROF_NAME, F_PROF_MLX_SERIAL, F_PROF_MLX_API_ID, F_PROF_STATUS,
                        F_PROF_NEEDS_HUMAN, F_PROF_ISSUE_REASON, F_PROF_ISSUE_NOTES,
                        F_PROF_FLAGGED_AT, F_PROF_WARMUP_STARTED, F_PROF_WARMUP_DAY,
                        F_PROF_WARMUP_STAGE, F_PROF_WARMUP_LAST_RUN, F_PROF_HAS_SECOND,
                        F_PROF_ACCOUNTS, F_PROF_POSTING_QUEUE, *HANDOFF_FIELDS]):
            fields = record.get("fields", {}) or {}
            out.append({
                "record_id": record.get("id"),
                "name": str(fields.get(F_PROF_NAME) or "").strip() or record.get("id"),
                "serial": str(fields.get(F_PROF_MLX_SERIAL) or "").strip(),
                "launch_id": str(fields.get(F_PROF_MLX_API_ID) or "").strip() or None,
                "status": _select_name(fields.get(F_PROF_STATUS)) or STATUS_SELECT_ACTIVE,
                "needs_human": bool(fields.get(F_PROF_NEEDS_HUMAN)),
                "reason": _select_name(fields.get(F_PROF_ISSUE_REASON)) or "",
                "note": str(fields.get(F_PROF_ISSUE_NOTES) or "").splitlines()[:1],
                "flagged_at": str(fields.get(F_PROF_FLAGGED_AT) or "")[:16].replace("T", " "),
                "warmup_started": str(fields.get(F_PROF_WARMUP_STARTED) or "").strip(),
                "warmup_day": fields.get(F_PROF_WARMUP_DAY),
                "warmup_stage": _select_name(fields.get(F_PROF_WARMUP_STAGE)) or "",
                "warmup_last_run": str(fields.get(F_PROF_WARMUP_LAST_RUN) or "")[:16].replace("T", " "),
                "has_second": bool(fields.get(F_PROF_HAS_SECOND)),
                "accounts": len(fields.get(F_PROF_ACCOUNTS) or []),
                "queue_rows": len(fields.get(F_PROF_POSTING_QUEUE) or []),
                # `{field: done}` rather than three keys, so the caller can name
                # what is outstanding without re-deriving the label each time.
                "handoff": {name: bool(fields.get(name)) for name in HANDOFF_FIELDS},
            })
        return out

    def set_warmup_state(self, record_id: str, fields: dict) -> bool:
        """Write the warm-up progress fields on one profile.

        `typecast` is on, so the first sweep adds the Stage and Last Result
        options to their selects rather than 422-ing on a name Airtable has not
        seen -- the same way `flag_profile_for_human` grows Issue Reason.
        """
        return self._patch_in(TABLE_PROFILES, record_id, dict(fields))

    def warmup_run_log(self, flows=WARMUP_RUN_FLOWS) -> list:
        """The whole warm-up history, newest first: every flow the warm-up
        runner schedules, not just `warm_up_process`.

        The whole history, not just today's: the dashboard reports which run of
        the plan each profile is on and whether the last one worked, and both
        are counts over the past, not a snapshot.

        It used to read one flow, and that is how the client's day 4 stayed
        invisible: their plan's last day asks for scroll-only, which runs as
        `instagram_scroll`, so 155 attempts across 46 profiles never appeared in
        the warm-up tab at all -- neither their failures nor, had any worked,
        their successes. Posting and reel rows are still nobody's warm-up and
        stay out; they belong to the Posting Queue.
        """
        if isinstance(flows, str):
            flows = [flows]
        names = [str(name).strip() for name in (flows or []) if str(name).strip()]
        if not names:
            raise ValueError("warmup_run_log needs at least one flow name")
        # The formula is interpolated, not escaped -- and a malformed one does
        # not degrade, it 422s the entire listing, which `report.warmup_progress`
        # turns into an `error` key and the dashboard renders as an empty
        # warm-up tab. A flow name with an apostrophe in it is a bug in the
        # caller either way, so say so here instead of shipping a broken filter.
        for name in names:
            if "'" in name:
                raise ValueError(f"flow name would corrupt the Airtable formula: {name!r}")
        clauses = [f"{{{F_RUN_FLOW}}}='{name}'" for name in names]
        formula = clauses[0] if len(clauses) == 1 else f"OR({','.join(clauses)})"
        rows = self._list_table(
            TABLE_RUN_LOG,
            fields=[F_RUN_NAME, F_RUN_FLOW, F_RUN_RESULT, F_RUN_AT, F_RUN_NOTES],
            filter_formula=formula,
        )
        rows.sort(key=lambda r: str((r.get("fields") or {}).get(F_RUN_AT) or ""), reverse=True)
        return rows

    def todays_attempted_profile_runs(self) -> set:
        """``{(key, flow)}`` for every profile run logged today, whatever it
        returned -- including the failures `todays_completed_profile_runs`
        deliberately leaves out.

        Not a dedupe key: a failed run must stay eligible to be retried. This is
        what tells a capped tick which profiles have already had a turn today,
        so a profile that fails every hour cannot hold the cap against the ones
        that have had none.
        """
        formula = f"IS_SAME({{{F_RUN_AT}}}, TODAY(), 'day')"
        seen: set = set()
        try:
            rows = self._list_table(TABLE_RUN_LOG,
                                    fields=[F_RUN_NAME, F_RUN_FLOW, F_RUN_AT],
                                    filter_formula=formula)
        except Exception as exc:  # pragma: no cover - network path
            print(f"[-] Airtable todays_attempted_profile_runs failed: {exc}")
            return seen
        for record in rows:
            fields = record.get("fields", {}) or {}
            flow = _select_name(fields.get(F_RUN_FLOW))
            key = str(fields.get(F_RUN_NAME) or "").split(" / ")[0].strip()
            if key and flow:
                seen.add((key, flow))
        return seen

    def todays_completed_profile_runs(self) -> set:
        """``{(profile name, flow)}`` already run to Done/Running today, for runs
        that have no Accounts row to link.

        `todays_completed_runs` keys on the linked account, which a
        profile-driven run does not have -- so without this every warm-up would
        run again on every tick of the hourly timer. The profile name comes off
        the Run Log's Name, which `create_run_log` writes as
        ``"<name> / <flow> / <when>"``.
        """
        # Same server-side "today" as todays_completed_runs, so the two agree on
        # the day boundary rather than each deciding it from a different clock.
        formula = f"IS_SAME({{{F_RUN_AT}}}, TODAY(), 'day')"
        done: set = set()
        try:
            rows = self._list_table(TABLE_RUN_LOG,
                                    fields=[F_RUN_NAME, F_RUN_FLOW, F_RUN_RESULT, F_RUN_AT],
                                    filter_formula=formula)
        except Exception as exc:  # pragma: no cover - network path
            print(f"[-] Airtable todays_completed_profile_runs failed: {exc}")
            return done
        for record in rows:
            fields = record.get("fields", {}) or {}
            if _select_name(fields.get(F_RUN_RESULT)) not in (RESULT_DONE, RESULT_RUNNING):
                continue
            flow = _select_name(fields.get(F_RUN_FLOW))
            name = str(fields.get(F_RUN_NAME) or "")
            profile_name = name.split(" / ")[0].strip()
            if profile_name and flow:
                done.add((profile_name, flow))
        return done

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

    def posting_profiles(self) -> list:
        """Every Profiles (Cloning) row a post could go out on, with the two
        switches that decide whether one will: Status and Needs Human Check.

        `profile_launch_map` carries neither, and `profile_targets_by_model`
        applies its own filters and reshapes the result by model -- so a caller
        asking "which profiles are live, and which are already flagged" had
        nothing to read. Filtering is left to the caller for the same reason
        the planner keeps its gate order explicit: a profile skipped for being
        Inactive and one skipped for being flagged are different answers.
        """
        out: list = []
        for record in self._list_table(
                TABLE_PROFILES,
                fields=[F_PROF_NAME, F_PROF_MLX_API_ID, F_PROF_STATUS,
                        F_PROF_NEEDS_HUMAN, F_PROF_ISSUE_REASON, F_PROF_FLAGGED_AT]):
            fields = record.get("fields", {}) or {}
            out.append({
                "record_id": record.get("id"),
                "name": str(fields.get(F_PROF_NAME) or "").strip(),
                "launch_id": (str(fields.get(F_PROF_MLX_API_ID) or "").strip() or None),
                # Empty Status counts as Active, exactly as the planners read it.
                "status": _select_name(fields.get(F_PROF_STATUS)) or STATUS_SELECT_ACTIVE,
                "needs_human": bool(fields.get(F_PROF_NEEDS_HUMAN)),
                # Why it was flagged (PROFILE_ISSUE_*), for a caller that has to
                # say something to a person -- "24 flagged" and "19 of them
                # waiting on an Instagram checkpoint" are different sentences,
                # and the second one is the actionable one. Costs nothing: it
                # rides along in the same list call as the checkbox.
                "reason": _select_name(fields.get(F_PROF_ISSUE_REASON)) or "",
                "flagged_at": str(fields.get(F_PROF_FLAGGED_AT) or "").strip() or None,
            })
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

    def flag_profile_for_human(self, record_id: str, reason: str, note: str,
                               when_iso: str | None = None,
                               max_notes_chars: int = 4000) -> bool:
        """Mark a profile as needing a person, and record why.

        The bot sets this and never clears it: clearing is the signal that
        somebody actually looked. Notes are prepended, so the newest reason is
        the first line and the history below it survives -- a profile that fails
        the same way for three nights should read as three nights, not one.

        Older entries are dropped once the field would exceed `max_notes_chars`
        rather than letting it grow without bound (Airtable's long-text limit is
        generous but not infinite, and a 100 KB cell is unreadable anyway).
        """
        stamp = when_iso or _now_iso()
        body = f"{reason}: {note}".strip()
        entry = f"[{stamp}] {body}"
        existing = ""
        try:
            existing = str(self._get_field(TABLE_PROFILES, record_id, F_PROF_ISSUE_NOTES) or "")
        except Exception:
            # A failed read must not cost the flag -- the checkbox is the part
            # that actually surfaces the profile to a person.
            existing = ""

        # The retry pass reconsiders every Failed row on every tick, so without
        # this the same unchanged problem is re-recorded every 30 minutes: ~48
        # identical lines per profile per day, which buries the one line that
        # says what is wrong and burns a write each time. A repeat of a problem
        # already recorded is not news; a *different* problem still appends.
        if body and body in existing:
            return True
        combined = f"{entry}\n{existing}".strip() if existing else entry
        if len(combined) > max_notes_chars:
            combined = combined[:max_notes_chars].rsplit("\n", 1)[0] + "\n[older entries trimmed]"
        return self._patch_in(TABLE_PROFILES, record_id, {
            F_PROF_NEEDS_HUMAN: True,
            F_PROF_ISSUE_REASON: reason,
            F_PROF_ISSUE_NOTES: combined,
            F_PROF_FLAGGED_AT: stamp,
        })

    def profiles_awaiting_recovery(self) -> list:
        """Profiles a person has un-flagged but the bot has not yet acted on.

        The signal is the *pair* of fields, because the checkbox alone cannot
        say it: once `Needs Human Check` is cleared there is nothing left to
        distinguish "somebody fixed this" from "never had a problem". `Flagged
        At` is written only by `flag_profile_for_human` and cleared only by the
        recovery pass, so **unchecked + still stamped** means exactly "was
        flagged, a person has looked, nobody has resumed it yet".
        """
        out: list = []
        try:
            rows = self._list_table(
                TABLE_PROFILES,
                fields=[F_PROF_NAME, F_PROF_NEEDS_HUMAN, F_PROF_ISSUE_REASON,
                        F_PROF_FLAGGED_AT, F_PROF_STATUS, F_PROF_MLX_API_ID],
                # BLANK() rather than !='': on a dateTime field the empty-string
                # comparison is not reliable, and a filter that silently matches
                # nothing would make this loop a permanent no-op.
                filter_formula=(f"AND(NOT({{{F_PROF_NEEDS_HUMAN}}}=1), "
                                f"NOT({{{F_PROF_FLAGGED_AT}}}=BLANK()))"),
            )
        except Exception as exc:  # pragma: no cover - network path
            print(f"[-] Airtable profiles_awaiting_recovery failed: {exc}")
            return out
        for record in rows:
            fields = record.get("fields", {}) or {}
            out.append({
                "record_id": record.get("id"),
                "name": str(fields.get(F_PROF_NAME) or "").strip() or record.get("id"),
                "status": _select_name(fields.get(F_PROF_STATUS)),
                "reason": _select_name(fields.get(F_PROF_ISSUE_REASON)),
                "flagged_at": str(fields.get(F_PROF_FLAGGED_AT) or "").strip() or None,
            })
        return out

    def reset_row_for_retry(self, queue_record_id: str) -> bool:
        """Hand one dead queue row back to the retry pass.

        Issue Type and Retry Count are what `retry_runner._row_verdict` rules
        on, so putting them back to "retryable, no attempts yet" is the whole
        handover -- the retry pass then applies its own ledger check and does the
        actual re-queueing. Post Status is deliberately left at Failed: this pass
        does not decide that a post may go out again, it only makes the row
        eligible to be *considered*.
        """
        return self._patch_in(TABLE_POSTING_QUEUE, queue_record_id, {
            F_PQ_ISSUE_TYPE: ISSUE_NEEDS_RETRY,
            F_PQ_RETRY_COUNT: 0,
        })

    def clear_profile_issue(self, record_id: str, note: str,
                            when_iso: str | None = None,
                            max_notes_chars: int = 4000) -> bool:
        """Close out a profile's issue once the recovery pass has resumed it.

        Clears `Flagged At` and `Issue Reason` -- which is what stops this
        profile being picked up again on the next tick -- while keeping Issue
        Notes as the history, with a line recording what was resumed. The
        checkbox is not touched: the person already cleared it, and writing it
        again would be the bot arguing with them.
        """
        stamp = when_iso or _now_iso()
        entry = f"[{stamp}] Recovered: {note}".strip()
        try:
            existing = str(self._get_field(TABLE_PROFILES, record_id, F_PROF_ISSUE_NOTES) or "")
        except Exception:
            existing = ""
        combined = f"{entry}\n{existing}".strip() if existing else entry
        if len(combined) > max_notes_chars:
            combined = combined[:max_notes_chars].rsplit("\n", 1)[0] + "\n[older entries trimmed]"
        return self._patch_in(TABLE_PROFILES, record_id, {
            F_PROF_ISSUE_REASON: None,
            F_PROF_FLAGGED_AT: None,
            F_PROF_ISSUE_NOTES: combined,
        })

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

    def warmup_plan_by_day(self) -> dict:
        """Day number -> the client's warm-up row for that day.

        The client edits this table to change what the warm-up does, so it is
        read fresh each run. Returns {} when the table is missing or empty, and
        the planner then falls back to the built-in schedule -- a base without
        the table keeps working exactly as before.
        """
        out: dict = {}
        try:
            rows = self._list_table(
                TABLE_WARMUP_PLAN,
                fields=[F_WP_DAY, F_WP_SCROLL, F_WP_FOLLOW, F_WP_FEED_POSTS,
                        F_WP_PICTURE, F_WP_BIO, F_WP_REEL, F_WP_NOTES],
            )
        except Exception:
            return {}
        for record in rows:
            fields = record.get("fields", {}) or {}
            try:
                day = int(fields.get(F_WP_DAY))
            except (TypeError, ValueError):
                continue          # a row with no day number can't be scheduled
            if day < 1:
                continue
            out[day] = {
                "scroll": bool(fields.get(F_WP_SCROLL)),
                "follow": bool(fields.get(F_WP_FOLLOW)),
                "feed_posts": int(fields.get(F_WP_FEED_POSTS) or 0),
                "picture": bool(fields.get(F_WP_PICTURE)),
                "bio": bool(fields.get(F_WP_BIO)),
                "reel": bool(fields.get(F_WP_REEL)),
                "notes": (str(fields.get(F_WP_NOTES) or "").strip() or None),
            }
        return out

    def create_run_log(self, account_id: str | None, account_name: str, flow: str, result: str, notes: str | None = None) -> str | None:
        """One row per flow run. `account_id` is None for a profile-driven run
        (no Accounts row exists): the row is still written -- losing the record
        of a real run is worse than an unlinked one -- just without the link,
        with the profile name carried by F_RUN_NAME."""
        fields: dict = {
            F_RUN_NAME: f"{account_name} / {flow} / {_now_local_label()}",
            F_RUN_FLOW: flow,
            F_RUN_RESULT: result,
            F_RUN_AT: _now_iso(),
        }
        if account_id:
            fields[F_RUN_ACCOUNT] = [account_id]
        if notes:
            fields[F_RUN_NOTES] = notes
        return self._create_in(TABLE_RUN_LOG, fields)

    def update_run_log(self, run_log_id: str, result: str, notes: str | None = None) -> bool:
        fields: dict = {F_RUN_RESULT: result}
        if notes:
            fields[F_RUN_NOTES] = notes
        return self._patch_in(TABLE_RUN_LOG, run_log_id, fields)

    def set_account_result(self, account_id: str | None, last_result: str, needs_verification: bool | None = None) -> bool:
        """No-op for a profile-driven run: there is no Accounts row to stamp."""
        if not account_id:
            return False
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
    def _list_queue_with_slot(self, fields: list, filter_formula: str | None = None) -> list:
        """Posting Queue rows, asking for the two second-account fields as well.

        Every caller wants them -- which account a row is for decides where the
        post goes, whether a slot is free, and which account a recheck must look
        at -- but a base that predates them answers 422 for the whole request,
        not just the unknown column. One retry without them keeps such a base on
        the single-account behaviour instead of failing the loop.
        """
        try:
            return self._list_table(
                TABLE_POSTING_QUEUE,
                fields=fields + [F_PQ_TARGET_HANDLE, F_PQ_ACCOUNT_SLOT],
                filter_formula=filter_formula,
            )
        except Exception:
            return self._list_table(TABLE_POSTING_QUEUE, fields=fields,
                                    filter_formula=filter_formula)

    def list_pending_posts(self) -> list:
        """Posting Queue rows still Pending, with the fields the planner needs.
        The due-time filter (Scheduled DateTime <= now) is applied in the planner
        so it stays testable."""
        formula = f"{{{F_PQ_POST_STATUS}}}='{POST_STATUS_PENDING}'"
        return self._list_queue_with_slot(
            [
                F_PQ_NAME, F_PQ_SCHEDULED, F_PQ_POST_STATUS, F_PQ_RETRY_COUNT,
                F_PQ_CAPTION, F_PQ_SPOOF_VARIANT, F_PQ_TARGET_ACCOUNT,
                F_PQ_TARGET_PROFILE,
            ],
            filter_formula=formula,
        )

    def list_failed_posts(self) -> list:
        """Posting Queue rows sitting in Failed -- the automatic-retry pass's
        work queue.

        Only Post Status is filtered here. Whether a failed row may actually be
        retried (Issue Type, Retry Count, and above all the local post ledger)
        is decided in retry_runner, so those rules stay testable without a base
        -- the same split list_pending_posts uses for the due-time check.
        """
        formula = f"{{{F_PQ_POST_STATUS}}}='{POST_STATUS_FAILED}'"
        return self._list_table(
            TABLE_POSTING_QUEUE,
            fields=[
                F_PQ_NAME, F_PQ_SCHEDULED, F_PQ_POST_STATUS, F_PQ_ISSUE_TYPE,
                F_PQ_RETRY_COUNT, F_PQ_SPOOF_VARIANT, F_PQ_TARGET_ACCOUNT,
                F_PQ_TARGET_PROFILE,
            ],
            filter_formula=formula,
        )

    def mark_post_retries_exhausted(self, queue_record_id: str) -> bool:
        """Retire a row that has used every retry.

        Post Status stays Failed; only Issue Type moves, off
        `Failed - Needs Retry` and onto `Retries Exhausted`. That value is what
        the retry pass keys on, so this is also what stops it reconsidering the
        row every 30 minutes -- and, for a person reading the base, the
        difference between "waiting for another attempt" and "nothing else will
        happen to this without you".
        """
        return self._patch_in(TABLE_POSTING_QUEUE, queue_record_id, {
            F_PQ_ISSUE_TYPE: ISSUE_RETRIES_EXHAUSTED,
        })

    def requeue_post(self, queue_record_id: str, scheduled_iso: str,
                     note: str | None = None) -> bool:
        """Put a failed row back in the queue: Pending, due at `scheduled_iso`.

        Retry Count is deliberately NOT touched. The posting runner bumps it when
        an attempt fails, so it counts *attempts*; bumping it here as well would
        burn two of the three allowed retries per real attempt and the row would
        die at half its budget.

        Issue Type is cleared because the row is no longer failed -- leaving
        "Failed - Needs Retry" on a Pending row makes the queue unreadable to the
        client. The reason it was requeued goes in Notes instead.
        """
        fields: dict = {
            F_PQ_POST_STATUS: POST_STATUS_PENDING,
            F_PQ_SCHEDULED: scheduled_iso,
            F_PQ_ISSUE_TYPE: ISSUE_NONE,
        }
        if note:
            fields[F_PQ_NOTES] = note[:1000]
        return self._patch_in(TABLE_POSTING_QUEUE, queue_record_id, fields)

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
        return self._list_queue_with_slot(
            [
                F_PQ_NAME, F_PQ_SCHEDULED, F_PQ_POST_STATUS, F_PQ_RETRY_COUNT,
                F_PQ_CAPTION, F_PQ_SPOOF_VARIANT, F_PQ_TARGET_ACCOUNT,
                F_PQ_TARGET_PROFILE, F_PQ_RECHECK_AFTER,
            ],
            filter_formula=formula,
        )

    # ------------------------------------------------------------------
    # Slot creation (queue_runner): Ready variant -> Posting Queue row. The
    # Airtable automations that were meant to fill the queue never wrote the
    # Spoof Variant link, so every row they made was unpostable; these three
    # give the code path everything it needs to write a complete row.
    # ------------------------------------------------------------------
    def list_ready_variants(self) -> list:
        """Spoof Variants at Status Ready, with the target link each one carries:
        ``[{'id', 'file_path', 'status', 'account_id', 'profile_id', 'created'}]``.

        Exactly one of `account_id` / `profile_id` is set (a profile-driven
        variant has no Accounts row). `created` orders the pool so the queue
        works through the backlog oldest-first.

        `slot` is which of a two-account phone's accounts the variant was made
        for; an unset slot reads as Primary, which is what every variant spoofed
        before two-account phones existed is."""
        formula = f"{{{F_SV_STATUS}}}='{SV_STATUS_READY}'"
        base_fields = [F_SV_FILE_PATH, F_SV_STATUS, F_SV_CREATED_DATE,
                       F_SV_TARGET_ACCOUNT, F_SV_TARGET_PROFILE]
        try:
            rows = self._list_table(TABLE_SPOOF_VARIANTS,
                                    fields=base_fields + [F_SV_ACCOUNT_SLOT],
                                    filter_formula=formula)
        except Exception:
            rows = self._list_table(TABLE_SPOOF_VARIANTS, fields=base_fields,
                                    filter_formula=formula)
        out: list = []
        for record in rows:
            fields = record.get("fields", {}) or {}
            accounts = fields.get(F_SV_TARGET_ACCOUNT) or []
            profiles = fields.get(F_SV_TARGET_PROFILE) or []
            out.append({
                "id": record.get("id"),
                "file_path": (str(fields.get(F_SV_FILE_PATH) or "").strip() or None),
                "status": _select_name(fields.get(F_SV_STATUS)),
                "account_id": accounts[0] if accounts else None,
                "profile_id": profiles[0] if profiles else None,
                "slot": _select_name(fields.get(F_SV_ACCOUNT_SLOT)) or SLOT_PRIMARY,
                "created": fields.get(F_SV_CREATED_DATE),
            })
        return out

    def list_queue_rows(self, statuses=None) -> list:
        """Posting Queue rows with the fields slot creation needs to see.

        `statuses` filters on Post Status; **None means every status**, which is
        what the slot runner wants: a slot whose post already landed (Posted) or
        was abandoned (Failed) has been served, and only a full listing can tell
        it that. The Pending-only listing is `list_pending_posts`."""
        formula = None
        if statuses:
            clauses = ",".join(f"{{{F_PQ_POST_STATUS}}}='{s}'" for s in statuses)
            formula = f"OR({clauses})"
        return self._list_queue_with_slot(
            [
                F_PQ_NAME, F_PQ_SCHEDULED, F_PQ_POST_STATUS, F_PQ_SPOOF_VARIANT,
                F_PQ_TARGET_ACCOUNT, F_PQ_TARGET_PROFILE,
            ],
            filter_formula=formula,
        )

    def create_posting_queue(self, scheduled_iso: str, variant_id: str,
                             target_account_id: str | None = None,
                             target_profile_id: str | None = None,
                             name: str | None = None,
                             caption_id: str | None = None,
                             target_handle: str | None = None,
                             account_slot: str | None = None) -> str | None:
        """One scheduled post: Pending, at `scheduled_iso`, for `variant_id`.

        The Spoof Variant link is not optional -- a row without it is rejected by
        the posting planner ("no Spoof Variant video path"), which is exactly how
        the Airtable automations this replaces produced dead rows.

        Exactly one target link is written, matching whichever one the variant
        carries; writing both would make the row ambiguous for the planner, which
        reads the account first and would post a profile's video on someone
        else's account.

        `target_handle` / `account_slot` say WHICH Instagram account on the
        target phone the row is for. Both are left off entirely when no handle
        was given, so a single-account row is byte-for-byte what it was before
        two-account phones existed -- and a base whose Posting Queue has no such
        fields keeps working, since the fields are only sent when they are
        needed."""
        fields: dict = {
            F_PQ_POST_STATUS: POST_STATUS_PENDING,
            F_PQ_SCHEDULED: scheduled_iso,
            F_PQ_SPOOF_VARIANT: [variant_id],
        }
        if name:
            fields[F_PQ_NAME] = name
        if target_profile_id:
            fields[F_PQ_TARGET_PROFILE] = [target_profile_id]
        elif target_account_id:
            fields[F_PQ_TARGET_ACCOUNT] = [target_account_id]
        if caption_id:
            fields[F_PQ_CAPTION] = [caption_id]
        handle = _handle(target_handle)
        if handle:
            fields[F_PQ_TARGET_HANDLE] = handle
            fields[F_PQ_ACCOUNT_SLOT] = account_slot or SLOT_PRIMARY
        return self._create_in(TABLE_POSTING_QUEUE, fields)

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

    def reel_schedules_by_model(self) -> dict | None:
        """Lower-cased model name -> ``{'times': ['09:00', ...], 'per_day': int|None}``.

        This is where a person says *when* a model's reels go out: the Models
        row's `Reel Post Times`. A model with times posts at those times; a model
        with none is flexible -- the queue loop posts for it whenever a spoofed
        video is available, bounded by `Reels Per Day`.

        Returns **None**, not ``{}``, when the base has no `Reel Post Times`
        field: Airtable answers 422 UNKNOWN_FIELD_NAME for a field that isn't
        there, and the two answers must not be confused. ``{}`` would mean "every
        model is flexible" and would take a base that never opted in off its
        fixed grid; None means "this base doesn't do per-model times", and the
        caller keeps the single global grid it used before.
        """
        try:
            rows = self._list_table(
                TABLE_MODELS,
                fields=[F_MODEL_NAME, F_MODEL_REEL_TIMES, F_MODEL_REELS_PER_DAY],
            )
        except Exception as exc:  # pragma: no cover - network/schema path
            print(f"[-] Airtable: no per-model reel times ({exc}); using the global slot grid")
            return None
        out: dict = {}
        for record in rows:
            fields = record.get("fields", {}) or {}
            name = str(fields.get(F_MODEL_NAME) or "").strip()
            if not name:
                continue
            raw = fields.get(F_MODEL_REEL_TIMES) or []
            if not isinstance(raw, list):
                raw = [raw]
            # REST returns choice *names* ('09:00'); the MCP layer returns choice
            # objects. _select_name copes with both.
            times = [t for t in (_select_name(value) for value in raw) if t]
            try:
                per_day = int(fields.get(F_MODEL_REELS_PER_DAY))
            except (TypeError, ValueError):
                per_day = None
            out[name.lower()] = {"times": times, "per_day": per_day}
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

    def profile_targets_by_model(self, include_link_profiles: bool = False) -> dict:
        """Lower-cased model name -> [{'profile_id', 'handle', 'launch_id', 'slot',
        'ig_handle'}] built from the MLX profile inventory instead of the
        Accounts table.

        Used when the pipeline is told to take its targets from the profiles
        (`targets='profiles'`), which is how models with real phones but no
        Accounts rows yet get content made for them.

        The model comes from the profile *name*, not the MLX folder: folders and
        groups on this workspace are named with raw UUIDs, while the names follow
        ``<Model> <N>`` ("Jil 1", "Katja 3"), so the first word is the only
        reliable key.

        Skipped: any profile whose Status is not Active, the per-model "Link"
        profile (`Jasmin Link`, `Jil I Link Account`), which is the fixed
        link-in-bio account rather than a posting target, and any profile with no
        MLX API ID -- without the 18-digit launch key nothing can be launched for
        it anyway.

        Status is the ONLY switch a profile-driven target has. An Accounts row
        carries three health guards (Lifecycle Stage, Automation Mode, Needs
        Human Verification) that the planners honour, but most profiles have no
        Accounts row at all, so without this check there was no way to take a
        flagged account -- or an unused MLX staging profile ("Blank ...") -- out
        of the run short of deleting its row. Setting Status to Inactive in
        Airtable is now how a person parks a profile: it stops both the spoof
        pipeline making variants for it and the queue creating slots for it.

        **Two-account phones yield two targets.** A profile with `Has Second
        Account` ticked and BOTH handles filled in returns a Primary entry and a
        Second entry -- same phone, same launch key, different Instagram account
        -- so everything downstream (spoofing, slot creation, posting,
        verification) treats the second account as a first-class target without
        knowing it shares a device with the first.

        Both handles are required, not just the second one. The flow has to be
        able to name the account it is switching *back* to; with only the second
        handle known, one Second post would leave the phone signed in as the
        second account and every later Primary post would go out on the wrong
        one. Half-filled rows are therefore left as single-account phones.

        The `handle` of the Second entry is the IG handle itself rather than the
        profile name, because `handle` is what names variant files, queue rows
        and log lines -- two targets sharing "Jil 5" would be indistinguishable
        in all three, and `finalize_variant` would have them overwrite each
        other's video.
        """
        out: dict = {}
        # The second-account fields are recent; a base without them must still
        # return its single-account targets rather than raise. Airtable answers
        # 422 for an unknown field name, so ask once and fall back.
        wanted = [F_PROF_NAME, F_PROF_MLX_API_ID, F_PROF_STATUS,
                  F_PROF_HAS_SECOND, F_PROF_PRIMARY_HANDLE, F_PROF_SECOND_HANDLE]
        try:
            records = self._list_table(TABLE_PROFILES, fields=wanted)
        except Exception:
            records = self._list_table(
                TABLE_PROFILES, fields=[F_PROF_NAME, F_PROF_MLX_API_ID, F_PROF_STATUS])
        for record in records:
            fields = record.get("fields", {}) or {}
            name = str(fields.get(F_PROF_NAME) or "").strip()
            if not name:
                continue
            # An empty Status is treated as Active: rows created before the field
            # was filled in must not silently drop out of the run.
            status = _select_name(fields.get(F_PROF_STATUS))
            if status is not None and status != STATUS_SELECT_ACTIVE:
                continue
            if not include_link_profiles and "link" in name.lower():
                continue
            launch_id = str(fields.get(F_PROF_MLX_API_ID) or "").strip()
            if not launch_id:
                continue
            model_key = name.split()[0].lower()
            primary_handle = _handle(fields.get(F_PROF_PRIMARY_HANDLE))
            second_handle = _handle(fields.get(F_PROF_SECOND_HANDLE))
            two_accounts = (bool(fields.get(F_PROF_HAS_SECOND))
                            and bool(primary_handle) and bool(second_handle))
            out.setdefault(model_key, []).append({
                "profile_id": record.get("id"),
                "handle": name,
                "profile_name": name,
                "launch_id": launch_id,
                "slot": SLOT_PRIMARY,
                # Named only on a two-account phone. On every other phone an
                # empty handle means "post as whoever is signed in", which is
                # what the flow has always done -- and asking a single-account
                # phone to prove its handle would turn one stale Airtable value
                # into a failed post.
                "ig_handle": primary_handle if two_accounts else None,
            })
            if two_accounts:
                out[model_key].append({
                    "profile_id": record.get("id"),
                    "handle": second_handle,
                    # Kept alongside the handle so a row for this target can
                    # still say which phone it is on. The report works out a
                    # model from the target's name by taking its first word, and
                    # a bare handle ("jiji.ll12") has no model in it.
                    "profile_name": name,
                    "launch_id": launch_id,
                    "slot": SLOT_SECOND,
                    "ig_handle": second_handle,
                })
        for targets in out.values():
            targets.sort(key=lambda t: t["handle"])
        return out

    def second_account_profiles(self) -> list | None:
        """Every profile that carries two Instagram accounts, for the report.

        ``[{'record_id', 'name', 'status', 'primary', 'second', 'checked_at',
        'usable'}]``. `usable` is the same rule `profile_targets_by_model`
        applies -- both handles known -- so the page can show a ticked box that
        is NOT yet producing posts as exactly that, rather than as a phone which
        is quietly doing nothing.

        Returns None when the base has no `Has Second Account` field at all, so
        the report can leave the section out instead of claiming no phone has a
        second account.
        """
        try:
            records = self._list_table(
                TABLE_PROFILES,
                fields=[F_PROF_NAME, F_PROF_STATUS, F_PROF_HAS_SECOND,
                        F_PROF_PRIMARY_HANDLE, F_PROF_SECOND_HANDLE, F_PROF_ACCOUNTS_CHECKED],
            )
        except Exception:
            return None
        out: list = []
        for record in records:
            fields = record.get("fields", {}) or {}
            if not bool(fields.get(F_PROF_HAS_SECOND)):
                continue
            primary = _handle(fields.get(F_PROF_PRIMARY_HANDLE))
            second = _handle(fields.get(F_PROF_SECOND_HANDLE))
            out.append({
                "record_id": record.get("id"),
                "name": str(fields.get(F_PROF_NAME) or "").strip(),
                "status": _select_name(fields.get(F_PROF_STATUS)),
                "primary": primary,
                "second": second,
                "checked_at": str(fields.get(F_PROF_ACCOUNTS_CHECKED) or "").strip() or None,
                "usable": bool(primary and second),
            })
        out.sort(key=lambda p: p["name"].lower())
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

    def create_spoof_variant(self, source_content_id: str, target_account_id: str | None,
                             file_path: str, method: str | None = None,
                             variant_id: str | None = None,
                             target_profile_id: str | None = None,
                             target_handle: str | None = None,
                             account_slot: str | None = None) -> str | None:
        """One spoofed video, linked to what it was made for.

        The target is an Account normally, or a Profiles (Cloning) row when the
        pipeline is driven by the MLX profile inventory (`targets='profiles'`).
        Exactly one of the two links is written -- writing both would make the
        row ambiguous for the posting planner, which reads whichever is set.

        On a two-account phone the profile link alone is ambiguous in a second
        way: both accounts live on the same Profiles row. `account_slot` is what
        separates their pools, so the queue can tell "a video for the primary
        account" from "a video for the second one" and never hand one account's
        clip to the other.
        """
        fields: dict = {
            F_SV_FILE_PATH: file_path,
            F_SV_STATUS: SV_STATUS_READY,
            F_SV_CREATED_DATE: _now_date(),
            F_SV_SOURCE_CONTENT: [source_content_id],
        }
        if target_profile_id:
            fields[F_SV_TARGET_PROFILE] = [target_profile_id]
        elif target_account_id:
            fields[F_SV_TARGET_ACCOUNT] = [target_account_id]
        if variant_id:
            fields[F_SV_VARIANT_ID] = variant_id
        if method:
            fields[F_SV_METHOD] = method
        handle = _handle(target_handle)
        if handle:
            fields[F_SV_TARGET_HANDLE] = handle
            fields[F_SV_ACCOUNT_SLOT] = account_slot or SLOT_PRIMARY
        return self._create_in(TABLE_SPOOF_VARIANTS, fields)


def _handle(value):
    """An Instagram handle as the flow wants it: bare, no '@', or None.

    People type the handle into Airtable with the '@' about half the time, and
    the account switcher shows it without one -- so normalising here is what
    stops a leading '@' from making every switch fail to match.
    """
    text = str(value or "").strip().lstrip("@").strip()
    return text or None


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
