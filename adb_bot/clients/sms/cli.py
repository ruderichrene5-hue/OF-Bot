"""Command line for the SMS layer -- check credentials, inspect and reset the breaker.

    python -m adb_bot.clients.sms.cli balance     # credit at every provider
    python -m adb_bot.clients.sms.cli state       # which provider is active, and why
    python -m adb_bot.clients.sms.cli reset       # clear the failure count / cooldowns
    python -m adb_bot.clients.sms.cli rent        # rent a real number end to end

`state` is the one to reach for when someone asks why verification suddenly
started using 5sim: it prints the failure count, which provider is benched and
for how long. `reset` is the manual override for when a provider has been fixed
and there is no reason to sit out the rest of its 30 minutes.

`rent` spends real money (a US Instagram number is ~$0.25-0.42). It rents one,
waits the full 45 seconds, and always gives the number back, so it exercises the
purchase/poll/refund path exactly as the flow does.
"""

from __future__ import annotations

import argparse
import sys
import time

from adb_bot.clients.sms.base import DEFAULT_COUNTRY, SERVICE_INSTAGRAM
from adb_bot.clients.sms.breaker import BreakerStore
from adb_bot.clients.sms.router import build_router
from adb_bot.core.logger import get_logger


def _fmt_age(seconds: float) -> str:
    minutes, secs = divmod(int(max(0, seconds)), 60)
    return f"{minutes}m{secs:02d}s"


def _router_with_scratch_breaker(router):
    """The same providers, but writing its breaker state to a temp file.

    A diagnostic must not be able to bench a production provider.
    """
    import tempfile
    from pathlib import Path

    from adb_bot.clients.sms.breaker import BreakerStore
    from adb_bot.clients.sms.router import SmsRouter

    scratch = Path(tempfile.mkdtemp(prefix="adbbot-sms-check-")) / "breaker.json"
    return SmsRouter(router.providers, store=BreakerStore(path=scratch),
                     logger=router.logger)


def cmd_balance(router, _args) -> int:
    for provider in router.providers:
        try:
            print(f"{provider.name:>10}: {provider.balance():.2f}")
        except Exception as exc:
            print(f"{provider.name:>10}: unavailable ({exc})")

    try:
        from adb_bot.clients.captcha import build_solver
        solver = build_solver()
        if hasattr(solver, "balance"):
            print(f"{solver.name:>10}: {solver.balance():.2f}")
        else:
            print(f"{'captcha':>10}: no solver configured")
    except Exception as exc:
        print(f"{'captcha':>10}: unavailable ({exc})")
    return 0


def cmd_state(router, _args) -> int:
    state = router.store.load()
    now = time.time()
    active = router.active_provider()

    print(f"configured  : {', '.join(p.name for p in router.providers)}")
    print(f"active      : {active.name}")
    print(f"failures    : {state.consecutive_failures}"
          f" / {router.max_consecutive_failures} before switching")
    for provider in router.providers:
        until = state.cooling_until(provider.name, now)
        if until:
            print(f"  {provider.name}: benched for another {_fmt_age(until - now)}")
        else:
            print(f"  {provider.name}: available")
    if state.last_switch_at:
        print(f"last switch : {_fmt_age(now - state.last_switch_at)} ago")
    if state.last_failure_at:
        print(f"last failure: {_fmt_age(now - state.last_failure_at)} ago")
    print(f"state file  : {router.store.path}")
    return 0


def cmd_reset(router, _args) -> int:
    with router.store.mutate() as state:
        state.consecutive_failures = 0
        state.cooldowns = {}
    print("breaker reset: no failures recorded, no provider benched")
    return 0


def cmd_rent(router, args) -> int:
    """Rent one real number and give it back -- the live end-to-end check.

    **No code can arrive during this check.** Nothing has asked Instagram to
    send one, so the 45-second wait always times out. That is a successful run
    of the plumbing, not a failing pool -- but the router cannot tell the
    difference, and the first real run of this quietly left the production
    breaker at 1/10 with SMSPool blamed for it. Ten diagnostics in a row would
    have benched the primary provider for half an hour over nothing.

    So by default this runs against a scratch breaker file and leaves the real
    one untouched. `--count-failures` opts back in, for the rare case where you
    want the diagnostic to feed the live counter.
    """
    logger = get_logger("adb_bot")
    if not args.count_failures:
        router = _router_with_scratch_breaker(router)
        print("(using a scratch breaker file; the live failure count is "
              "untouched -- pass --count-failures to change that)")
    print(f"renting a {args.country} {args.service} number "
          f"from {router.active_provider().name}...")

    with router.lease(service=args.service, country=args.country) as lease:
        print(f"  provider : {lease.provider.name}")
        print(f"  order    : {lease.order.order_id}")
        print(f"  number   : {lease.e164}   (typed as {lease.typed_number})")
        print(f"  waiting {router.code_wait_seconds}s for a code...")

        started = time.time()
        code = lease.wait_for_code()
        elapsed = time.time() - started

        if code:
            print(f"  code     : {code}  (after {elapsed:.0f}s)")
            print("  the order will be marked finished")
        else:
            print(f"  no code in {elapsed:.0f}s -- the number was refunded and "
                  f"one failure was counted")
    logger.info("sms rent check finished")
    return 0


COMMANDS = {
    "balance": cmd_balance,
    "state": cmd_state,
    "reset": cmd_reset,
    "rent": cmd_rent,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adb_bot.clients.sms.cli",
        description="Inspect and exercise the SMS verification providers.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--service", default=SERVICE_INSTAGRAM)
    parser.add_argument("--country", default=DEFAULT_COUNTRY,
                        help=f"canonical country to rent from "
                             f"(default {DEFAULT_COUNTRY}).")
    parser.add_argument("--count-failures", action="store_true",
                        help="rent: let this diagnostic's inevitable timeout count "
                             "against the live circuit breaker. Off by default -- "
                             "no code can arrive during a bare rent, so counting it "
                             "blames the provider for the test.")
    args = parser.parse_args(argv)

    try:
        router = build_router(logger=get_logger("adb_bot"))
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    return COMMANDS[args.command](router, args)


if __name__ == "__main__":
    raise SystemExit(main())
