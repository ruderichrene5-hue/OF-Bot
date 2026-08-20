"""Switching a Geelark phone's ADB bridge on and off.

Two things about this endpoint bite:

* ADB is **off per phone by default**, and stays off until asked. A phone that
  is running perfectly still answers `/adb/getData` with
  `code: 49001, "ADB did not opened"`.
* Enabling is **asynchronous**, and the phone must already be started. Reading
  the port back immediately returns nothing useful; Geelark's own guidance is to
  wait a few seconds, which is what `enable_and_wait` does rather than leaving
  every caller to rediscover it.
"""

from __future__ import annotations

import time

from .transport import BatchOutcome, GeelarkError, GeelarkTransport

SET_STATUS_PATH = "/adb/setStatus"

# Geelark enables the bridge asynchronously; this is the settle time before the
# port and password are readable.
ENABLE_SETTLE_SECONDS = 4


class GeelarkAdbEnableClient:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def enable_adb(self, profile_ids: list[str], enabled: bool = True) -> BatchOutcome:
        try:
            data = self.transport.post(
                SET_STATUS_PATH,
                {"ids": list(profile_ids), "open": bool(enabled)},
            )
        except GeelarkError as error:
            return BatchOutcome({
                "totalAmount": len(profile_ids),
                "successAmount": 0,
                "failAmount": len(profile_ids),
                "failDetails": [
                    {"id": profile_id, "code": error.code, "msg": error.message}
                    for profile_id in profile_ids
                ],
            })

        # Unlike `/phone/start` and friends, this endpoint answers a bare
        # `code: 0` with **no data block at all** -- no totals, no per-id
        # details. Handing that straight to BatchOutcome reads as "0 of 0
        # succeeded", i.e. a failure, and the caller gives up on a phone whose
        # ADB was in fact just switched on. `transport.post` has already raised
        # for any non-zero code, so reaching here with an empty body means every
        # requested id was accepted.
        if not any(key in data for key in
                   ("totalAmount", "successAmount", "failAmount", "failDetails")):
            return BatchOutcome({
                "totalAmount": len(profile_ids),
                "successAmount": len(profile_ids),
                "failAmount": 0,
                "successDetails": [{"id": profile_id} for profile_id in profile_ids],
            })
        return BatchOutcome(data)

    def enable_and_wait(self, profile_ids: list[str],
                        settle_seconds: int = ENABLE_SETTLE_SECONDS) -> BatchOutcome:
        """Enable ADB, then pause long enough for the port to be readable."""
        outcome = self.enable_adb(profile_ids, enabled=True)
        if outcome.succeeded:
            time.sleep(settle_seconds)
        return outcome

    def disable_adb(self, profile_ids: list[str]) -> BatchOutcome:
        return self.enable_adb(profile_ids, enabled=False)
