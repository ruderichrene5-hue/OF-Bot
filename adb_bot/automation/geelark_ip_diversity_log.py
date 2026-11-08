"""Logs each real GeeLark proxy port's current exit IP on a timer, so IP
reuse frequency can be measured instead of guessed.

Added 2026-09-07 after a live spot-check showed all 6 then-readable exit IPs
sitting in the same /24 (109.41.112.x) -- a real question ("does the same IP
come back too often, and does the existing rotation cooldown actually stop
that") with no data to answer it. `exit_ip()` is a read-only HTTP GET through
the proxy (see ip_rotation.ProxyRotator.exit_ip) -- it never rotates
anything and never touches a phone or lease, so this is safe to run often
without disturbing live sessions.

Storage is the same append-only JSONL pattern as account_stats.py /
post_ledger.py: a torn write costs one line, not the whole file.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
LOG_FILENAME = "geelark_ip_diversity.jsonl"


def _log_path() -> Path:
    from adb_bot.config.settings import get_app_data_dir
    return get_app_data_dir() / LOG_FILENAME


def check(logger=None) -> int:
    """Record the current exit IP for every real port. Returns how many
    ports were successfully read (out of the configured total)."""
    from adb_bot.clients.geelark.ip_rotation import ProxyRotator
    from adb_bot.clients.geelark.proxies import GeelarkProxyClient
    from adb_bot.clients.geelark.transport import GeelarkTransport
    from adb_bot.clients.geelark.proxy_pool import passive_proxy_ports
    from adb_bot.automation.geelark_lifecycle import load_reboot_config

    transport = GeelarkTransport()
    rotator = ProxyRotator(GeelarkProxyClient(transport).list_proxies())
    all_ports = sorted(set(load_reboot_config().keys()) | passive_proxy_ports())
    ips = rotator.exit_ips()

    now = datetime.now(BERLIN)
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    read_ok = 0
    with path.open("a", encoding="utf-8") as handle:
        for port in all_ports:
            ip = ips.get(port)
            if ip:
                read_ok += 1
            line = json.dumps({"at": now.isoformat(), "port": port, "ip": ip},
                              ensure_ascii=False)
            handle.write(line + "\n")
    if logger:
        logger.info("geelark_ip_diversity_log: recorded %d/%d ports", read_ok, len(all_ports))
    return read_ok


if __name__ == "__main__":
    check()
