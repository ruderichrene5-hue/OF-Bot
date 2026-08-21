"""How far the move onto Geelark actually is, read off the phones themselves.

Geelark's `remark` is the only per-phone note the platform gives us, so the
provisioner wrote the migration's whole state into it in a fixed, greppable
shape rather than into a database that would drift from what a person sees when
they open the phone::

    IG:@handle | MAIL:address | PW:password [| MLX:name]
    NOCREDS | MLX:Nikki 12 | ID:6285... | IG:@handle

and each attempt at signing the account in appends one clause per fact::

    ... | LOGIN:OK-on-feed 2026-08-20
    ... | LOGIN:OK-needs-email-code 2026-08-20 | MAILBOX:CAPTCHA 2026-08-20
    ... | SIGNUP:created            (an account this fleet made, not migrated)

This module turns that text back into counts. It is deliberately a *reader*:
it never calls Geelark and never writes, so the dashboard cannot rotate a
proxy, start a phone or cost money by being looked at.

**The distinction the whole tab turns on.** "Could not sign in" is three
different findings wearing one label, and collapsing them overstates how much
of the fleet is lost:

* the account answered and wants a security code -- the credentials are *good*
  and the account is alive; it is a new-device challenge, not a dead account
* the account, handle or password is genuinely wrong or gone
* the phone never booted, ADB never answered, or Instagram never opened -- which
  says nothing whatsoever about the account and simply needs running again

Only the middle group is a migration blocker. Counting the third group as
"cannot be connected" is exactly the mistake that has been made on the MLX side
repeatedly -- a counter that hit its limit reported as a diagnosis, a flag that
described the phone rather than the account. See the `Retries Exhausted` and
`Human Verification Required` labels, both of which turned out to name the
symptom and not the cause.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Tags the Geelark side already writes. `IG connected` means signed in and on
# the feed -- *not* "could be signed in". An account sitting behind "confirm
# you're human" is real but cannot post, and tagging it would say the migration
# is further along than it is.
CONNECTED_TAG = "IG connected"
NEW_PROFILE_TAG = "new profile"

# Where `signup_geelark` appends one row per attempt to create a fresh account.
SIGNUP_LEDGER = Path.home() / ".adb_bot" / "signup" / "geelark_signups.jsonl"

# `KEY:value` up to the next pipe. Values can hold anything but `|`, which is
# why the provisioner picked it as the separator -- passwords contain spaces,
# colons and slashes, and splitting on any of those loses them.
_CLAUSE = re.compile(r"(?:^|\|)\s*([A-Z]+)\s*:\s*([^|]*)")

# A clause value is "LABEL" or "LABEL 2026-08-20". The date is optional because
# the earliest outcomes were written before it was added.
_LABEL_AND_DAY = re.compile(r"^(\S+)(?:\s+(\d{4}-\d{2}-\d{2}))?")

# What each `LOGIN:` label means for the migration, and who has to act.
#
# `group` is the bucket the tab counts in; `why` is what a person reading the
# row needs to know. Kept as data rather than branches because the vocabulary
# lives in `clients/geelark/outcomes.py` and will grow -- an unrecognised label
# must fall through as "unknown" rather than being silently miscounted as good.
LOGIN_MEANING: dict[str, tuple[str, str]] = {
    # Signed in and usable. The only label that finishes the job.
    "OK-on-feed": ("connected", "Signed in and on the feed."),

    # Instagram took the handle and password, then asked for a code. The
    # credentials are PROVEN GOOD -- this is the account not recognising a new
    # device. Migratable the moment a code can be answered.
    "OK-needs-email-code": ("needs_code", "Password accepted; wants an emailed code."),
    "OK-needs-sms-code": ("needs_code", "Password accepted; wants an SMS code."),
    "OK-needs-2fa": ("needs_code", "Password accepted; wants its 2FA code."),

    # The account side is genuinely wrong. A person has to find a working
    # credential or write the account off.
    "WRONG-PASSWORD": ("blocked", "Instagram rejected the stored password."),
    "ACCOUNT-GONE": ("blocked", "The account no longer exists."),
    "HANDLE-NOT-FOUND": ("blocked", "Instagram does not know this handle."),
    "SUSPENDED": ("blocked", "The account is suspended."),

    # The run failed, not the account. Says nothing about whether the account
    # can be connected -- it has simply not been tested yet.
    "PHONE-DID-NOT-BOOT": ("retry", "The phone never finished booting."),
    "INSTAGRAM-DID-NOT-OPEN": ("retry", "Instagram never opened on the phone."),
    "ADB-UNREACHABLE": ("retry", "ADB never answered on the phone."),
    "PHONE-NOT-READY": ("retry", "The phone was not ready to drive."),
    "UNKNOWN-SCREEN": ("retry", "Stopped on a screen the flow does not know."),
    "STUCK": ("retry", "The flow stopped making progress."),

    # Reached through the mailbox half. The account may well be fine; what
    # failed is the address that holds its code.
    "NO-CODE-ARRIVED": ("mailbox", "The code never arrived in the mailbox."),
    "CAPTCHA": ("mailbox", "Google put a robot check on the mailbox."),
}

# The `MAILBOX:` clause is scored on its own, because Instagram accepting a
# password and that account's mailbox being reachable are independent facts --
# writing one over the other cost two phones their known-good credentials once
# already.
MAILBOX_MEANING: dict[str, str] = {
    "OK-on-feed": "Mailbox reachable.",
    "WRONG-PASSWORD": "Google rejected the mailbox password.",
    "CAPTCHA": "Google put a robot check on the mailbox.",
    "NO-CODE-ARRIVED": "No code arrived at the mailbox.",
}

# Order the buckets are reported in: the ones a person can act on first, and
# within that, best news first. `new_account` sits last because those phones are
# not migration candidates at all -- they are where fresh accounts get made.
STATE_ORDER = ["connected", "needs_code", "ready", "blocked",
               "mailbox", "retry", "no_credentials",
               "signed_up", "new_account"]

STATE_LABELS = {
    "connected": "Connected",
    "needs_code": "Needs a code",
    "ready": "Ready to try",
    "blocked": "Cannot connect",
    "mailbox": "Mailbox problem",
    "retry": "Run failed — untested",
    "no_credentials": "No credentials",
    "signed_up": "New account made",
    "new_account": "For new accounts",
}

STATE_BLURBS = {
    "connected": ("Signed in and on the feed. These are migrated as far as "
                  "logging in goes."),
    "needs_code": ("Instagram accepted the password and then asked for a code. "
                   "The credentials are proven good and the account is alive — "
                   "this is a new-device challenge, not a dead account."),
    "ready": ("Credentials are on the phone and no login has been tried yet. "
              "These are the untested middle of the migration."),
    "blocked": ("Tried, and the account or its password is the problem. This "
                "is the only group that is genuinely a migration blocker."),
    "mailbox": ("The account may be fine; the address that holds its code is "
                "not reachable. Google has flagged most of the pool."),
    "retry": ("The phone or the app failed, so the account was never actually "
              "tested. Counting these as “cannot connect” overstates the "
              "problem — they just need running again."),
    "no_credentials": ("No password was ever recovered for this phone. The "
                       "remark says which MultiLogin profile to open."),
    "signed_up": ("A brand-new account this fleet created. Real and usable, "
                  "but it is not a recovered MultiLogin account, so it is "
                  "counted apart from the migration."),
    "new_account": ("Set aside to create a brand-new Instagram account on, "
                    "using the signup flow. Not a migration candidate."),
}


def parse_remark(remark: str) -> dict:
    """Pull the clauses out of one phone's remark.

    Returns plain strings, never `None`, so callers can format without
    guarding every field. `has_credentials` is the one derived answer, and it
    is deliberately keyed on the password rather than the handle: a phone can
    carry `IG:@name` with no password at all, and treating that as migratable
    is how a "NOCREDS" phone gets counted as ready.
    """
    out = {
        "handle": "", "email": "", "mlx_name": "", "mlx_id": "",
        "login": "", "login_day": "", "mailbox": "", "mailbox_day": "",
        "signup": "", "has_credentials": False, "no_creds_marker": False,
    }
    text = str(remark or "")
    if not text:
        return out

    # `NOCREDS` is a bare marker with no value, so the clause regex never sees
    # it -- it has to be looked for directly.
    out["no_creds_marker"] = "NOCREDS" in text

    for key, raw in _CLAUSE.findall(text):
        value = raw.strip()
        if key == "IG":
            out["handle"] = value.lstrip("@")
        elif key == "MAIL":
            out["email"] = value
        elif key == "MLX":
            out["mlx_name"] = value
        elif key == "ID":
            out["mlx_id"] = value
        elif key == "PW":
            # Never carried out of this function. Its presence is the fact that
            # matters; its value is a password sitting in a third-party
            # metadata field and does not belong on a web page.
            out["has_credentials"] = bool(value)
        elif key in ("LOGIN", "MAILBOX", "SIGNUP"):
            match = _LABEL_AND_DAY.match(value)
            label = match.group(1) if match else value
            day = (match.group(2) or "") if match else ""
            if key == "LOGIN":
                out["login"], out["login_day"] = label, day
            elif key == "MAILBOX":
                out["mailbox"], out["mailbox_day"] = label, day
            else:
                out["signup"] = label

    return out


def classify(phone: dict) -> dict:
    """One phone's migration state, its reason, and what it is called.

    `phone` is a row as `report.geelark_status` builds it: `name`, `tags`,
    `group` and `remark`.

    The order of these checks is the argument this module makes. Tags are read
    before remarks because a tag is a deliberate human or flow decision, and
    the remark is a log; where they disagree the decision wins.
    """
    tags = {str(tag or "") for tag in (phone.get("tags") or [])}
    parsed = parse_remark(phone.get("remark"))

    state, why = _state_for(tags, parsed)

    return {
        "name": str(phone.get("name") or ""),
        "folder": str(phone.get("group") or ""),
        "handle": parsed["handle"],
        "email": parsed["email"],
        "mlx_name": parsed["mlx_name"],
        "state": state,
        "why": why,
        "login": parsed["login"],
        "mailbox": parsed["mailbox"],
        "signup": parsed["signup"],
        "last_tried": parsed["login_day"] or parsed["mailbox_day"],
        "has_credentials": parsed["has_credentials"],
        "phone_status": str(phone.get("status") or ""),
    }


def _state_for(tags: set[str], parsed: dict) -> tuple[str, str]:
    """The bucket and the sentence, kept apart from `classify`'s bookkeeping."""
    # An account this fleet *created* rather than recovered. Checked FIRST,
    # ahead of the connected tag, because a finished signup writes both -- and
    # if the tag were read first these would land in `connected` and be counted
    # as migration wins. They are not: a new account does not bring back a
    # MultiLogin account, it adds a different one, and letting the two share a
    # bucket would make a fleet that is losing accounts look like one holding
    # steady.
    if parsed["signup"] == "created":
        return "signed_up", STATE_BLURBS["signed_up"]

    # Signed in and usable, decided by the tag the login flow writes. This wins
    # over everything below it: a phone that reached the feed is connected even
    # if an older failed attempt is still sitting in its remark.
    if CONNECTED_TAG in tags:
        return "connected", STATE_BLURBS["connected"]

    # Set aside for a fresh signup. Checked before the credential test because
    # a phone part-way through signup already carries a handle and password.
    if NEW_PROFILE_TAG in tags:
        detail = ""
        if parsed["signup"]:
            detail = f" Last signup attempt: {parsed['signup']}."
        return "new_account", STATE_BLURBS["new_account"] + detail

    if parsed["login"]:
        group, why = LOGIN_MEANING.get(
            parsed["login"], ("retry", f"Unrecognised login result "
                                       f"“{parsed['login']}”."))
        # A mailbox failure alongside a code request is the more useful of the
        # two: "wants an emailed code" and "the mailbox is captchaed" together
        # mean the account is fine and unreachable, which is a different errand
        # from a bad password.
        if group == "needs_code" and parsed["mailbox"] in MAILBOX_MEANING:
            if parsed["mailbox"] != "OK-on-feed":
                return "mailbox", (f"{why} {MAILBOX_MEANING[parsed['mailbox']]}")
        return group, why

    # Tried the mailbox but never got as far as a login result.
    if parsed["mailbox"] and parsed["mailbox"] != "OK-on-feed":
        return "mailbox", MAILBOX_MEANING.get(
            parsed["mailbox"], f"Mailbox result “{parsed['mailbox']}”.")

    if parsed["has_credentials"]:
        return "ready", STATE_BLURBS["ready"]

    # No password, whether or not the phone says NOCREDS out loud. A handle
    # with no password is not a migratable account.
    return "no_credentials", STATE_BLURBS["no_credentials"]


def summarise(phones: list[dict]) -> dict:
    """Roll the whole account up into the numbers the tab leads with.

    The three headline answers, and why they are drawn where they are:

    * **connected** -- signed in right now.
    * **can be connected** -- connected, plus the accounts whose password
      Instagram has already accepted, plus the untried ones that still hold a
      password. This is the migration's realistic ceiling.
    * **cannot** -- only the account-side failures and the phones with no
      password at all. A phone whose *run* failed is excluded on purpose; it is
      untested, not lost, and counting it here is how a bad afternoon of phone
      launches turns into a false verdict on the fleet.
    """
    rows = [classify(phone) for phone in phones]

    by_state: dict[str, list[dict]] = {state: [] for state in STATE_ORDER}
    for row in rows:
        by_state.setdefault(row["state"], []).append(row)

    counts = {state: len(entries) for state, entries in by_state.items()}

    # Phones that carry a *migrated* account. Both new-account states are
    # excluded: one is a phone reserved for a signup, the other is a signup that
    # worked, and neither says anything about whether the old fleet can move.
    migration_pool = [row for row in rows
                      if row["state"] not in ("new_account", "signed_up")]
    can = counts.get("connected", 0) + counts.get("needs_code", 0) + counts.get("ready", 0)
    cannot = counts.get("blocked", 0) + counts.get("no_credentials", 0)
    untested = counts.get("retry", 0) + counts.get("mailbox", 0)

    # Why each blocked phone is blocked, most common first -- the shape of the
    # problem rather than a list of 48 rows nobody reads to the end.
    reasons: dict[str, int] = {}
    for row in rows:
        if row["state"] in ("blocked", "mailbox", "retry"):
            reasons[row["why"]] = reasons.get(row["why"], 0) + 1

    folders: dict[str, dict] = {}
    for row in rows:
        folder = row["folder"] or "(no folder)"
        entry = folders.setdefault(folder, {"folder": folder, "total": 0})
        entry["total"] += 1
        entry[row["state"]] = entry.get(row["state"], 0) + 1

    for row in rows:
        row["state_label"] = STATE_LABELS.get(row["state"], row["state"])

    rows.sort(key=lambda r: (STATE_ORDER.index(r["state"])
                             if r["state"] in STATE_ORDER else len(STATE_ORDER),
                             r["folder"].lower(), r["name"].lower()))

    return {
        "rows": rows,
        "counts": counts,
        "migration_total": len(migration_pool),
        "can_connect": can,
        "cannot_connect": cannot,
        "untested": untested,
        # Reported separately from every migration number above, and read off
        # the phones rather than the ledger -- a run that died before writing
        # its ledger row still left the account on the phone.
        "new_accounts_made": counts.get("signed_up", 0),
        "reserved_for_signup": counts.get("new_account", 0),
        "reasons": sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])),
        "folders": sorted(folders.values(), key=lambda f: -f["total"]),
    }


def signup_progress(path: Path | None = None) -> dict:
    """What the new-account pipeline has done, from its own ledger.

    Read from the local ledger rather than from the phones, because a run that
    never reached the write-back step still left a row here -- and those are
    precisely the runs worth seeing. A missing file is a pipeline that has not
    run yet, not an error.
    """
    ledger = path or SIGNUP_LEDGER
    out = {"attempts": 0, "created": 0, "by_status": {}, "recent": [],
           "ledger": str(ledger), "exists": ledger.exists()}
    if not ledger.exists():
        return out

    rows: list[dict] = []
    try:
        for line in ledger.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # One malformed line must not cost the tab the whole ledger.
                continue
    except OSError:
        return out

    for row in rows:
        status = str(row.get("status") or "unknown")
        out["by_status"][status] = out["by_status"].get(status, 0) + 1

    out["attempts"] = len(rows)
    out["created"] = out["by_status"].get("created", 0)
    out["recent"] = [
        {
            "profile": str(row.get("profile") or row.get("serial_name") or ""),
            "folder": str(row.get("folder") or ""),
            "status": str(row.get("status") or ""),
            "handle": str(row.get("username") or ""),
            "mailbox": str(row.get("mailbox") or row.get("address") or ""),
        }
        for row in rows[-25:]
    ]
    out["recent"].reverse()
    return out
