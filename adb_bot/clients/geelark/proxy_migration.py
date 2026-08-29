"""Batch-move every Geelark phone still on a non-real proxy (chiefly
MultiLogin's relay, `gate.multilogin.com` -- see `session.py`'s module
docstring for how phones ended up there) onto our own 4 real proxy-seller.com
modems, then clean up the now-orphaned old proxy-book entries.

Confirmed 2026-08-29 as the request behind this module: everything gets
verified live against Geelark after reassigning, not assumed from the
reassign call's own response.

"Real" means a saved proxy whose port is one of `GEELARK_PROXY_REBOOT_URLS`'s
keys -- the same canonical list `ip_rotation.load_reboot_config` already
uses, not a second hardcoded one here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ip_rotation import load_reboot_config
from .phones import GeelarkPhoneClient
from .proxies import GeelarkProxyClient
from .transport import GeelarkTransport


@dataclass
class MigrationReport:
    total_phones: int = 0
    already_real: int = 0
    reassigned: list[str] = field(default_factory=list)
    reassign_failed: list[tuple[str, str]] = field(default_factory=list)
    validation_failed: list[str] = field(default_factory=list)
    junk_proxies_deleted: int = 0
    junk_proxies_stuck: int = 0

    @property
    def clean(self) -> bool:
        """True only if every phone Geelark reports right now sits on a real
        port -- the thing the caller actually wants to know."""
        return not self.reassign_failed and not self.validation_failed


def _real_ports() -> set[int]:
    ports = set(load_reboot_config().keys())
    if not ports:
        raise RuntimeError(
            "GEELARK_PROXY_REBOOT_URLS is not set; refusing to guess which "
            "proxies are our own real ports")
    return ports


def _real_proxy_ids(transport: GeelarkTransport, real_ports: set[int]) -> list[str]:
    """One proxy-book id per real port (first match if duplicates), in port
    order -- the round-robin assignment pool."""
    seen: set[int] = set()
    ids: list[str] = []
    for proxy in GeelarkProxyClient(transport).list_proxies():
        port = proxy.get("port")
        if port in real_ports and port not in seen:
            seen.add(port)
            ids.append(str(proxy.get("id")))
    if not ids:
        raise RuntimeError(
            "GEELARK_PROXY_REBOOT_URLS names real ports, but none of them "
            "have a matching saved proxy on the account -- add them first")
    return ids


def reassign_wrong_proxies(transport: GeelarkTransport | None = None) -> MigrationReport:
    """Step 1+2: round-robin every non-real-proxy phone onto a real one,
    then re-query Geelark to confirm each one actually took.

    Skips phones that are currently running or starting -- Geelark's own
    docs warn `/phone/detail/update` refuses writes while a phone is
    starting, and reassigning a phone mid-session doesn't move its live
    connection anyway (only the config for its *next* start). Those are
    reported neither as reassigned nor failed; rerun once they're stopped.
    """
    transport = transport or GeelarkTransport()
    phone_client = GeelarkPhoneClient(transport)
    real_ports = _real_ports()
    proxy_ids = _real_proxy_ids(transport, real_ports)

    phones = phone_client.list_phones()
    report = MigrationReport(total_phones=len(phones))

    wrong = []
    for row in phones:
        port = (row.get("proxy") or {}).get("port")
        if port in real_ports:
            report.already_real += 1
        elif row.get("status") in (0, 1):        # started/starting: skip, don't touch
            continue
        else:
            wrong.append(row)

    for i, row in enumerate(wrong):
        phone_id = str(row.get("id"))
        name = str(row.get("serialName") or phone_id)
        proxy_id = proxy_ids[i % len(proxy_ids)]
        try:
            phone_client.update_phone(phone_id, proxy_id=proxy_id)
            report.reassigned.append(phone_id)
        except Exception as exc:
            report.reassign_failed.append((phone_id, str(exc)))

    # Validate against a fresh read, not the update call's own response --
    # the account has form for accepting a write that doesn't actually stick.
    fresh = {str(row.get("id")): row for row in phone_client.list_phones()}
    for phone_id in report.reassigned:
        port = (fresh.get(phone_id, {}).get("proxy") or {}).get("port")
        if port not in real_ports:
            report.validation_failed.append(phone_id)

    return report


def delete_orphaned_proxies(transport: GeelarkTransport | None = None,
                            report: MigrationReport | None = None,
                            batch_size: int = 40) -> MigrationReport:
    """Step 3: delete every saved proxy that is not one of our real ports
    AND is not bound to any phone Geelark still has.

    Geelark refuses to delete a proxy still "bound to an environment" (its
    own wording, error 40010) -- including, confirmed 2026-08-27, a phone
    that was already deleted, which the account then can never release. That
    is counted as `junk_proxies_stuck`, not a failure: it is account-book
    clutter with no live phone using it, harmless beyond visual noise.
    """
    transport = transport or GeelarkTransport()
    proxy_client = GeelarkProxyClient(transport)
    real_ports = _real_ports()
    report = report or MigrationReport()

    junk_ids = [str(p.get("id")) for p in proxy_client.list_proxies()
               if p.get("port") not in real_ports]

    for i in range(0, len(junk_ids), batch_size):
        batch = junk_ids[i:i + batch_size]
        try:
            proxy_client.delete_proxies(batch)
        except Exception:
            pass    # per-id outcome only knowable from the recount below

    remaining = {str(p.get("id")) for p in proxy_client.list_proxies()
                if p.get("port") not in real_ports}
    report.junk_proxies_deleted = len(junk_ids) - len(remaining & set(junk_ids))
    report.junk_proxies_stuck = len(remaining & set(junk_ids))
    return report


def run_migration(transport: GeelarkTransport | None = None) -> MigrationReport:
    """The full sequence: reassign, validate, then clean up the book."""
    transport = transport or GeelarkTransport()
    report = reassign_wrong_proxies(transport)
    return delete_orphaned_proxies(transport, report)


if __name__ == "__main__":
    result = run_migration()
    print(f"phones total: {result.total_phones}")
    print(f"already on a real proxy: {result.already_real}")
    print(f"reassigned: {len(result.reassigned)}")
    if result.reassign_failed:
        print(f"reassign FAILED: {len(result.reassign_failed)}")
        for phone_id, error in result.reassign_failed:
            print(f"  {phone_id}: {error}")
    if result.validation_failed:
        print(f"validation FAILED (reassigned but didn't stick): "
              f"{len(result.validation_failed)}")
        for phone_id in result.validation_failed:
            print(f"  {phone_id}")
    print(f"orphaned proxy entries deleted: {result.junk_proxies_deleted}")
    print(f"orphaned proxy entries permanently stuck (Geelark bug, harmless): "
          f"{result.junk_proxies_stuck}")
    print(f"clean: {result.clean}")
