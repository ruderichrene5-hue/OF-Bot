"""Write a detected ban/verification incident back to Airtable (checklist
section 5). Kept separate from detection (`ban_detection.py`) and from the runner
so both the lifecycle runner and the future posting loop record incidents the
same way.

For a flagged account this:
- sets the Account's incident fields (Lifecycle Stage / Needs Human Verification
  / Ban / Flag Notes) per the kind's mapping;
- appends a Ban & Flag History row (Resolved unchecked);
- optionally marks the triggering Posting Queue row's Issue Type (posting loop).

The account then drops out of the loops automatically: the planner already skips
Lifecycle Stage = Banned and Needs Human Verification.
"""

from __future__ import annotations

from adb_bot.automation import ban_detection


def _compose_note(label: str, flow: str | None, detail: str | None) -> str:
    note = f"{label} during {flow}" if flow else label
    if detail and detail.strip() and detail.strip().lower() != label.lower():
        note = f"{note} — {detail.strip()}"
    return note


def apply_account_incident(airtable, account_id, flow, kind, detail=None, logger=None, queue_record_id=None):
    """Record a ban/verification/action-block incident for one account.

    Returns the IncidentMapping that was applied, or None if `kind` is unknown
    (so a caller can tell whether anything was written). Never raises -- the
    underlying client writes swallow their own errors so an incident write can't
    abort an in-progress run.
    """
    mapping = ban_detection.mapping_for(kind)
    if mapping is None:
        if logger:
            logger.warning("Unknown incident kind %r for account %s; nothing written", kind, account_id)
        return None

    notes = _compose_note(mapping.label, flow, detail)

    airtable.flag_account(
        account_id,
        lifecycle_stage=mapping.lifecycle_stage,
        # Only ever set the checkbox to True; never write False here, so we don't
        # un-flag an account that a human is still working through.
        needs_verification=True if mapping.needs_verification else None,
        ban_notes=notes,
    )
    airtable.create_ban_flag_history(account_id, mapping.event_type, notes)

    if queue_record_id:
        airtable.set_posting_queue_issue(queue_record_id, mapping.issue_type)

    if logger:
        logger.warning("Recorded %s incident for account %s (%s)", mapping.kind, account_id, notes)
    return mapping
