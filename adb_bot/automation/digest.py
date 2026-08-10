"""The once-a-day picture of what is waiting on a person.

The event notification in `issue_tags` tells the group the moment a phone breaks.
That is the right shape for "something just happened" and the wrong shape for
"this has been broken since Tuesday": a message you have already scrolled past
does not get louder as it ages. This is the other half -- one message a day, and
the number in it is the backlog.

What makes it worth reading is the **ageing**, not the count. "27 phones need a
person" is a wall; "the oldest has been waiting 5 days" is the line that gets one
picked up.

Two things it deliberately reports that nothing else surfaces to a human:

* **Un-flagged but still parked.** A profile whose `Needs Human Check` was
  cleared while `Status` stayed `Inactive` will never post, and the VA who
  cleared it has no way to know -- they saw the `Issue` tag disappear and
  reasonably assumed they were done. `recovery_runner` computes this and writes
  it to a log nobody reads.
* **What was due against what landed**, over the last 24h rather than "today", so
  the number means the same thing whatever hour the digest runs.

The composition is pure -- `build_digest` and `format_digest` take rows and
return a summary and a string. Only `run_digest` touches Airtable or Telegram,
which is what makes the interesting part testable without either.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from adb_bot.clients import airtable as at

# The window "last 24h" rather than "since midnight": the digest should report
# the same span whenever somebody moves the timer, and a morning run that said
# "3 posted" only because it ran at 08:00 would be misleading.
WINDOW_HOURS = 24

# Long enough that a phone appearing here is genuinely stuck rather than just
# waiting for the next working day.
STALE_FLAG_DAYS = 2


def _parse(value) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _select(fields: dict, key: str):
    value = fields.get(key)
    return value.get("name") if isinstance(value, dict) else value


@dataclass
class Digest:
    flagged: int = 0
    by_reason: list = field(default_factory=list)     # [(reason, count)], commonest first
    oldest_name: str | None = None
    oldest_days: int = 0
    stale: int = 0                                    # flagged longer than STALE_FLAG_DAYS
    parked_unflagged: list = field(default_factory=list)   # cleared, but Status Inactive
    due: int = 0
    posted: int = 0
    failed: int = 0
    pending: int = 0

    @property
    def quiet(self) -> bool:
        """Nothing waiting and nothing stuck -- worth saying so in one line."""
        return not self.flagged and not self.parked_unflagged


def build_digest(profiles, queue_rows, now=None) -> Digest:
    """Compose the day's numbers. Pure: rows in, summary out."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    out = Digest()

    reasons: Counter = Counter()
    oldest: datetime | None = None
    for row in profiles or []:
        f = row.get("fields", row) or {}
        flagged = bool(f.get(at.F_PROF_NEEDS_HUMAN))
        status = _select(f, at.F_PROF_STATUS)
        stamp = _parse(f.get(at.F_PROF_FLAGGED_AT))

        if flagged:
            out.flagged += 1
            reasons[str(_select(f, at.F_PROF_ISSUE_REASON) or "no reason recorded")] += 1
            if stamp:
                age = (now - stamp).days
                if age >= STALE_FLAG_DAYS:
                    out.stale += 1
                if oldest is None or stamp < oldest:
                    oldest, out.oldest_name, out.oldest_days = (
                        stamp, str(f.get(at.F_PROF_NAME) or "?"), age)
        elif stamp and status == at.STATUS_SELECT_INACTIVE:
            # Cleared by a person, but still switched off: the exact state that
            # looks fixed from MultiLogin and posts nothing.
            out.parked_unflagged.append(str(f.get(at.F_PROF_NAME) or "?"))

    out.by_reason = reasons.most_common()

    for row in queue_rows or []:
        f = row.get("fields", row) or {}
        when = _parse(f.get(at.F_PQ_SCHEDULED))
        if not when or when < cutoff or when > now:
            continue
        out.due += 1
        status = str(_select(f, at.F_PQ_POST_STATUS) or "")
        if status == at.POST_STATUS_POSTED:
            out.posted += 1
        elif status == at.POST_STATUS_FAILED:
            out.failed += 1
        elif status == at.POST_STATUS_PENDING:
            out.pending += 1
    return out


def format_digest(d: Digest) -> str:
    """The message body, in Telegram HTML."""
    if d.quiet:
        lines = ["📋 <b>Morning check — nothing waiting on a person</b>", ""]
    else:
        lines = [f"📋 <b>Morning check — {d.flagged} phone"
                 f"{'' if d.flagged == 1 else 's'} waiting on a person</b>", ""]
        for reason, count in d.by_reason:
            lines.append(f"• <b>{count}</b> — {reason}")
        if d.oldest_name:
            lines += ["", f"Oldest: <b>{d.oldest_name}</b>, waiting "
                          f"<b>{d.oldest_days} day{'' if d.oldest_days == 1 else 's'}</b>."]
        if d.stale:
            lines.append(f"{d.stale} of them have been waiting more than "
                         f"{STALE_FLAG_DAYS} days.")

    if d.parked_unflagged:
        names = ", ".join(f"<b>{n}</b>" for n in sorted(d.parked_unflagged)[:8])
        more = f" and {len(d.parked_unflagged) - 8} more" if len(d.parked_unflagged) > 8 else ""
        lines += ["", f"⚠️ Un-flagged but still switched off, so still not posting: "
                      f"{names}{more}. Set <b>Status</b> back to <b>Active</b> in Airtable."]

    lines += ["", f"Last {WINDOW_HOURS}h: <b>{d.posted} posted</b>, {d.failed} failed, "
                  f"{d.pending} still queued (of {d.due} due)."]
    if d.flagged:
        lines.append(f"All flagged phones carry the <code>{at_issue_tag()}</code> "
                     f"tag in MultiLogin.")
    return "\n".join(lines)


def at_issue_tag() -> str:
    from adb_bot.automation.issue_tags import ISSUE_TAG
    return ISSUE_TAG


def run_digest(airtable, notifier=None, dry_run: bool = True, logger=None,
               now=None) -> Digest:
    """Read both tables, compose, and send. Read-only against Airtable."""
    if notifier is None:
        from adb_bot.clients.telegram import TelegramNotifier
        notifier = TelegramNotifier()

    profiles = airtable.list_profile_rows() if hasattr(airtable, "list_profile_rows") \
        else airtable._list_table(at.TABLE_PROFILES)
    queue_rows = airtable._list_table(at.TABLE_POSTING_QUEUE)
    d = build_digest(profiles, queue_rows, now=now)
    body = format_digest(d)

    if logger is not None:
        logger.info("digest: %d flagged, %d parked-unflagged, last %dh %d/%d posted",
                    d.flagged, len(d.parked_unflagged), WINDOW_HOURS, d.posted, d.due)
        for line in body.replace("<b>", "").replace("</b>", "") \
                        .replace("<code>", "").replace("</code>", "").splitlines():
            if line.strip():
                logger.info("  %s", line)

    if dry_run:
        return d
    if not notifier.configured:
        if logger is not None:
            logger.info("digest: %s, nothing sent", notifier.describe())
        return d
    notifier.send(body, logger=logger)
    return d
