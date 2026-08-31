"""Per-cycle-attempt timing and outcome log for GeeLark's Active_Posting
pass -- built 2026-08-31 explicitly to analyze a full day's run afterwards:
how long each phone actually took, how many cycle attempts it needed, and
what it ended up as. The live journal has this too, but only as scattered
text across hours of HTTP noise; this is the same facts, one JSON line
per attempt, ready to aggregate without grepping logs.

Storage is an append-only JSON-lines file, same pattern and same reasoning
as post_ledger.py and account_stats.py: a torn write costs one line, not
the whole log, and concurrent writers (multiple phones running at once)
cannot corrupt each other's entries.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

RUN_LOG_FILENAME = "geelark_active_posting_run_log.jsonl"


@dataclass
class RunLogRecord:
    at: float                 # when this cycle attempt started
    duration_seconds: float   # how long this one attempt took, start to finish
    phone_id: str
    name: str
    cycle: str                 # "active_posting" -- room for other cycles later
    attempt: int               # 1..max_attempts for this phone's cycle
    result: str                 # posted / post_failed / post_uncertain / could_not_reach_over_adb / error / aborted_* / no_content
    posts_confirmed: int = 0
    posts_failed: int = 0
    posts_uncertain: int = 0
    error: str = ""


class RunLogStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else (get_app_data_dir() / RUN_LOG_FILENAME)

    def record(self, out: dict, *, started_at: float) -> RunLogRecord | None:
        """`out` is one call's result dict from _run_active_posting_cycle_once
        (already carries "attempt"); `started_at` is when that attempt began
        (wall clock, from before the call)."""
        phone_id = out.get("id")
        if not phone_id:
            return None
        posts = out.get("posts") or []
        rec = RunLogRecord(
            at=started_at,
            duration_seconds=max(0.0, time.time() - started_at),
            phone_id=str(phone_id),
            name=str(out.get("name") or ""),
            cycle=str(out.get("cycle") or "active_posting"),
            attempt=int(out.get("attempt") or 1),
            result=str(out.get("result") or ""),
            posts_confirmed=sum(1 for p in posts if p.get("result") == "posted"),
            posts_failed=sum(1 for p in posts if p.get("result") == "post_failed"),
            posts_uncertain=sum(1 for p in posts if p.get("result") == "post_uncertain"),
            error=str(out.get("error") or ""),
        )
        self._append(rec)
        return rec

    def load(self) -> list[RunLogRecord]:
        """Every record, in file order -- this log is analyzed as a whole
        history, not folded to "latest per key" like post_ledger/account_stats."""
        records: list[RunLogRecord] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(RunLogRecord(**json.loads(line)))
                    except Exception:
                        continue
        except FileNotFoundError:
            pass
        return records

    def _append(self, record: RunLogRecord) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(record), ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except Exception:
            # A logging failure must never take a posting cycle down with it
            # -- this store is for later analysis, not a dependency of
            # anything that posts.
            return False
