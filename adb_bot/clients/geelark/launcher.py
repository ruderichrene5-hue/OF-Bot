"""Starting Geelark cloud phones.

Mirrors `MultiloginLauncherClient`: the launch is the flaky call in this whole
chain, so it **never raises**. A refusal comes back as a `BatchOutcome` with the
per-phone reasons attached, because Geelark answers `code: 0, msg: "success"`
even when it started nothing at all.
"""

from __future__ import annotations

from .transport import BatchOutcome, GeelarkError, GeelarkTransport

START_PATH = "/phone/start"
RESTART_PATH = "/phone/restart"


class GeelarkLauncherClient:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def _call(self, path: str, profile_ids: list[str]) -> BatchOutcome:
        try:
            data = self.transport.post(path, {"ids": list(profile_ids)})
        except GeelarkError as error:
            # Shape a transport-level failure like a per-item one so callers
            # only ever handle a BatchOutcome.
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

    def start_profiles(self, profile_ids: list[str]) -> BatchOutcome:
        return self._call(START_PATH, profile_ids)

    def restart_profiles(self, profile_ids: list[str]) -> BatchOutcome:
        return self._call(RESTART_PATH, profile_ids)

    @staticmethod
    def charging_methods(outcome: BatchOutcome) -> dict[str, str]:
        """{phone id: charging method} from a start.

        This is the only place Geelark's API says anything at all about how a
        phone is billed -- there is no balance, quota or usage endpoint. On this
        account it reads `Parallels`, i.e. the phone is drawn from a pool of
        concurrent slots rather than billed as its own always-on device, so what
        costs money is how many phones run *at once*, not how many exist.

        Worth reading on every start: a phone that came back on a different
        charging method than expected is a billing surprise, and nothing else
        would report it.
        """
        return {
            str(detail.get("id")): str(detail.get("chargingMethod") or "")
            for detail in outcome.success_details
            if detail.get("chargingMethod")
        }

    @staticmethod
    def console_urls(outcome: BatchOutcome) -> dict[str, str]:
        """{phone id: phone.geelark.com console URL} from a start.

        The same console the MultiLogin launcher opens -- MLX's mobile profiles
        are Geelark underneath. Handy for looking at a phone by eye, but the URL
        embeds a session token, so it is not something to log or paste around.
        """
        return {
            str(detail.get("id")): str(detail.get("url") or "")
            for detail in outcome.success_details
            if detail.get("url")
        }
