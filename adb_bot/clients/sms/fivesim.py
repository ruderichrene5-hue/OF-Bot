"""5sim client -- the fallback number provider.

API surface used here (all `GET`, bearer token in the header):

    /user/profile                                  account balance
    /user/buy/activation/{country}/{operator}/{product}   rent a number
    /user/check/{id}                               poll for the code
    /user/finish/{id}                              mark the order used
    /user/cancel/{id}                              refund the number

Two things about 5sim that shape this client:

**Errors are plain text, not JSON.** A failed buy answers `400` with a bare body
like `no free phones` or `not enough user balance`, and a bad id answers `404
order not found`. So a `json()` call cannot be the thing that decides success --
the status code and the text are.

**A timed-out order refunds itself.** An order 5sim never delivered ends up
`TIMEOUT` and the money comes back without anyone calling `/user/cancel`. That
matters because the router cancels at 45 seconds, which is inside the window
where 5sim can still refuse an explicit cancel: a refused cancel here is not
lost money, it just means the refund arrives when the order expires instead of
immediately. `cancel` therefore reports failure without raising, and the router
treats it as a soft outcome.
"""

from __future__ import annotations

import requests

from adb_bot.clients.sms.base import (
    COUNTRY_DE,
    DEFAULT_COUNTRY,
    DIALLING_CODES,
    COUNTRY_US,
    PROVIDER_5SIM,
    InsufficientBalance,
    NoNumbersAvailable,
    NumberOrder,
    SERVICE_INSTAGRAM,
    SmsProviderError,
    digits_only,
    extract_code,
)

API_BASE = "https://5sim.net/v1"
HTTP_TIMEOUT = 30

# Canonical name -> 5sim product / country slug.
_PRODUCTS = {
    SERVICE_INSTAGRAM: "instagram",
}
_COUNTRIES = {
    COUNTRY_US: "usa",
    COUNTRY_DE: "germany",
}
# "any" lets 5sim pick the cheapest operator with stock, which is what keeps the
# fallback useful when one operator's pool is the burned one.
DEFAULT_OPERATOR = "any"

# Order statuses, as 5sim spells them.
STATUS_PENDING = "PENDING"
STATUS_RECEIVED = "RECEIVED"
STATUS_CANCELED = "CANCELED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_FINISHED = "FINISHED"
STATUS_BANNED = "BANNED"

# An order in one of these will never deliver a code, so polling stops early.
_DEAD_STATUSES = frozenset({STATUS_CANCELED, STATUS_TIMEOUT, STATUS_BANNED})

_NO_STOCK_MARKERS = ("no free phones", "no product", "no free numbers")
_BALANCE_MARKERS = ("not enough user balance", "not enough balance")


class FiveSimProvider:
    """Rent / poll / refund numbers at 5sim."""

    name = PROVIDER_5SIM

    def __init__(self, token: str, api_base: str = API_BASE,
                 timeout: int = HTTP_TIMEOUT, operator: str = DEFAULT_OPERATOR,
                 session=None) -> None:
        if not token:
            raise ValueError("5sim authorization token is required")
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.operator = operator
        self._session = session or requests.Session()

    # --- HTTP -----------------------------------------------------------------
    def _get(self, path: str) -> tuple[int, dict | None, str]:
        """GET `path`; return (status_code, parsed JSON or None, raw text)."""
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        try:
            response = self._session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SmsProviderError(self.name, f"{path} failed: {exc}") from exc

        text = (response.text or "").strip()
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = None
        return response.status_code, body, text

    def _get_json(self, path: str) -> dict:
        """GET `path`, insisting on a JSON object and a 2xx."""
        status, body, text = self._get(path)
        if status >= 400 or body is None:
            raise SmsProviderError(
                self.name, f"{path} returned HTTP {status}: {text[:200]}")
        return body

    # --- provider protocol ----------------------------------------------------
    def purchase(self, service: str = SERVICE_INSTAGRAM,
                 country: str = DEFAULT_COUNTRY) -> NumberOrder:
        product = _PRODUCTS.get(service)
        country_slug = _COUNTRIES.get(country)
        if product is None:
            raise SmsProviderError(self.name, f"unmapped service {service!r}")
        if country_slug is None:
            raise SmsProviderError(self.name, f"unmapped country {country!r}")

        status, body, text = self._get(
            f"/user/buy/activation/{country_slug}/{self.operator}/{product}")

        if status >= 400 or body is None:
            lowered = text.lower()
            if any(marker in lowered for marker in _BALANCE_MARKERS):
                raise InsufficientBalance(self.name, text or f"HTTP {status}")
            if any(marker in lowered for marker in _NO_STOCK_MARKERS):
                raise NoNumbersAvailable(self.name, text or f"HTTP {status}")
            # Same reasoning as the SMSPool client: an unrecognised refusal is
            # counted as a pool failure so the fleet keeps moving.
            raise NoNumbersAvailable(self.name, text or f"HTTP {status}")

        order_id = str(body.get("id") or "").strip()
        phone = digits_only(body.get("phone"))
        if not order_id or not phone:
            raise SmsProviderError(
                self.name, f"buy gave no order id / number: {body}")

        country_code, national = _split_number(phone, country)
        return NumberOrder(
            provider=self.name,
            order_id=order_id,
            phone=phone,
            country=country,
            country_code=country_code,
            national_number=national,
            cost=_as_float(body.get("price")),
        )

    def poll_code(self, order: NumberOrder) -> str | None:
        status_code, body, text = self._get(f"/user/check/{order.order_id}")
        if status_code >= 400 or body is None:
            raise SmsProviderError(
                self.name,
                f"check {order.order_id} returned HTTP {status_code}: {text[:200]}")

        for message in body.get("sms") or []:
            if not isinstance(message, dict):
                continue
            code = extract_code(message.get("code")) or extract_code(message.get("text"))
            if code:
                return code

        state = str(body.get("status") or "").upper()
        if state in _DEAD_STATUSES:
            raise SmsProviderError(
                self.name, f"order {order.order_id} is closed (status {state})")
        return None

    def cancel(self, order: NumberOrder) -> bool:
        """Ask 5sim to refund the number now.

        Returns False rather than raising when 5sim refuses -- see the module
        docstring: an order it will not cancel yet still self-refunds on
        `TIMEOUT`, so a refusal is not money lost and must not abort the run.
        """
        try:
            status, body, text = self._get(f"/user/cancel/{order.order_id}")
        except SmsProviderError:
            return False
        if status < 400 and body is not None:
            return str(body.get("status") or "").upper() in (
                STATUS_CANCELED, STATUS_TIMEOUT, STATUS_FINISHED)
        return False

    def finish(self, order: NumberOrder) -> bool:
        """Close an order whose code we have used, so it stops counting active."""
        try:
            status, body, _ = self._get(f"/user/finish/{order.order_id}")
        except SmsProviderError:
            return False
        return status < 400 and body is not None

    def balance(self) -> float:
        body = self._get_json("/user/profile")
        return _as_float(body.get("balance")) or 0.0


# --- helpers ------------------------------------------------------------------
def _split_number(phone: str, country: str):
    """Split an international number into (country code, national part).

    Instagram's phone box takes only the national digits; the country picker
    beside it supplies the prefix. Getting this wrong is silent -- the form
    accepts whatever is typed and simply confirms a different number -- so an
    unmapped country returns (None, None) and the flow types the full
    international number instead. That is correct rather than clever: worse for
    the form, but never wrong.

    German numbers vary in length (national parts run ~10-11 digits), so this
    checks the prefix and leaves the rest alone rather than asserting a total
    length the way the US branch can.
    """
    code = DIALLING_CODES.get(country)
    if not code or not phone.startswith(code):
        return None, None
    national = phone[len(code):]
    if not national:
        return None, None
    if country == COUNTRY_US and len(phone) != 11:
        # A US number is always 1 + 10 digits. Anything else is not the shape
        # this split assumes, so leave it whole.
        return None, None
    return code, national


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
