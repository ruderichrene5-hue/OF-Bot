"""Signed transport for the Geelark OpenAPI.

Every other client in this package is a thin wrapper around `GeelarkTransport`.
It exists because Geelark is the first integration here that *signs* requests
rather than carrying a bearer token, and because two of its habits will silently
produce wrong answers if each caller has to remember them:

* **HTTP 200 means nothing.** Failures come back 200 with a non-zero `code` in
  the body. `request()` raises `GeelarkError` so no caller can read a failure as
  a success.
* **Batch endpoints report success while failing.** `/phone/start`, `/phone/stop`
  and `/phone/delete` return envelope ``code: 0, msg: "success"`` even when every
  item failed; the truth is in `successAmount` / `failDetails`. `BatchOutcome`
  makes that impossible to miss -- this fleet has lost posts to exactly this
  shape of silent no-op before.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import requests

from adb_bot.config import settings

GEELARK_API_URL = "https://openapi.geelark.com/open/v1"

# Body codes seen from the live API.
CODE_OK = 0
CODE_BAD_ARGUMENT = 40004
CODE_ENV_NOT_FOUND = 42001
CODE_PHONE_NOT_RUNNING = 42002
CODE_ADB_NOT_OPEN = 49001

# A `pageSize` above this silently returns `data: null` instead of an error.
MAX_PAGE_SIZE = 100


class GeelarkError(RuntimeError):
    """A Geelark call that came back with a non-zero `code`."""

    def __init__(self, path: str, code: Any, message: str) -> None:
        super().__init__(f"{path} failed: code={code} msg={message}")
        self.path = path
        self.code = code
        self.message = message


class GeelarkTransport:
    """Signs and posts to the Geelark OpenAPI.

    ``sign = SHA256(appId + traceId + ts + nonce + apiKey)`` upper-cased, where
    ``ts`` is epoch **milliseconds**. There is no session, login or bearer token:
    the app id and API key are the entire credential.
    """

    def __init__(
        self,
        app_id: str | None = None,
        api_key: str | None = None,
        api_url: str = GEELARK_API_URL,
        timeout: int = 30,
    ) -> None:
        self.app_id = (app_id if app_id is not None
                       else settings.get_saved_geelark_app_id()).strip()
        self.api_key = (api_key if api_key is not None
                        else settings.get_saved_geelark_api_key()).strip()
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.app_id and self.api_key)

    def _headers(self) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        trace_id = uuid.uuid4().hex
        nonce = trace_id[:6]
        raw = f"{self.app_id}{trace_id}{timestamp}{nonce}{self.api_key}"
        return {
            "Content-Type": "application/json",
            "appId": self.app_id,
            "traceId": trace_id,
            "ts": timestamp,
            "nonce": nonce,
            "sign": hashlib.sha256(raw.encode()).hexdigest().upper(),
        }

    def post(self, path: str, payload: dict | None = None) -> dict:
        """POST to `path` and return the body's `data` block.

        Raises `GeelarkError` on a non-zero `code`.
        """
        if not self.is_configured:
            raise GeelarkError(path, "unconfigured",
                               "GEELARK_APP_ID / GEELARK_API_KEY are not set")

        url = f"{self.api_url}{path}"
        print(f"[HTTP] POST {url}")
        print(f"[HTTP] Payload: {payload or {}}")
        response = requests.post(url, json=payload or {},
                                 headers=self._headers(), timeout=self.timeout)
        print(f"[HTTP] Status: {response.status_code}")
        print(f"[HTTP] Response: {response.text}")
        response.raise_for_status()
        body = response.json()

        code = body.get("code")
        if code != CODE_OK:
            raise GeelarkError(path, code, body.get("msg") or "")
        return body.get("data") or {}

    def paged(self, path: str, page_size: int = MAX_PAGE_SIZE,
              extra: dict | None = None) -> list[dict]:
        """Walk every page of a list endpoint and return the rows.

        Geelark is inconsistent about which key holds the rows -- `items` on
        phones and apps, `list` on proxies, tags and groups -- so both are read.
        """
        page_size = min(page_size, MAX_PAGE_SIZE)
        page = 1
        rows: list[dict] = []

        while True:
            payload = {"page": page, "pageSize": page_size}
            payload.update(extra or {})
            data = self.post(path, payload)

            batch = data.get("items")
            if batch is None:
                batch = data.get("list") or []

            rows.extend(batch)
            total = data.get("total")
            if not batch or total is None or len(rows) >= total:
                return rows
            page += 1


class BatchOutcome:
    """The real result of a Geelark batch call.

    Read `ok` or `succeeded`, never the envelope -- see the module docstring.
    """

    def __init__(self, data: dict) -> None:
        self.total: int = data.get("totalAmount") or 0
        self.succeeded: int = data.get("successAmount") or 0
        self.failed: int = data.get("failAmount") or 0
        self.success_details: list[dict] = data.get("successDetails") or []
        self.failure_details: list[dict] = data.get("failDetails") or []

    @property
    def ok(self) -> bool:
        """True only when something succeeded and nothing failed."""
        return self.succeeded > 0 and self.failed == 0

    def failures(self) -> dict[str, str]:
        """{phone id: reason} for every item the API refused."""
        return {
            str(item.get("id")): str(item.get("msg") or item.get("code") or "")
            for item in self.failure_details
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"BatchOutcome(total={self.total}, succeeded={self.succeeded}, "
                f"failed={self.failed})")
