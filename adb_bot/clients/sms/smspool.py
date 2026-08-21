"""SMSPool client -- the primary number provider.

API surface used here (all `POST`, form-encoded, key in the body):

    /purchase/sms    rent a number       -> success, number, cc, phonenumber, order_id
    /sms/check       poll for the code   -> status, sms, full_sms, time_left
    /sms/cancel      refund the number   -> success, message
    /request/balance account credit      -> balance
    /request/price   price + hit rate    -> price, high_price, success_rate

A note on `/sms/check` statuses, because it drives the polling loop:

SMSPool answers with a numeric `status`, and its public documentation only
spells out one of them (6 = refunded). Rather than guess the rest and have the
loop hang on a code that already arrived -- or spin on an order that is already
dead -- the code below decides in this order:

1. Any non-empty `sms` / `full_sms` means the code is here. This is authoritative
   and independent of the status number.
2. `status` in `_DEAD_STATUSES` means the order is over (refunded/cancelled/
   expired) and polling should stop early instead of burning the full 45s.
3. Anything else -- including a status we have never seen -- is "still pending".

That ordering is deliberately biased toward "keep waiting": mistaking pending
for dead throws away a number that was about to deliver, while mistaking dead
for pending only costs the remainder of the timeout, which the router is already
budgeting for. `_DEAD_STATUSES` is the one thing to correct if SMSPool ever
publishes the full table.
"""

from __future__ import annotations

import requests

from adb_bot.clients.sms.base import (
    COUNTRY_DE,
    COUNTRY_GB,
    COUNTRY_US,
    PROVIDER_SMSPOOL,
    DEFAULT_COUNTRY,
    InsufficientBalance,
    NoNumbersAvailable,
    NumberOrder,
    SERVICE_INSTAGRAM,
    SmsProviderError,
    digits_only,
    extract_code,
)

API_BASE = "https://api.smspool.net"
HTTP_TIMEOUT = 30

# Canonical name -> SMSPool numeric id. Confirmed live against
# /service/retrieve_all and /country/retrieve_all on 2026-08-11.
_SERVICE_IDS = {
    SERVICE_INSTAGRAM: 457,       # "Instagram / Threads"
}
_COUNTRY_IDS = {
    COUNTRY_US: 1,                # "United States". 22 is "United States (Virtual)";
                                  # virtuals are cheaper but Instagram rejects many of
                                  # them, so the real pool is the default.
    COUNTRY_GB: 2,                # "United Kingdom" (cc 44). $0.30 across four
                                  # separate pools -- half what Germany costs,
                                  # and a different carrier range, which is the
                                  # point: on 2026-08-21 fifteen German numbers
                                  # from BOTH providers delivered nothing and
                                  # every one of them came out of the same
                                  # +49 1590 56xx block.
    COUNTRY_DE: 24,               # "Germany" (cc 49). The default -- the profiles are
                                  # German and the challenge screen's picker is +49.
                                  # Dearer and less reliable than the US pool here:
                                  # $0.60 at 56% vs $0.42 at 71% (checked 2026-08-11),
                                  # so expect the breaker to see more failures than
                                  # the US numbers would have produced.
}

# Documented: 6 = refunded. The others are inferred -- see the module docstring.
STATUS_PENDING = 1
STATUS_RECEIVED = 3
STATUS_REFUNDED = 6

# Orders that will never deliver a code, so polling can stop early.
#
# Confirmed 2026-08-11 against a real German order (BSWODSHR): once refunded,
# `/sms/check` answers `{"status": 6, "message": "This order has been
# refunded"}`. `/request/history` shows the vocabulary in words -- `refunded`,
# `expired`, `completed` -- but does not give their numbers, so only 6 is known
# for certain. Everything else still falls through to "keep waiting", which
# costs the remainder of the 45s at worst.
_DEAD_STATUSES = frozenset({STATUS_REFUNDED})

# Substrings SMSPool puts in `message` when the wallet is empty. Matched
# case-insensitively; an empty wallet must not be mistaken for a burned pool.
_BALANCE_MARKERS = ("insufficient balance", "not enough balance", "add funds",
                    "insufficient funds", "top up")

# ...and when the pool has nothing left for this service/country.
_NO_STOCK_MARKERS = ("no numbers", "out of stock", "no stock", "not available",
                     "no available", "sold out")


class SmsPoolProvider:
    """Rent / poll / refund numbers at SMSPool."""

    name = PROVIDER_SMSPOOL

    def __init__(self, api_key: str, api_base: str = API_BASE,
                 timeout: int = HTTP_TIMEOUT, session=None) -> None:
        if not api_key:
            raise ValueError("SMSPool API key is required")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

    # --- HTTP -----------------------------------------------------------------
    def _post(self, path: str, **params) -> dict:
        """POST a form-encoded call and return the decoded JSON object.

        SMSPool answers `200 OK` with `{"success": 0, "message": ...}` for
        application-level failures, so the status code alone proves nothing.
        """
        payload = {"key": self.api_key}
        payload.update({k: v for k, v in params.items() if v is not None})
        url = f"{self.api_base}{path}"
        try:
            response = self._session.post(url, data=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SmsProviderError(self.name, f"{path} failed: {exc}") from exc

        if response.status_code >= 500:
            raise SmsProviderError(
                self.name, f"{path} returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError:
            raise SmsProviderError(
                self.name,
                f"{path} returned non-JSON (HTTP {response.status_code}): "
                f"{response.text[:200]}") from None

        if not isinstance(body, dict):
            raise SmsProviderError(self.name, f"{path} returned {type(body).__name__}")
        return body

    @staticmethod
    def _message(body: dict) -> str:
        for key in ("message", "error", "errors"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (list, tuple)) and value:
                return "; ".join(str(item) for item in value)
        return ""

    # --- provider protocol ----------------------------------------------------
    def purchase(self, service: str = SERVICE_INSTAGRAM,
                 country: str = DEFAULT_COUNTRY) -> NumberOrder:
        service_id = _SERVICE_IDS.get(service)
        country_id = _COUNTRY_IDS.get(country)
        if service_id is None:
            raise SmsProviderError(self.name, f"unmapped service {service!r}")
        if country_id is None:
            raise SmsProviderError(self.name, f"unmapped country {country!r}")

        body = self._post("/purchase/sms", country=country_id, service=service_id)
        if not _truthy(body.get("success")):
            message = self._message(body) or "purchase refused"
            lowered = message.lower()
            if any(marker in lowered for marker in _BALANCE_MARKERS):
                raise InsufficientBalance(self.name, message)
            if any(marker in lowered for marker in _NO_STOCK_MARKERS):
                raise NoNumbersAvailable(self.name, message)
            # An unrecognised refusal is treated as a pool failure rather than a
            # crash: the fleet must keep moving, and the router's counter is what
            # notices if it keeps happening.
            raise NoNumbersAvailable(self.name, message)

        order_id = str(body.get("order_id") or "").strip()
        phone = digits_only(body.get("number") or body.get("phonenumber"))
        if not order_id or not phone:
            raise SmsProviderError(
                self.name, f"purchase gave no order id / number: {body}")

        country_code = digits_only(body.get("cc")) or None
        national = digits_only(body.get("phonenumber")) or None
        # SMSPool sometimes repeats the full international number in
        # `phonenumber`; only keep it as the national part when it is shorter.
        if national and country_code and not phone.endswith(national):
            national = None
        if national and national == phone:
            national = None

        return NumberOrder(
            provider=self.name,
            order_id=order_id,
            phone=phone,
            country=country,
            country_code=country_code,
            national_number=national,
            cost=_as_float(body.get("cost") or body.get("price")),
        )

    def poll_code(self, order: NumberOrder) -> str | None:
        body = self._post("/sms/check", orderid=order.order_id)

        # `sms` is the code SMSPool already extracted; `full_sms` is the whole
        # message body, so it has to be parsed rather than returned as-is.
        raw = _first_nonempty(body.get("sms"), body.get("full_sms"))
        if raw:
            code = extract_code(raw)
            if code:
                return code
            raise SmsProviderError(
                self.name, f"order {order.order_id} delivered an SMS with no "
                           f"readable code: {raw[:120]!r}")

        status = _as_int(body.get("status"))
        if status in _DEAD_STATUSES:
            raise SmsProviderError(
                self.name,
                f"order {order.order_id} is closed (status {status}: "
                f"{self._message(body) or 'refunded/expired'})")

        # An order SMSPool has forgotten answers `{"success": 0, "message":
        # "We could not find this order!"}` -- no `status` field at all.
        # Confirmed 2026-08-11 against an order from an earlier day. Without
        # this it falls through to "still pending" and the loop waits out the
        # full 45 seconds on an order that can never answer.
        if status is None and not _truthy(body.get("success")):
            raise SmsProviderError(
                self.name,
                f"order {order.order_id} is gone "
                f"({self._message(body) or 'no status returned'})")
        return None

    def cancel(self, order: NumberOrder) -> bool:
        try:
            body = self._post("/sms/cancel", orderid=order.order_id)
        except SmsProviderError:
            return False
        # An order SMSPool already refunded on its own answers success=0; that is
        # still the outcome we wanted, so do not treat it as a failure to cancel.
        if _truthy(body.get("success")):
            return True
        lowered = self._message(body).lower()
        return "refund" in lowered or "cancel" in lowered

    def finish(self, order: NumberOrder) -> bool:
        """No-op: SMSPool closes an order itself once its code is delivered.

        Present so the provider satisfies the same protocol as 5sim, which does
        need an explicit finish call.
        """
        return True

    def balance(self) -> float:
        body = self._post("/request/balance")
        return _as_float(body.get("balance")) or 0.0

    # --- extras (doctor / reporting, not part of the protocol) -----------------
    def price(self, service: str = SERVICE_INSTAGRAM,
              country: str = DEFAULT_COUNTRY) -> dict:
        """Current price and SMSPool's advertised success rate for the pool."""
        body = self._post("/request/price",
                          country=_COUNTRY_IDS.get(country),
                          service=_SERVICE_IDS.get(service))
        return {
            "price": _as_float(body.get("price")),
            "high_price": _as_float(body.get("high_price")),
            "success_rate": _as_float(body.get("success_rate")),
        }


# --- small coercions ----------------------------------------------------------
def _truthy(value) -> bool:
    """SMSPool returns `success` as 1, "1", or true depending on the endpoint."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip() in ("1", "true", "True")
    return False


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_nonempty(*values) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ("null", "none", "false", "0"):
            return text
    return None
