"""Catches two Geelark phones running on the same real proxy port at once
and stops the more-recently-started one automatically.

Anything started through our own code is already safe -- session.py's
proxy-port lease blocks a second automated start on a port already in use.
This guard exists for the gap that leaves open: a person starting a phone
by hand in Geelark's own web UI, which has no idea our lease files exist.
Confirmed real incident 2026-08-29: Nikki 2 and Nikki 26 both ran on port
54021 at the same time after being opened by hand.

Polls rather than reacting to an event -- Geelark's API has no push
notification for "phone started" -- so the window between a collision
starting and this catching it is bounded by `POLL_SECONDS`, not instant.
"""

from __future__ import annotations

import time
from collections import defaultdict

from adb_bot.clients.geelark.phones import GeelarkPhoneClient
from adb_bot.clients.geelark.transport import GeelarkTransport

POLL_SECONDS = 15.0

# phone_id -> first tick this process saw it running. Module-level and
# process-local on purpose: a restart of this guard forgets who was
# "first" and just resolves whatever collision it sees next, which is a
# fine trade-off for something this cheap to get wrong.
_first_seen: dict[str, float] = {}


def check_once(transport: GeelarkTransport | None = None, logger=None,
              _now: float | None = None) -> list[dict]:
    """One poll. Stops every phone past the first (by how long this
    process has seen it running) on any port currently holding more than
    one. Returns what it stopped, if anything."""
    transport = transport or GeelarkTransport()
    client = GeelarkPhoneClient(transport)
    phones = client.list_phones()

    running = [p for p in phones if p.get("status") in (0, 1)]
    now = _now if _now is not None else time.monotonic()

    seen_ids: set[str] = set()
    by_port: dict[int, list[dict]] = defaultdict(list)
    for p in running:
        pid = str(p.get("id"))
        seen_ids.add(pid)
        _first_seen.setdefault(pid, now)
        port = (p.get("proxy") or {}).get("port")
        if port:
            by_port[port].append(p)

    # A phone this process no longer sees running (stopped, by us or
    # someone else) must not keep an ownership timestamp -- the next time
    # that id shows up running, it is a genuinely new start.
    for pid in list(_first_seen):
        if pid not in seen_ids:
            del _first_seen[pid]

    stopped = []
    for port, group in by_port.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda p: _first_seen.get(str(p.get("id")), now))
        for p in group[1:]:
            pid = str(p.get("id"))
            name = p.get("serialName")
            try:
                client.transport.post("/phone/stop", {"ids": [pid]})
                stopped.append({"id": pid, "name": name, "port": port})
                if logger:
                    logger.warning(
                        "geelark_collision_guard: stopped %s -- port %s had "
                        "%s phones running at once (likely a manual Geelark "
                        "UI start; our own launches never collide)",
                        name, port, len(group))
            except Exception as exc:
                if logger:
                    logger.warning(
                        "geelark_collision_guard: failed to stop %s (%s)",
                        name, exc)
    return stopped


def run_loop(poll_seconds: float = POLL_SECONDS, logger=None) -> None:
    """Runs forever. Meant for a long-lived systemd service (Restart=always),
    not a periodic timer -- the whole point is catching a collision within
    `poll_seconds`, not once every N minutes."""
    if logger:
        logger.info("geelark_collision_guard: starting, polling every %ss",
                    poll_seconds)
    while True:
        try:
            check_once(logger=logger)
        except Exception as exc:
            if logger:
                logger.warning("geelark_collision_guard: check failed (%s)", exc)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    from adb_bot.core.logger import get_logger

    run_loop(logger=get_logger("adb_bot"))
