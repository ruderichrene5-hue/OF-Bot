"""The pool of Gmail addresses new accounts are created on, and writing back what was used.

    python -m adb_bot.automation.signup_mailboxes --list
    python -m adb_bot.automation.signup_mailboxes --take 5

The mailboxes live in a **different base from the bot's** -- `VA - ML I IG`
(`appwUBCFYK7c5kVln`), table `Gmail Accounts` -- with its own token, because it
is the VAs' base rather than the fleet's. Nothing else in this repo reads it, so
the token is separate too: `VA_AIRTABLE_TOKEN` in the environment.

**Which rows are free.** `Used For` links a mailbox to the
`Profile Creation (VA)` row of the account created on it. 123 of the 170 rows
were linked on 2026-08-16, leaving 47. That link is the only claim on a mailbox
in the base, so writing it is what stops two runs racing onto one address.

**Which free rows are usable.** Signing a mailbox into a phone goes through
Google's own login, and every one of these mailboxes has 2-Step Verification on
or is silent about it. A row with a `2FA Secret Key` can be signed in
unattended (see `totp.py`); one without it needs a person. So the key is what
sorts the pool, not the password.
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

VA_BASE_ID = "appwUBCFYK7c5kVln"
GMAIL_TABLE = "tblllMfqrgRi7uHxz"           # "Gmail Accounts"
PROFILE_CREATION_TABLE = "tblysCsjvtYNyAdto"    # "Profile Creation (VA)"

FIELD_ADDRESS = "Gmail Account"
FIELD_PASSWORD = "Password"
FIELD_TOTP = "2FA Secret Key"
FIELD_USED_FOR = "Used For"
FIELD_WORKS = "Works"

TIMEOUT = 30


class MailboxPoolError(Exception):
    pass


def _token(explicit: str | None = None) -> str:
    # `VA_AIRTABLE_TOKEN` first, because that is the token this base was set up
    # with. The bot's own `AIRTABLE_TOKEN` was rejected here when this module
    # was written and now reaches the base -- checked 2026-08-21, HTTP 200 --
    # so the fleet's token is a real fallback rather than a guess. Keeping the
    # dedicated one ahead of it means nothing changes for anyone who has it.
    token = (explicit or os.environ.get("VA_AIRTABLE_TOKEN")
             or os.environ.get("AIRTABLE_TOKEN"))
    if not token:
        raise MailboxPoolError(
            "no token for the VA base. Set VA_AIRTABLE_TOKEN (or the fleet's "
            "AIRTABLE_TOKEN, which also reaches it).")
    return token


def _get(table: str, token: str, params: dict | None = None) -> dict:
    response = requests.get(
        f"https://api.airtable.com/v0/{VA_BASE_ID}/{table}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def all_mailboxes(token: str | None = None) -> list[dict]:
    token = _token(token)
    out, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        page = _get(GMAIL_TABLE, token, params)
        out.extend(page.get("records", []))
        offset = page.get("offset")
        if not offset:
            return out


def free_mailboxes(records=None, token: str | None = None) -> list[dict]:
    """Unclaimed rows that have everything a phone sign-in needs.

    Sorted so the ones that can be signed in without a person come first. Rows
    with no address at all are dropped -- the table has a few, one of them a
    bare profile id somebody pasted into the wrong column.
    """
    records = all_mailboxes(token) if records is None else records
    usable = []
    for record in records:
        fields = record.get("fields", {})
        address = str(fields.get(FIELD_ADDRESS) or "").strip()
        if "@" not in address:
            continue
        if fields.get(FIELD_USED_FOR):
            continue
        if not fields.get(FIELD_PASSWORD):
            continue
        if fields.get(FIELD_WORKS) is False:
            continue
        usable.append(record)

    return sorted(usable, key=lambda r: (
        0 if r["fields"].get(FIELD_TOTP) else 1,
        str(r["fields"].get(FIELD_ADDRESS) or "")))


def describe(record: dict) -> str:
    fields = record.get("fields", {})
    return (f"{str(fields.get(FIELD_ADDRESS)):46} "
            f"2fa_key={'yes' if fields.get(FIELD_TOTP) else 'NO '} "
            f"2fa={fields.get('2FA Enabled') or '-'}")


def record_created_account(record_id: str, identity, profile_name: str,
                           mlx_profile_id: str, token: str | None = None,
                           created_by: str = "adb_bot",
                           apply: bool = False) -> str:
    """Write the new account into `Profile Creation (VA)` and claim its mailbox.

    Claiming is the point: the `Used For` link is what stops the next run
    picking the same address, and it is only honest to write it once the
    account actually exists.

    Returns the new record id, or "" on a dry run.
    """
    token = _token(token)
    fields = {
        "Account Name": identity.full_name,
        "IG Username": identity.username,
        "IG Password": identity.password,
        "ML Profile ID": str(mlx_profile_id),
        "Email & Pass": f"{identity.email} & {identity.email_password}",
        "Created By": created_by,
        "Gmail Accounts": [record_id],
    }
    if not apply:
        print(f"  DRY RUN -- would add to Profile Creation (VA): {fields}")
        return ""

    response = requests.post(
        f"https://api.airtable.com/v0/{VA_BASE_ID}/{PROFILE_CREATION_TABLE}",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"fields": fields, "typecast": True}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json().get("id", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="The Gmail pool new accounts are created on.")
    parser.add_argument("--token", help="defaults to $VA_AIRTABLE_TOKEN")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--take", type=int, default=0,
                        help="show the N that would be used next")
    args = parser.parse_args(argv)

    try:
        records = all_mailboxes(args.token)
    except MailboxPoolError as exc:
        print(f"[stop] {exc}", file=sys.stderr)
        return 2

    free = free_mailboxes(records)
    with_key = [r for r in free if r["fields"].get(FIELD_TOTP)]
    print(f"\n{len(records)} mailbox(es), {len(free)} unclaimed, "
          f"{len(with_key)} of those with a 2FA key\n")

    if args.take:
        for record in free[:args.take]:
            print(f"  {describe(record)}  {record['id']}")
        short = args.take - len(free[:args.take])
        if short > 0:
            print(f"\n[warn] {short} short of {args.take}")
    elif args.list:
        for record in free:
            print(f"  {describe(record)}  {record['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
