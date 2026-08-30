"""Provider fallback: hand out numbers, count failures, switch when a pool burns.

This is the piece the verification flow talks to. It owns the 45-second wait,
the refund-on-timeout, and the failure counting described in `breaker.py`.

The unit of work is a **lease** -- one number, rented for one attempt at one
account:

    with router.lease() as lease:
        type_into_instagram(lease.typed_number)
        submit()
        code = lease.wait_for_code()      # blocks up to 45s
        if code:
            type_into_instagram(code)

Leaving the `with` block always settles the order: a lease whose code was used
is *finished*, and one that was not is *cancelled* for a refund. Nothing rented
is left hanging, including when the flow raises half-way through -- a leaked
order is money gone and a number that stays "in use" at the provider.

`wait_for_code` returning `None` is the ordinary failed attempt, not an error:
it has already refunded the number and told the breaker. The caller's job is
just to take a fresh lease and retype -- which is where the fallback happens,
because the new lease may come from the other provider.
"""

from __future__ import annotations

import time

from adb_bot.clients.sms.base import (
    DEFAULT_COUNTRY,
    InsufficientBalance,
    NumberOrder,
    SERVICE_INSTAGRAM,
    SmsProvider,
    SmsProviderError,
)
from adb_bot.clients.sms.breaker import (
    CODE_WAIT_SECONDS,
    COOLDOWN_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    POLL_INTERVAL_SECONDS,
    SWITCH_WARNING,
    BreakerStore,
)


class AllProvidersFailed(RuntimeError):
    """No configured provider could rent a number for this attempt."""


class SmsRouter:
    """Pick a provider, lease numbers from it, and switch when it keeps failing.

    `providers` is in preference order -- primary first. `clock` and `sleep` are
    injectable so the breaker's timing rules can be tested without real waits.
    """

    def __init__(self, providers, store: BreakerStore | None = None, logger=None,
                 code_wait_seconds: int = CODE_WAIT_SECONDS,
                 poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
                 max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
                 cooldown_seconds: int = COOLDOWN_SECONDS,
                 clock=time.time, sleep=time.sleep) -> None:
        self.providers = list(providers or [])
        if not self.providers:
            raise ValueError("SmsRouter needs at least one provider")
        self.store = store or BreakerStore(clock=clock)
        self.logger = logger
        self.code_wait_seconds = code_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._sleep = sleep

    # --- provider selection ---------------------------------------------------
    def active_provider(self) -> SmsProvider:
        """The provider that should serve the next request."""
        return self._select(self.store.load(), self._clock())

    def _select(self, state, now: float) -> SmsProvider:
        for provider in self.providers:
            if not state.is_cooling(provider.name, now):
                return provider
        # Everything is cooling down. Use whichever recovers first rather than
        # refuse to work -- see breaker.py.
        soonest = min(self.providers,
                      key=lambda p: state.cooldowns.get(p.name, 0.0))
        self._log("warning",
                  "sms: every provider is in cooldown; using %s anyway",
                  soonest.name)
        return soonest

    def _ordered_from(self, active: SmsProvider) -> list:
        """`active` first, then the rest in preference order."""
        return [active] + [p for p in self.providers if p.name != active.name]

    # --- leasing --------------------------------------------------------------
    def lease(self, service: str = SERVICE_INSTAGRAM,
              country: str = DEFAULT_COUNTRY) -> "NumberLease":
        """Rent one number, falling through to the next provider if one refuses.

        A provider that cannot sell a number *is* a failing provider -- a burned
        pool shows up as "no numbers available" just as often as it shows up as a
        number that never receives -- so a refusal counts toward the breaker the
        same way a timeout does.
        """
        state = self.store.load()
        active = self._select(state, self._clock())
        errors = []
        broke = []

        for provider in self._ordered_from(active):
            try:
                order = provider.purchase(service=service, country=country)
            except InsufficientBalance as exc:
                # **Each provider has its own wallet.** SMSPool running dry
                # says nothing about 5sim, so this falls through to the next
                # one rather than failing the run -- on 2026-08-13 a signup
                # died on SMSPool's $0.02 while 5sim held $6.89 and was never
                # asked. It is still not a *pool* problem, so it does not count
                # toward the breaker: an empty wallet is not evidence that the
                # numbers are burned.
                broke.append(f"{provider.name} ({exc})")
                self._log("warning", "sms: %s is out of money (%s); trying the "
                                     "next provider", provider.name, exc)
                continue
            except SmsProviderError as exc:
                errors.append(str(exc))
                self._log("warning", "sms: %s could not sell a number (%s)",
                          provider.name, exc)
                self._record_failure(provider.name)
                continue

            self._log("info", "sms: leased %s from %s", order.e164, provider.name)
            return NumberLease(self, provider, order)

        if broke and not errors:
            # Every provider is out of money. That really does need a person,
            # and saying so beats a generic "nobody could sell a number".
            raise InsufficientBalance(
                "all", "every SMS provider is out of money -- top one up: "
                       + "; ".join(broke))

        raise AllProvidersFailed(
            "no provider could rent a number: " + "; ".join(errors + broke))

    # --- outcome recording ----------------------------------------------------
    def record_success(self, provider_name: str) -> None:
        """A code arrived: clear the consecutive-failure count."""
        with self.store.mutate() as state:
            if state.consecutive_failures:
                self._log("info",
                          "sms: %s delivered a code; failure count %d -> 0",
                          provider_name, state.consecutive_failures)
            state.consecutive_failures = 0

    def _record_failure(self, provider_name: str) -> None:
        """An attempt produced no code. Count it, and trip if we hit the limit."""
        now = self._clock()
        with self.store.mutate() as state:
            state.consecutive_failures += 1
            state.last_failure_at = now
            count = state.consecutive_failures
            self._log("warning", "sms: %s failed (%d/%d consecutive)",
                      provider_name, count, self.max_consecutive_failures)
            if count >= self.max_consecutive_failures:
                self._trip(state, provider_name, now)

    def _trip(self, state, provider_name: str, now: float) -> None:
        """Cool the failing provider down and let selection move to the other."""
        state.cooldowns[provider_name] = now + self.cooldown_seconds
        state.consecutive_failures = 0
        state.last_switch_at = now

        successor = self._select(state, now)
        self._log("warning", "%s (%s -> %s; %s benched for %d min)",
                  SWITCH_WARNING, provider_name, successor.name, provider_name,
                  self.cooldown_seconds // 60)

    # --- logging --------------------------------------------------------------
    def _log(self, level: str, message: str, *args) -> None:
        if self.logger is None:
            return
        handler = getattr(self.logger, level, None)
        if handler:
            handler(message, *args)


class NumberLease:
    """One rented number, and the 45-second wait for its code.

    Use as a context manager so the order is always settled -- see the module
    docstring.
    """

    def __init__(self, router: SmsRouter, provider: SmsProvider,
                 order: NumberOrder) -> None:
        self.router = router
        self.provider = provider
        self.order = order
        self.code: str | None = None
        self._settled = False

    # --- what the flow types --------------------------------------------------
    @property
    def phone(self) -> str:
        return self.order.phone

    @property
    def e164(self) -> str:
        return self.order.e164

    @property
    def typed_number(self) -> str:
        """The digits to put in Instagram's phone field.

        Instagram pairs the field with a country picker that is already set, so
        the national part is what belongs in the box when the provider told us
        where the split is; otherwise the full international number goes in.
        """
        return self.order.national_number or self.order.phone

    # --- the wait -------------------------------------------------------------
    def wait_for_code(self, timeout: float | None = None) -> str | None:
        """Poll until the code arrives or the budget runs out.

        On success the breaker's counter is reset and the code is returned. On
        timeout -- or on an order the provider declares dead early -- the number
        is refunded, the failure is counted, and `None` comes back.
        """
        budget = self.router.code_wait_seconds if timeout is None else timeout
        deadline = self.router._clock() + budget

        while True:
            try:
                code = self.provider.poll_code(self.order)
            except SmsProviderError as exc:
                # The order is closed (refunded/cancelled/expired at the
                # provider's end). No point spending the rest of the budget.
                self.router._log("warning", "sms: %s stopped early (%s)",
                                 self.order, exc)
                self._fail()
                return None

            if code:
                self.code = code
                self.router._log("info", "sms: %s received code after %.0fs",
                                 self.order, budget - (deadline - self.router._clock()))
                self.router.record_success(self.provider.name)
                return code

            remaining = deadline - self.router._clock()
            if remaining <= 0:
                self.router._log("warning",
                                 "sms: no code from %s within %ss; refunding",
                                 self.order, int(budget))
                self._fail()
                return None
            self.router._sleep(min(self.router.poll_interval_seconds, remaining))

    # --- settling -------------------------------------------------------------
    def _fail(self) -> None:
        """Refund the number and count the failure. Safe to call once."""
        if self._settled:
            return
        self._settled = True
        self._cancel_quietly()
        self.router._record_failure(self.provider.name)

    def _cancel_quietly(self) -> None:
        try:
            refunded = self.provider.cancel(self.order)
        except Exception as exc:                      # never mask the real outcome
            self.router._log("warning", "sms: cancel of %s raised (%s)",
                             self.order, exc)
            return
        if not refunded:
            # 5sim refuses an explicit cancel inside its own minimum window, but
            # refunds the order itself when it times out -- so this is a note,
            # not lost money. See fivesim.py.
            self.router._log("info",
                             "sms: %s was not cancelled on request; the provider "
                             "will refund it when it expires", self.order)

    def release(self, count_failure: bool = True) -> None:
        """Settle the lease: finish it if its code was used, refund it if not.

        `count_failure=False` refunds the number **without** counting it against
        the provider's circuit breaker. For the case the breaker must never see:
        Instagram declining to send at all ("code not sent: try again later or
        use a different mobile number"). The provider delivered a working
        number; nothing about that is its fault, and on 2026-08-12 two such
        refusals in one run pushed the breaker from 4/10 to 6/10 -- two-thirds
        of the way to switching providers over Instagram's behaviour.
        """
        if self._settled:
            return
        self._settled = True
        if self.code:
            try:
                self.provider.finish(self.order)
            except Exception as exc:
                self.router._log("warning", "sms: finish of %s raised (%s)",
                                 self.order, exc)
            return
        if not count_failure:
            self._cancel_quietly()
            self.router._log("info", "sms: gave %s back without counting it "
                                     "against %s -- the number was fine, the "
                                     "send was refused", self.order,
                             self.provider.name)
            return
        # Rented but never waited on (the flow gave up, or raised). Give it back;
        # this is an abandoned attempt, so it counts like any other failure.
        self._settled = False
        self._fail()

    def __enter__(self) -> "NumberLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


# --- construction -------------------------------------------------------------
def build_router(logger=None, **kwargs) -> SmsRouter:
    """The configured router: 5sim primary, SMSPool fallback.

    Swapped 2026-08-30 (was SMSPool primary) -- SMSPool's wallet is empty and
    5sim's has just been topped up; 5sim's own historical delivery rate (289
    orders, 91.7 rating) is also the better of the two, see
    [[adbbot-sms-verification-providers]].

    A provider with no credential is left out rather than constructed and left
    to fail on every call, so running with only one key configured degrades to
    "one provider, no fallback" instead of erroring on every lease.
    """
    from adb_bot.clients.sms.fivesim import FiveSimProvider
    from adb_bot.clients.sms.smspool import SmsPoolProvider
    from adb_bot.config.settings import (
        get_saved_fivesim_token,
        get_saved_smspool_key,
    )

    providers = []
    fivesim_token = get_saved_fivesim_token()
    if fivesim_token:
        providers.append(FiveSimProvider(fivesim_token))
    smspool_key = get_saved_smspool_key()
    if smspool_key:
        providers.append(SmsPoolProvider(smspool_key))

    if not providers:
        raise RuntimeError(
            "No SMS provider configured. Set SMSPOOL_API_KEY and/or FIVESIM_TOKEN "
            "(normally from /etc/adbbot/env), or save `smspool_api_key` / "
            "`fivesim_token` in the settings file.")
    if len(providers) == 1 and logger:
        logger.warning("sms: only %s is configured -- there is no fallback "
                       "provider to switch to", providers[0].name)
    return SmsRouter(providers, logger=logger, **kwargs)
