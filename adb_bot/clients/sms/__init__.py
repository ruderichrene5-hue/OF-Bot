"""Disposable phone numbers for Instagram's SMS verification challenge.

Two providers sit behind one interface -- SMSPool (primary) and 5sim (fallback)
-- so the verification flow never has to know which one it is talking to, and a
burned number pool at one provider routes to the other without a human.

Entry points:

- :func:`build_router` -- the configured, ready-to-use router. This is what
  callers want.
- :class:`SmsRouter` -- the circuit breaker / fallback policy.
- :class:`NumberLease` -- one number, leased for one verification attempt.

See `breaker.py` for the failure-counting rules and `base.py` for the provider
contract.
"""

from adb_bot.clients.sms.base import (
    COUNTRY_US,
    PROVIDER_5SIM,
    PROVIDER_SMSPOOL,
    InsufficientBalance,
    NoNumbersAvailable,
    NumberOrder,
    SERVICE_INSTAGRAM,
    SmsProviderError,
)
from adb_bot.clients.sms.breaker import BreakerState, BreakerStore
from adb_bot.clients.sms.fivesim import FiveSimProvider
from adb_bot.clients.sms.router import NumberLease, SmsRouter, build_router
from adb_bot.clients.sms.smspool import SmsPoolProvider

__all__ = [
    "BreakerState",
    "BreakerStore",
    "COUNTRY_US",
    "FiveSimProvider",
    "InsufficientBalance",
    "NoNumbersAvailable",
    "NumberLease",
    "NumberOrder",
    "PROVIDER_5SIM",
    "PROVIDER_SMSPOOL",
    "SERVICE_INSTAGRAM",
    "SmsPoolProvider",
    "SmsProviderError",
    "SmsRouter",
    "build_router",
]
