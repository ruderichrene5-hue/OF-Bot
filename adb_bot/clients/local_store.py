"""Local, file-backed replacement for `AirtableClient`.

Airtable has broken twice in ways nothing here controls: the `Accounts` table
disappeared from the base for 7 hours (2026-08-18), and the `Models` table came
back with 0 rows and two fields renamed out from under us (2026-09-17), which
silently degraded the posting schedule to a hardcoded fallback. Both times the
bot kept running against a table a person could edit or delete without the bot
knowing. This module removes that risk by keeping the exact same data model --
one JSON file per table, each row shaped exactly like an Airtable record,
`{"id": ..., "fields": {...}}` -- on this server's own disk.

**Why subclassing, not a rewrite:** every one of AirtableClient's ~60
business methods (`list_pending_posts`, `mark_post_result`,
`reel_schedules_by_model`, ...) is written purely in terms of seven primitives:
`list_ready_records`, `update_record`, `_list_table`, `_create_in`,
`_get_field`, `_get_fields`, `_patch_in`. Nothing else in that file makes an
HTTP call. Overriding just those seven means every business method keeps
working, unchanged, against local files instead of Airtable's API -- the
calling code across the rest of the repo (`posting_planner.py`,
`queue_runner.py`, etc.) needs no changes at all, because it only ever reads
`row["fields"][...]` / `row["id"]`, which local records provide identically.

Storage layout: `~/.adb_bot/local_tables/<table>.json`, each holding
`{"records": {"<id>": {"id": "<id>", "fields": {...}}, ...}}`. Table names
carry spaces/parens ("Profiles (Cloning)", "Ban & Flag History") so they are
slugified for the filename only; the in-memory/record shape is untouched.

`filterByFormula` is Airtable's own formula language. Nothing here implements
all of it -- only the finite set of shapes this codebase actually sends
(checked against every `filter_formula=` call site in airtable.py): field
equality, `!=''`, `AND(...)`, `OR(...)`, `IS_SAME(field, TODAY(), 'day')`,
`IS_BEFORE(field, NOW())`. An unrecognised formula fails loudly rather than
silently returning the wrong rows -- a formula this module cannot evaluate is a
bug in this module, not a reason to guess.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.clients.airtable import AirtableClient
from adb_bot.config.settings import get_app_data_dir


def _tables_dir() -> Path:
    d = get_app_data_dir() / "local_tables"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(table_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", table_name.lower()).strip("_")


def _table_path(table_name: str) -> Path:
    return _tables_dir() / f"{_slug(table_name)}.json"


def _read_table(table_name: str) -> dict:
    path = _table_path(table_name)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data.get("records", {}) or {}


def _write_table(table_name: str, records: dict) -> None:
    """Atomic write: temp file + os.replace, same pattern as save_settings()."""
    path = _table_path(table_name)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"records": records}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def new_record_id() -> str:
    """Looks nothing like Airtable's `rec...` ids, deliberately -- a value that
    started local should never be mistaken for one that round-tripped through
    the real API."""
    return "loc" + uuid.uuid4().hex[:15]


# --- minimal formula evaluator -------------------------------------------

def _split_args(inner: str) -> list[str]:
    """Comma-split respecting paren depth, so `AND(a, IS_BEFORE(x, NOW()))`
    splits into two args, not three."""
    args, depth, buf = [], 0, ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        args.append(buf)
    return [a.strip() for a in args]


_EQ_RE = re.compile(r"^\{([^}]+)\}\s*=\s*'((?:[^'\\]|\\.)*)'$")
_NEQ_BLANK_RE = re.compile(r"^\{([^}]+)\}\s*!=\s*''$")
_IS_SAME_TODAY_RE = re.compile(r"^IS_SAME\(\{([^}]+)\},\s*TODAY\(\),\s*'day'\)$")
_IS_BEFORE_NOW_RE = re.compile(r"^IS_BEFORE\(\{([^}]+)\},\s*NOW\(\)\)$")


def _eval_formula(formula: str, fields: dict) -> bool:
    formula = formula.strip()

    m = _EQ_RE.match(formula)
    if m:
        field, value = m.group(1), m.group(2).replace("\\'", "'")
        return str(fields.get(field) or "") == value

    m = _NEQ_BLANK_RE.match(formula)
    if m:
        return bool(fields.get(m.group(1)))

    m = _IS_SAME_TODAY_RE.match(formula)
    if m:
        raw = fields.get(m.group(1))
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False
        now = datetime.now(timezone.utc)
        return dt.astimezone(timezone.utc).date() == now.date()

    m = _IS_BEFORE_NOW_RE.match(formula)
    if m:
        raw = fields.get(m.group(1))
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)

    if formula.startswith("AND(") and formula.endswith(")"):
        return all(_eval_formula(a, fields) for a in _split_args(formula[4:-1]))

    if formula.startswith("OR(") and formula.endswith(")"):
        return any(_eval_formula(a, fields) for a in _split_args(formula[3:-1]))

    raise ValueError(f"local_store cannot evaluate this filterByFormula shape: {formula!r}")


class LocalAirtableClient(AirtableClient):
    """Drop-in for AirtableClient, same constructor, same public methods,
    backed by ~/.adb_bot/local_tables/*.json instead of api.airtable.com.

    `token` and `base_id` are accepted and ignored (kept so every existing
    `AirtableClient(token, base_id, table_name)` call site needs no argument
    changes, only the class name swaps)."""

    def list_ready_records(self) -> list[dict]:
        from adb_bot.clients.airtable import FIELD_STATUS, STATUS_READY
        records = _read_table(self.table_name)
        return [r for r in records.values() if str((r.get("fields") or {}).get(FIELD_STATUS) or "") == STATUS_READY]

    def update_record(self, record_id: str, fields: dict) -> bool:
        records = _read_table(self.table_name)
        row = records.get(record_id)
        if row is None:
            print(f"[-] local_store update_record: no such record {record_id!r} in {self.table_name!r}")
            return False
        row.setdefault("fields", {}).update(fields)
        _write_table(self.table_name, records)
        return True

    def _list_table(self, table: str, fields: list | None = None, filter_formula: str | None = None,
                    page_size: int = 100, max_records: int | None = None) -> list:
        records = list(_read_table(table).values())
        if filter_formula:
            records = [r for r in records if _eval_formula(filter_formula, r.get("fields") or {})]
        if fields:
            records = [
                {"id": r.get("id"), "fields": {k: v for k, v in (r.get("fields") or {}).items() if k in fields}}
                for r in records
            ]
        if max_records is not None:
            records = records[:max_records]
        return records

    def _create_in(self, table: str, fields: dict, typecast: bool = True) -> str | None:
        records = _read_table(table)
        rid = new_record_id()
        records[rid] = {"id": rid, "fields": dict(fields)}
        _write_table(table, records)
        return rid

    def _get_field(self, table: str, record_id: str, field: str):
        row = _read_table(table).get(record_id)
        if row is None:
            return None
        return (row.get("fields") or {}).get(field)

    def _get_fields(self, table: str, record_id: str) -> dict:
        row = _read_table(table).get(record_id)
        return dict((row or {}).get("fields") or {})

    def content_pipeline_names(self) -> set:
        """Same idea as AirtableClient's, but a `Failed` row does not block a
        retry -- it only stops the pipeline creating a *second* Content
        Pipeline row for the same file while a fix is pending. The real
        Airtable version excludes every recorded name regardless of status,
        which was fine while a genuine failure meant "look at this by hand,"
        but 24 of 24 rows in this base failed for the same external reason
        (the video spoofer's project directory going missing, fixed
        2026-09-17) -- once the actual cause is fixed, "already recorded"
        must not keep meaning "never try again."
        """
        from adb_bot.clients.airtable import TABLE_CONTENT_PIPELINE, F_CP_NAME, F_CP_SPOOF_STATUS, CP_SPOOF_FAILED
        names: set = set()
        for record in _read_table(TABLE_CONTENT_PIPELINE).values():
            fields = record.get("fields") or {}
            if str(fields.get(F_CP_SPOOF_STATUS) or "") == CP_SPOOF_FAILED:
                continue
            name = str(fields.get(F_CP_NAME) or "").strip()
            if name:
                names.add(name)
        return names

    def reel_schedules_by_model(self) -> dict | None:
        """Same contract as AirtableClient's: `None` means "no per-model
        config exists, use the one global --slots grid"; `{}` would mean
        "every model opted in and chose to be flexible", which is not the
        same thing and must not be inferred from an empty local table -- an
        empty `Models` table (the exact state that caused the 2026-09-17
        scheduling regression) means nobody has entered per-model times
        *yet*, not that they were all deliberately left blank."""
        from adb_bot.clients.airtable import TABLE_MODELS
        if not _read_table(TABLE_MODELS):
            return None
        return super().reel_schedules_by_model()

    def _patch_in(self, table: str, record_id: str, fields: dict, typecast: bool = True) -> bool:
        records = _read_table(table)
        row = records.get(record_id)
        if row is None:
            print(f"[-] local_store patch: no such record {record_id!r} in {table!r}")
            return False
        row.setdefault("fields", {}).update(fields)
        _write_table(table, records)
        return True
