"""Stopping Geelark cloud phones.

Stopping is what actually saves money here: Geelark bills a running phone by the
minute (`chargeMode: 0` on this account), so a phone left up after a run is pure
spend. Like the launcher this never raises -- a shutdown that fails silently is
worse than one that reports.
"""

from __future__ import annotations

from .transport import BatchOutcome, GeelarkError, GeelarkTransport

STOP_PATH = "/phone/stop"


class GeelarkShutdownClient:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def shutdown_profiles(self, profile_ids: list[str]) -> BatchOutcome:
        try:
            data = self.transport.post(STOP_PATH, {"ids": list(profile_ids)})
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
        return BatchOutcome(data)
