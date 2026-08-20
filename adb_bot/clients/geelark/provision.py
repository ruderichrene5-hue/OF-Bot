"""Stand up the fleet's model folders and phones on Geelark.

Mirrors the MultiLogin fleet onto Geelark: one Geelark **group** per model, one
phone per profile, named the same so the two sides can be read side by side.
Each phone carries a **remark** that says either what logs the account in, or
which MultiLogin profile to go and look at by hand.

Two Geelark behaviours make this cheap, both verified rather than assumed:

* `profileGroup` on `/phone/addNew` **auto-creates the group** -- there is no
  need to create folders first and thread ids through.
* `profileNote` on the same call becomes the phone's `remark`, readable straight
  back from `/phone/list`.

**Creating a phone costs nothing** beyond the plan's profile allowance; only
running one bills. But the allowance is finite, so `plan_rows` refuses to build
a plan larger than what is free.

The remark is written in a fixed, greppable shape so it can be parsed back
later rather than only read by eye::

    IG:@handle | MAIL:address | PW:password
    NOCREDS | MLX:Nikki 12 | ID:6285... | IG:@nikki_lat

⚠️ This puts account passwords in a third-party system's metadata field, where
anyone with Geelark access can read them. That is a deliberate trade for making
the migration checkable by hand; it is not a secret store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .phones import GeelarkPhoneClient
from .proxies import GeelarkProxyClient
from .transport import GeelarkError, GeelarkTransport

# Geelark accepts at most 100 rows per create call.
MAX_PER_CALL = 100
# The vendor is rate limited per endpoint; a short pause between batches keeps a
# large build well clear of it.
BATCH_PAUSE_SECONDS = 2


@dataclass
class ProfileRow:
    """One phone to create."""

    name: str
    model: str
    mlx_id: str = ""
    handle: str = ""
    email: str = ""
    password: str = ""

    @property
    def has_credentials(self) -> bool:
        return bool(self.password)

    def remark(self) -> str:
        """What a person should see when they open this phone in Geelark."""
        if self.has_credentials:
            parts = []
            if self.handle:
                parts.append(f"IG:@{self.handle}")
            if self.email:
                parts.append(f"MAIL:{self.email}")
            parts.append(f"PW:{self.password}")
            return " | ".join(parts)

        parts = ["NOCREDS"]
        if self.name:
            parts.append(f"MLX:{self.name}")
        if self.mlx_id:
            parts.append(f"ID:{self.mlx_id}")
        if self.handle:
            parts.append(f"IG:@{self.handle}")
        return " | ".join(parts)


@dataclass
class ProvisionResult:
    created: list = field(default_factory=list)   # (name, geelark id)
    failed: list = field(default_factory=list)    # (name, code, msg)
    skipped: list = field(default_factory=list)   # names already on Geelark

    @property
    def ok(self) -> bool:
        return bool(self.created) and not self.failed


class GeelarkProvisioner:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()
        self.phones = GeelarkPhoneClient(self.transport)
        self.proxies = GeelarkProxyClient(self.transport)

    def existing_names(self) -> set[str]:
        """Phone names already on the account, so a rerun does not duplicate.

        Geelark does not enforce unique names, so nothing stops a second run
        creating a whole parallel fleet. This is what stops it.
        """
        return {str(row.get("serialName") or "").strip()
                for row in self.phones.list_phones()}

    def proxy_serials(self) -> list[int]:
        return sorted(int(p["serialNo"]) for p in self.proxies.list_proxies()
                      if p.get("serialNo") is not None)

    def plan_rows(self, rows: list[ProfileRow], free_slots: int) -> list[ProfileRow]:
        """Drop rows that already exist, and refuse to exceed the allowance."""
        existing = self.existing_names()
        fresh = [r for r in rows if r.name not in existing]
        if len(fresh) > free_slots:
            raise GeelarkError(
                "/phone/addNew", "plan",
                f"{len(fresh)} phones to create but only {free_slots} profile "
                f"slots free on the plan")
        return fresh

    def create(self, rows: list[ProfileRow], mobile_type: str = "Android 13",
               proxy_serials: list[int] | None = None,
               logger=None) -> ProvisionResult:
        """Create every row, batching and spreading proxies round-robin.

        Reads `details` per row rather than the envelope: `/phone/addNew`
        reports `code: 0` overall while individual rows fail, and a row that
        fails for want of a proxy looks exactly like success from the outside.
        """
        result = ProvisionResult()
        if not rows:
            return result

        serials = proxy_serials or self.proxy_serials()
        if not serials:
            raise GeelarkError("/phone/addNew", "plan",
                               "no proxies on the account; creation requires one")

        for start in range(0, len(rows), MAX_PER_CALL):
            batch = rows[start:start + MAX_PER_CALL]
            payload = {
                "mobileType": mobile_type,
                "chargeMode": 0,
                "amount": len(batch),
                "data": [{
                    "profileName": row.name,
                    "proxyNumber": serials[(start + offset) % len(serials)],
                    "profileGroup": row.model,
                    "profileNote": row.remark(),
                } for offset, row in enumerate(batch)],
            }
            data = self.transport.post("/phone/addNew", payload)

            for detail in data.get("details") or []:
                index = int(detail.get("index", 0))
                row = batch[index] if index < len(batch) else None
                name = (detail.get("profileName") or (row.name if row else "?"))
                if detail.get("code"):
                    result.failed.append((name, detail.get("code"),
                                          detail.get("msg")))
                else:
                    result.created.append((name, str(detail.get("id"))))

            if logger is not None:
                logger.info("geelark provision: %s created, %s failed so far",
                            len(result.created), len(result.failed))
            if start + MAX_PER_CALL < len(rows):
                time.sleep(BATCH_PAUSE_SECONDS)

        return result
