"""Create Instagram accounts on MLX profiles, round-robining the four shared
mobile proxies.

    python -m adb_bot.automation.signup_mlx --profile "Blank caio 1" --profile "Blank caio 2"
    python -m adb_bot.automation.signup_mlx --profile "Blank caio 1" --apply

Drives the exact same chain `signup_phone.run_phone` has always run for MLX
(sign the mailbox in, install Instagram + Gmail, create the account off the
emailed code, verify if held at a checkpoint) -- that flow was written
against MultiLogin originally and needs no adaptation. What is new here is
the proxy: each profile in the batch leases one of the four shared mobile
proxies (`mlx_proxy_session`, the same lock directory Geelark's
`proxy_pool` uses) instead of whatever the profile already carries, so many
profiles can round-robin through the four over time without two ever
sharing an exit IP concurrently.

**Explicit `--profile` only, deliberately no auto-discovery.** Geelark's
batch picks up every phone tagged `new profile` automatically; MLX has no
confirmed equivalent tag in this workspace, and guessing one risks running
real signups against profiles nobody meant to spend. Pass the profiles by
name or id, the same way `signup_phone.py`'s own CLI already does.

**No write-back yet.** Unlike `signup_geelark.py`, this does not write a
remark or tag onto the MLX profile when it finishes -- that would need its
own convention (MLX profiles do carry a `remark` field, same endpoint as the
proxy write, but what it should say has not been decided). Results print to
stdout and go into the local ledger/mailbox bookkeeping only. Add write-back
once that convention exists.

Dry run by default. `--apply` is what launches phones, leases proxies and
creates accounts.

Never prints a password.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from adb_bot.automation import signup_mailboxes
from adb_bot.automation.flows import signup
from adb_bot.automation.mlx_proxy_session import MlxSharedProxyHost
from adb_bot.automation.signup_geelark import (
    claim_mailbox,
    mailbox_queue,
    reached_instagram,
)
from adb_bot.automation.signup_phone import run_phone
from adb_bot.automation.verification_probe import _find_profile, _profiles, _resolve_token
from adb_bot.clients.adb import ADBClient
from adb_bot.core.logger import get_logger

# Separate from Geelark's `geelark_signups.jsonl` -- these are different
# phones with a different lifecycle (an MLX profile is not "spent" the way a
# one-shot Geelark phone is), so already_attempted() reusing Geelark's
# ledger would misfile every MLX result under the wrong platform's history.
LEDGER = Path.home() / ".adb_bot" / "signup" / "mlx_signups.jsonl"

LOCK = threading.Lock()


def already_attempted() -> set[str]:
    """MLX profile ids this ledger already shows reached Instagram."""
    seen: set[str] = set()
    if not LEDGER.exists():
        return seen
    for line in LEDGER.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if reached_instagram(row.get("status")):
            seen.add(str(row.get("profile_id")))
    return seen


def record_outcome(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        with LEDGER.open("a") as handle:
            handle.write(json.dumps(row) + "\n")


def run_one(profile: dict, record: dict | None, args, logger,
           bearer_token: str) -> dict:
    if record is None:
        box = None
    else:
        fields = record.get("fields") or {}
        box = {"address": str(fields.get("Gmail Account") or ""),
               "password": str(fields.get("Password") or ""),
               "totp_secret": str(fields.get("2FA Secret Key") or "")}
    profile_id = str(profile.get("id"))
    item = {"id": profile_id, "serial_name": profile.get("serial_name")}

    host = MlxSharedProxyHost(bearer_token,
                              wait_for_lease_seconds=args.wait_for_lease,
                              readiness_attempts=args.readiness_attempts,
                              readiness_wait=args.readiness_wait)
    out = run_phone(item, box, host, ADBClient(), args, logger)
    out["profile_id"] = profile_id

    if args.apply:
        identity = out.get("identity")
        if identity is not None:
            status = str(out.get("status"))
            address = box["address"] if box else "(sms, no mailbox)"
            print(f"  {profile.get('serial_name')}: {status}"
                  + (f" @{identity.username}" if reached_instagram(status) else ""))
            # Claimed only once Instagram has actually seen the address --
            # see signup_geelark.run_one's identical guard.
            if record is not None:
                if reached_instagram(status):
                    claim_mailbox(record, identity, profile, apply=True,
                                 logger=logger)
                else:
                    print(f"  mailbox left free: {address} ({status})")
        record_outcome({k: v for k, v in out.items() if k != "identity"})
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Instagram accounts on MLX profiles, via the shared "
                    "mobile-proxy pool.")
    parser.add_argument("--profile", action="append", required=True,
                        help="MLX profile name or id; repeat for several")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="profiles running at once (bounded by the 4 "
                             "shared proxies regardless of this number)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="stop at the checkpoint instead of clearing it. "
                             "Verification rents SMS numbers, which cost money")
    parser.add_argument("--country", default=None)
    parser.add_argument("--readiness-attempts", type=int, default=10)
    parser.add_argument("--readiness-wait", type=int, default=15)
    parser.add_argument("--wait-for-lease", type=float, default=60.0,
                        help="seconds to wait for a free shared proxy port "
                             "before giving up on a profile this batch")
    args = parser.parse_args(argv)

    logger = get_logger("adb_bot")
    token = _resolve_token()

    items = _profiles(token)
    profiles = []
    for wanted in args.profile:
        item = _find_profile(items, wanted)
        if item is None:
            print(f"[skip] no MLX profile named {wanted!r}")
            continue
        profiles.append(item)

    spent = already_attempted()
    profiles = [p for p in profiles if str(p.get("id")) not in spent]

    mailboxes = mailbox_queue(token=token)
    print(f"profiles requested and untried: {len(profiles)}")
    print(f"free mailboxes: {len(mailboxes)}")
    pairs = list(zip(profiles, mailboxes))

    print(f"this batch: {len(pairs)}\n")
    for profile, record in pairs:
        address = (record.get("fields") or {}).get("Gmail Account") if record else "(sms)"
        print(f"  {str(profile.get('serial_name')):18} {address}")
    if not pairs:
        return 0
    if not args.apply:
        print("\nDRY RUN -- pass --apply to launch profiles and create accounts")
        return 0

    results: list[dict] = []

    def worker(profile, record):
        try:
            results.append(run_one(profile, record, args, logger, token))
        except Exception as exc:
            logger.warning("signup_mlx: %s blew up (%s)",
                           profile.get("serial_name"), exc)
            results.append({"profile_id": profile.get("id"),
                            "status": "error", "detail": str(exc)[:300]})

    for start in range(0, len(pairs), max(1, args.concurrency)):
        batch = pairs[start:start + max(1, args.concurrency)]
        threads = [threading.Thread(target=worker, args=(p, r), daemon=True)
                  for p, r in batch]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20 * 60)
        time.sleep(5)

    print("\n" + "=" * 68)
    for row in results:
        print(f"  {str(row.get('profile_id')):20} {str(row.get('status')):22} "
              f"@{row.get('username', '')}")
    made = [r for r in results if r.get("status") == signup.RESULT_CREATED]
    held = [r for r in results
            if r.get("status") == signup.RESULT_CREATED_UNVERIFIED]
    print(f"\n{len(made)} usable, {len(held)} created but held at a "
          f"checkpoint, of {len(results)} attempted")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
