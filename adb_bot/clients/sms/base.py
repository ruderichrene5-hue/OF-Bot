"""The contract every SMS provider implements, plus the shared value types.

The router (`router.py`) and the on-device flow only ever see the types in this
module, so adding a third provider later means writing one class here-shaped and
listing it in `build_router` -- nothing else changes.

Two deliberate choices:

- **Canonical service/country names.** Callers ask for `SERVICE_INSTAGRAM` and
  `COUNTRY_US`; each provider translates those into its own ids (SMSPool wants
  numeric ids -- service 457, country 1 -- while 5sim wants the strings
  "instagram" and "usa"). Provider ids never leak upward, so a caller can be
  handed either provider and behave identically.

- **`poll_code` is a single non-blocking check, not a wait loop.** The 45-second
  budget and what happens when it runs out are policy, and policy lives in the
  router where the failure counter is. A provider that blocked internally would
  make that policy untestable without real sleeps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# --- provider names (also the keys used in the persisted breaker state) -------
PROVIDER_SMSPOOL = "smspool"
PROVIDER_5SIM = "5sim"

# --- canonical service / country names ----------------------------------------
SERVICE_INSTAGRAM = "instagram"
COUNTRY_US = "US"


class SmsProviderError(RuntimeError):
    """A provider's API refused a call, or answered something unusable.

    Carries the provider name so a log line identifies the culprit without the
    caller having to add it.
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message


class NoNumbersAvailable(SmsProviderError):
    """The provider has no number left for this service/country right now.

    Distinct from a generic error because it is the *expected* way a burned pool
    presents itself, and the router treats it as a provider failure (it counts
    toward the consecutive-failure total) rather than a bug.
    """


class InsufficientBalance(SmsProviderError):
    """The provider account is out of credit.

    Never counted as a pool failure: switching providers does not fix an empty
    wallet, and silently burning through the fallback's balance too would just
    take both providers down. The router re-raises this to the caller.
    """


@dataclass(frozen=True)
class NumberOrder:
    """One purchased number, and the handle needed to poll/cancel it.

    `phone` is international digits with no punctuation and no leading `+`
    ("15309040849"), because that is the only shape both providers agree on and
    the only shape safe to type into a field character by character.

    `country_code` / `national_number` are filled in when the provider tells us
    where the split is -- Instagram's phone field is usually preceded by a
    country picker, so the flow types only the national part when it knows it,
    and falls back to the full international string when it does not.
    """

    provider: str
    order_id: str
    phone: str
    country: str
    country_code: str | None = None
    national_number: str | None = None
    cost: float | None = None

    @property
    def e164(self) -> str:
        return f"+{self.phone}"

    def __str__(self) -> str:
        return f"{self.provider}:{self.order_id}:{self.e164}"


@runtime_checkable
class SmsProvider(Protocol):
    """What the router needs from a provider. Implemented by both clients."""

    name: str

    def purchase(self, service: str, country: str) -> NumberOrder:
        """Rent a number. Raises `NoNumbersAvailable` when the pool is empty."""

    def poll_code(self, order: NumberOrder) -> str | None:
        """Return the verification code if it has arrived, else None.

        Returns rather than raises when the code simply is not there yet -- that
        is the normal case on every poll but the last.
        """

    def cancel(self, order: NumberOrder) -> bool:
        """Give the number back for a refund. True when the provider accepted."""

    def finish(self, order: NumberOrder) -> bool:
        """Mark the order used, once a code has been read off it."""

    def balance(self) -> float:
        """Account credit, for the doctor check and for logging."""


# Instagram's SMS codes are six digits. The range is kept slightly wider so a
# format change does not silently stop matching, but it stays anchored on word
# boundaries so the "885485" in a message is picked up and a phone number or a
# year in the same text is not.
_CODE_PATTERN = re.compile(r"\b(\d{4,8})\b")


def extract_code(text: str | None) -> str | None:
    """Pull the verification code out of a raw SMS body.

    Both providers usually hand back the code already separated, but both also
    have paths that only give the full message ("885485 is your Instagram code.
    Don't share it."), and one provider strips the spaces out of it first --
    which is why this falls back to scanning an unspaced string too.
    """
    if not text:
        return None
    raw = str(text).strip()
    if raw.isdigit() and 4 <= len(raw) <= 8:
        return raw
    match = _CODE_PATTERN.search(raw)
    if match:
        return match.group(1)
    # 5sim has been seen returning the message with every space removed
    # ("885485isyourInstagramcode."), which leaves no word boundary to anchor on.
    unspaced = re.search(r"(\d{4,8})", raw)
    return unspaced.group(1) if unspaced else None


def digits_only(raw: str | None) -> str:
    """Strip `+`, spaces, dashes and brackets from a provider's phone string.

    Providers are inconsistent -- 5sim answers "+15309040849", SMSPool answers
    "15309040849" in one field and "5309040849" in another -- and a stray `+`
    typed into Instagram's field silently fails the form.
    """
    if not raw:
        return ""
    return "".join(ch for ch in str(raw) if ch.isdigit())
