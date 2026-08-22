"""Create Instagram accounts on the new Geelark phones, two at a time.

    python -m adb_bot.automation.signup_geelark --list
    python -m adb_bot.automation.signup_geelark --limit 2 --apply

Each run takes a phone tagged `new profile`, claims the next free mailbox from
the VA base, and drives the same four steps `signup_phone` has always done --
sign the mailbox into the phone, install Instagram and Gmail, create the
account off the emailed code, write the credentials down. The phone host is the
only difference, and it lives behind `GeelarkHost`.

**Why a mailbox and not a rented number.** An account made on an SMS number
cannot be recovered by anybody once the number is released -- roughly sixteen
fleet profiles are already in that hole. An account made on a mailbox we still
hold can be got back into.

**Two at a time.** Geelark sells four parallel slots, and a phone that is
running bills by the minute whether or not anything is driving it. Two leaves
headroom for a stuck phone to be cleaned up without queueing behind the batch.

Dry run by default. `--apply` is what launches phones and creates accounts.

Never prints a password.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from adb_bot.automation import signup_mailboxes
from adb_bot.automation.flows import signup
from adb_bot.automation.signup_phone import GeelarkHost, run_phone
from adb_bot.clients.adb import ADBClient
from adb_bot.clients.geelark import GeelarkTransport
from adb_bot.clients.geelark.phones import GeelarkPhoneClient
from adb_bot.core.logger import get_logger

NEW_TAG = "new profile"
CONNECTED_TAG = "IG connected"

# Where each run's outcome is appended, so a later run can see what has already
# been tried on a phone without asking Geelark to interpret its own remark.
LEDGER = Path.home() / ".adb_bot" / "signup" / "geelark_signups.jsonl"

# Batches worth spending a phone launch on, best first.
#
# Google answers an address it does not trust with a captcha *before* it asks
# for a password, and which batch an address came from predicts that better
# than anything in its own row: `cicireynaamelia@` from the 2026-08-12
# `akunbaru123@` batch signed in cleanly, while `hasan428483@` from another
# batch burned five and a half minutes on the robot check. Ten mailboxes
# sampled on 2026-08-20 across the older batches opened *none*, so an untested
# batch is worth less than an unproven one that is at least new.
PROVEN_BATCH = "akunbaru123@"
NEWEST_BATCH = "aass1122"

LOCK = threading.Lock()


def batch_rank(record: dict) -> int:
    password = str((record.get("fields") or {}).get("Password") or "")
    if password == PROVEN_BATCH:
        return 0
    if password == NEWEST_BATCH:
        return 1
    # A 2FA key still helps inside the tail: without one, Google's
    # authenticator step needs a person and the launch is wasted.
    return 2 if (record.get("fields") or {}).get("2FA Secret Key") else 3


def spent_locally() -> set[str]:
    """Addresses an account was already made on, whatever Airtable says.

    The base's `Used For` link is the intended claim, and it is missing for
    every account this pipeline made before today: `cicireynaamelia@` carries
    `@alina.sommer74` and still reads as free. Handing it out again would spend
    a launch on a mailbox that already holds an account -- these mailboxes take
    one each -- so the local record gets a veto over the remote one.
    """
    used: set[str] = set()
    assignments = Path.home() / ".adb_bot" / "mailboxes.json"
    if assignments.exists():
        try:
            for box in json.loads(assignments.read_text()).values():
                if box.get("address"):
                    used.add(str(box["address"]).lower())
        except ValueError:
            pass
    accounts = Path.home() / ".adb_bot" / "accounts" / "accounts.json"
    if accounts.exists():
        try:
            for row in json.loads(accounts.read_text()).values():
                if row.get("recovery_email"):
                    used.add(str(row["recovery_email"]).lower())
        except ValueError:
            pass
    return used


def mailbox_queue(token: str | None = None) -> list[dict]:
    """Free mailboxes usable by the email route, best batch first.

    A 2FA key is not a preference here, it is a requirement: signing the
    mailbox into the phone goes through Google's own login, which asks for an
    authenticator code, and a mailbox without the key cannot answer it. Ranking
    keyless mailboxes lower was not enough -- once the keyed ones were spent the
    run fell through to `adil32gmail@` from the `aass1122` batch (no key) and
    stuck at Google's email step. So they are excluded outright; an email run
    with no keyed mailbox left should report an empty queue, not fail on one it
    could never use.
    """
    used = spent_locally() | failed_mailboxes()
    free = [r for r in signup_mailboxes.free_mailboxes(token=token)
            if (r.get("fields") or {}).get("2FA Secret Key")
            and str((r.get("fields") or {}).get("Gmail Account") or "").lower()
            not in used]
    return sorted(free, key=lambda r: (
        batch_rank(r), str((r.get("fields") or {}).get("Gmail Account") or "")))


def new_phones(transport=None) -> list[dict]:
    """Geelark phones tagged `new profile`, in folder order."""
    phones = GeelarkPhoneClient(transport).list_phones()
    tagged = [p for p in phones
              if any((t or {}).get("name") == NEW_TAG
                     for t in (p.get("tags") or []))]
    return sorted(tagged, key=lambda p: (
        str((p.get("group") or {}).get("name") or ""),
        str(p.get("serialName") or "")))


def reached_instagram(status: str) -> bool:
    """Did this run get far enough for Instagram to have seen the address?

    The dividing line for both of the decisions below. `run_phone` prefixes its
    early exits -- `mailbox-...`, `install-...` -- and returns a bare signup
    result once the account chain itself starts. Only in that second case has
    anything irreversible happened.
    """
    status = str(status or "")
    if not status:
        return False
    early = ("mailbox-", "install-", "app-")
    return not (status.startswith(early)
                or status in ("dry-run", "busy", "not-ready", "unreachable"))


def already_attempted() -> set[str]:
    """Phone ids that have really been spent.

    A phone whose *mailbox* failed is untouched -- Instagram never opened on
    it -- so it belongs back in the queue with a different address. Retiring it
    would have thrown away a good phone for a bad Gmail row, and there are far
    more phones than working mailboxes to waste.
    """
    seen: set[str] = set()
    if not LEDGER.exists():
        return seen
    for line in LEDGER.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if reached_instagram(row.get("status")):
            seen.add(str(row.get("phone_id")))
    return seen


# Google verdicts that are about the *address* and will not change on another
# phone. Everything else that ends a sign-in -- a phone that would not dump, a
# screen nobody has named, a device that died -- is about the run, and the
# address goes back in the pool. Getting this list wrong in the generous
# direction retires good mailboxes we cannot replace: `stuck` was on it for one
# batch and cost five addresses from the only untested batch we had.
SETTLED_MAILBOX_VERDICTS = ("wrong_password", "google_robot_check")


def failed_mailboxes() -> set[str]:
    """Addresses Google itself has ruled out, so they are not offered again."""
    bad: set[str] = set()
    if not LEDGER.exists():
        return bad
    for line in LEDGER.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        status = str(row.get("status") or "")
        if not status.startswith("mailbox-"):
            continue
        if status[len("mailbox-"):] in SETTLED_MAILBOX_VERDICTS:
            bad.add(str(row.get("email") or "").lower())
    return bad


def record_outcome(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        with LEDGER.open("a") as handle:
            handle.write(json.dumps(row) + "\n")


def write_back(phone: dict, identity, address: str, status: str,
               transport, logger) -> None:
    """Put the result on the Geelark phone itself.

    The remark is the only place somebody looking at the Geelark console can
    see whose account this is. The `IG connected` tag means the account is
    signed in and usable -- so only a finished, verified signup earns it. An
    account sitting behind "confirm you're human" is real but cannot post, and
    tagging it would say the migration is further along than it is.
    """
    # Two different facts, kept in two different clauses. A run that died at
    # the Google sign-in has an Instagram handle in memory and no account
    # anywhere, and writing `IG:@someone` for it would invent one -- the same
    # conflation that once erased the knowledge that two accounts' Instagram
    # credentials were good.
    if reached_instagram(status):
        remark = (f"IG:@{identity.username} | PW:{identity.password} | "
                  f"MAIL:{address} | SIGNUP:{status}")
    else:
        remark = f"MAIL:{address} | SIGNUP-BLOCKED:{status} | no account made"
    try:
        from adb_bot.clients.geelark.tags import GeelarkTagClient

        tags = GeelarkTagClient(transport)
        # Resolved from names through the tag list, NOT read off the phone:
        # `/phone/list` returns each tag as a name with a **null id**, so
        # collecting ids from there yields an empty list -- and since `tagIDs`
        # REPLACES a phone's tags rather than adding to them, sending that
        # empty list strips every tag. Ten phones silently lost `new profile`
        # that way and dropped out of their own work queue.
        by_name = tags.tag_ids_by_name()
        wanted = [str(t.get("name")) for t in (phone.get("tags") or [])
                  if t.get("name")]
        if status == signup.RESULT_CREATED and CONNECTED_TAG not in wanted:
            wanted.append(CONNECTED_TAG)
            tags.ensure_tag(CONNECTED_TAG, "green")
            by_name = tags.tag_ids_by_name(refresh=True)
        tag_ids = [by_name[name] for name in wanted if name in by_name]
        if wanted and not tag_ids:
            # Better to leave the tags alone than to replace them with nothing.
            logger.warning("signup_geelark: could not resolve tags %s for %s; "
                           "leaving them as they are", wanted,
                           phone.get("serialName"))
            tag_ids = None
        GeelarkPhoneClient(transport).update_phone(
            str(phone["id"]), remark=remark, tag_ids=tag_ids)
    except Exception as exc:
        logger.warning("signup_geelark: could not write back to %s (%s)",
                       phone.get("serialName"), exc)


def claim_mailbox(record: dict, identity, phone: dict, apply: bool,
                  logger) -> str:
    """Flag the mailbox as used, so no later run picks the same address."""
    try:
        return signup_mailboxes.record_created_account(
            record["id"], identity, str(phone.get("serialName")),
            str(phone["id"]), created_by="adb_bot/geelark", apply=apply)
    except Exception as exc:
        logger.warning("signup_geelark: could not claim %s (%s)",
                       (record.get("fields") or {}).get("Gmail Account"), exc)
        return ""


def run_one(phone: dict, record: dict | None, args, logger,
            transport) -> dict:
    if record is None:
        box = None
    else:
        fields = record.get("fields") or {}
        box = {"address": str(fields.get("Gmail Account") or ""),
               "password": str(fields.get("Password") or ""),
               "totp_secret": str(fields.get("2FA Secret Key") or "")}
    item = {"id": str(phone["id"]), "serial_name": phone.get("serialName")}

    out = run_phone(item, box, GeelarkHost(transport, args), ADBClient(),
                    args, logger)
    out["phone_id"] = str(phone["id"])
    out["folder"] = (phone.get("group") or {}).get("name")
    out["mailbox_record"] = record["id"] if record else ""

    if args.apply:
        identity = out.get("identity")
        if identity is not None:
            status = str(out.get("status"))
            address = box["address"] if box else "(sms, no mailbox)"
            write_back(phone, identity, address, status, transport, logger)
            # Claimed only once Instagram has actually seen the address.
            # Claiming on a failed Google sign-in costs a pool row and writes a
            # `Profile Creation` entry for somebody who does not exist -- which
            # is exactly what the first batch did, twice, before this check.
            if record is None:
                pass
            elif reached_instagram(status):
                claim_mailbox(record, identity, phone, apply=True,
                              logger=logger)
            else:
                print(f"  mailbox left free: {box['address']} ({status})")
        record_outcome({k: v for k, v in out.items() if k != "identity"})
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Instagram accounts on the new Geelark phones.")
    parser.add_argument("--list", action="store_true",
                        help="show the phones and mailboxes that would be used")
    parser.add_argument("--limit", type=int, default=2,
                        help="how many phones to run in this batch")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="phones running at once (Geelark sells 4 slots)")
    parser.add_argument("--folder", action="append",
                        help="restrict to these model folders")
    parser.add_argument("--sms", action="store_true",
                        help="verify by rented SMS number instead of a "
                             "mailbox. Costs money per number, and the "
                             "account cannot be recovered afterwards -- use "
                             "when the mailbox pool cannot deliver")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="stop at the checkpoint instead of clearing it. "
                             "Verification rents SMS numbers, which cost money")
    parser.add_argument("--country", default=None,
                        help="two-letter country to rent numbers from, e.g. "
                             "GB. Defaults to the router's own default (DE), "
                             "which delivered nothing in 15 attempts on "
                             "2026-08-21 and costs twice what the UK does")
    parser.add_argument("--readiness-attempts", type=int, default=10)
    parser.add_argument("--readiness-wait", type=int, default=15)
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    transport = GeelarkTransport()

    phones = new_phones(transport)
    if args.folder:
        wanted = {f.lower() for f in args.folder}
        phones = [p for p in phones
                  if str((p.get("group") or {}).get("name") or "").lower()
                  in wanted]
    spent = already_attempted()
    phones = [p for p in phones if str(p["id"]) not in spent]

    if args.sms:
        mailboxes = [None] * len(phones)
        print(f"phones tagged {NEW_TAG!r} and untried: {len(phones)}")
        print("verifying by SMS -- no mailbox, and these accounts cannot be "
              "recovered once their number is released")
    else:
        mailboxes = mailbox_queue()
        print(f"phones tagged {NEW_TAG!r} and untried: {len(phones)}")
        print(f"free mailboxes: {len(mailboxes)}")
    pairs = list(zip(phones, mailboxes))[:max(0, args.limit)]

    print(f"this batch: {len(pairs)}\n")
    for phone, record in pairs:
        address = ((record.get("fields") or {}).get("Gmail Account")
                   if record else "(sms)")
        print(f"  {str(phone.get('serialName')):18} "
              f"{str((phone.get('group') or {}).get('name')):12} {address}")
    if args.list or not pairs:
        return 0
    if not args.apply:
        print("\nDRY RUN -- pass --apply to launch phones and create accounts")
        return 0

    results: list[dict] = []

    def worker(phone, record):
        try:
            results.append(run_one(phone, record, args, logger, transport))
        except Exception as exc:
            logger.warning("signup_geelark: %s blew up (%s)",
                           phone.get("serialName"), exc)
            results.append({"profile": phone.get("serialName"),
                            "status": "error", "detail": str(exc)[:300]})

    for start in range(0, len(pairs), max(1, args.concurrency)):
        batch = pairs[start:start + max(1, args.concurrency)]
        threads = [threading.Thread(target=worker, args=(p, r), daemon=True)
                   for p, r in batch]
        for thread in threads:
            thread.start()
        for thread in threads:
            # Generous, but bounded: these phones die by themselves at about
            # fifteen minutes and a wedged one must not hold the whole run.
            thread.join(20 * 60)
        time.sleep(5)

    print("\n" + "=" * 68)
    for row in results:
        print(f"  {str(row.get('profile')):18} {str(row.get('status')):22} "
              f"@{row.get('username', '')}")
    made = [r for r in results if r.get("status") == signup.RESULT_CREATED]
    held = [r for r in results
            if r.get("status") == signup.RESULT_CREATED_UNVERIFIED]
    print(f"\n{len(made)} usable, {len(held)} created but held at a "
          f"checkpoint, of {len(results)} attempted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
