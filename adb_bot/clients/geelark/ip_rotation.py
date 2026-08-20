"""Forcing a new exit IP on the mobile proxies behind the Geelark phones.

Geelark itself has **no rotate-now endpoint**. What it has is a `refreshUrl`
field you can attach to a phone at creation time; the rotation is performed by
the *proxy vendor*, not by Geelark. On this account the vendor is proxy-seller
and each of the four endpoints has its own reboot URL carrying a private token,
so rotating is a plain GET to that URL.

Two things make this worth a module rather than a curl:

* **The tokens are credentials.** One reboot URL is a permanent, unauthenticated
  right to reset that proxy. They are read from the environment, never from
  source -- `tests/test_no_hardcoded_secrets.py` scans everything git would
  commit.
* **Rotation is not instant and not guaranteed.** The vendor answers immediately
  but the new address appears seconds later, and a rotation can return the same
  IP. Firing the URL and assuming a fresh IP is how a phone ends up posting from
  the address it was supposed to leave behind, so `rotate_and_verify` checks.

Exit IPs are verified through the endpoint's **HTTP** port rather than its
SOCKS5 one, because `requests` speaks HTTP proxies out of the box and SOCKS
needs PySocks, which this venv does not have. On this vendor both ports are the
same tunnel: 44015 and 54015 were observed leaving from the same address.

`GEELARK_PROXY_REBOOT_URLS` is a JSON object keyed by the SOCKS5 port::

    {"54015": "https://proxy-seller.com/api/proxy/reboot?token=...",
     "54018": {"reboot": "https://...", "http_port": 44018}}

A bare string is the reboot URL and the HTTP port is derived by swapping the
leading `5` for a `4` -- this vendor's convention, verified on all four here.
Pass the object form for any endpoint that does not follow it.
"""

from __future__ import annotations

import json
import os
import time

import requests

IP_ECHO_URL = "https://api.ipify.org"
REBOOT_ENV = "GEELARK_PROXY_REBOOT_URLS"

# The vendor answers the reboot call at once; the new address takes a few
# seconds to appear.
ROTATE_SETTLE_SECONDS = 5
ROTATE_TIMEOUT_SECONDS = 90
ROTATE_POLL_SECONDS = 5


class ProxyRotationError(RuntimeError):
    pass


def _derive_http_port(socks_port: int) -> int:
    """44015 from 54015 -- proxy-seller's pairing, not a general rule."""
    text = str(socks_port)
    if text.startswith("5"):
        return int("4" + text[1:])
    return socks_port


def load_reboot_config(raw: str | None = None) -> dict[int, dict]:
    """{socks5 port: {"reboot": url, "http_port": int}} from the environment."""
    raw = raw if raw is not None else os.getenv(REBOOT_ENV, "")
    if not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProxyRotationError(f"{REBOOT_ENV} is not valid JSON: {exc}") from exc

    config: dict[int, dict] = {}
    for key, value in parsed.items():
        port = int(key)
        if isinstance(value, str):
            config[port] = {"reboot": value, "http_port": _derive_http_port(port)}
        else:
            config[port] = {
                "reboot": value["reboot"],
                "http_port": int(value.get("http_port") or _derive_http_port(port)),
            }
    return config


class ProxyRotator:
    """Rotate and verify the exit IP of the proxies Geelark's phones sit behind.

    Built from Geelark's own `/proxy/list` -- which returns server, port,
    username and password -- plus the reboot URLs from the environment, so the
    only thing not already on the account is the token.
    """

    def __init__(self, proxies: list[dict], reboot_config: dict[int, dict] | None = None,
                 timeout: int = 25) -> None:
        self.timeout = timeout
        self.reboot_config = (reboot_config if reboot_config is not None
                              else load_reboot_config())
        self.proxies_by_port = {int(p["port"]): p for p in proxies if p.get("port")}

    def rotatable_ports(self) -> list[int]:
        """Ports that exist on the account *and* have a reboot URL configured."""
        return sorted(set(self.proxies_by_port) & set(self.reboot_config))

    def _requests_proxy(self, port: int) -> dict[str, str]:
        proxy = self.proxies_by_port.get(port)
        if proxy is None:
            raise ProxyRotationError(f"no proxy on this account with port {port}")
        http_port = self.reboot_config.get(port, {}).get(
            "http_port", _derive_http_port(port))
        auth = ""
        if proxy.get("username"):
            auth = f"{proxy['username']}:{proxy.get('password', '')}@"
        url = f"http://{auth}{proxy['server']}:{http_port}"
        return {"http": url, "https": url}

    def exit_ip(self, port: int) -> str | None:
        """The address the world sees for this endpoint, or None if unreachable.

        Never raises: an endpoint that is down mid-rotation is expected, and a
        caller polling for a change should see None rather than an exception.
        """
        try:
            response = requests.get(IP_ECHO_URL, proxies=self._requests_proxy(port),
                                    timeout=self.timeout)
            response.raise_for_status()
            return response.text.strip()
        except Exception:
            return None

    def exit_ips(self) -> dict[int, str | None]:
        """Every configured endpoint's exit address.

        This is the reading Geelark cannot give you: its `/proxy/list` reports
        the gateway host, which is identical across all four here while the exit
        addresses are entirely different.
        """
        return {port: self.exit_ip(port) for port in sorted(self.proxies_by_port)}

    def rotate(self, port: int) -> bool:
        """Fire the vendor's reboot URL. Says nothing about whether the IP moved."""
        entry = self.reboot_config.get(port)
        if not entry:
            raise ProxyRotationError(
                f"no reboot URL configured for port {port}; set {REBOOT_ENV}")
        response = requests.get(entry["reboot"], timeout=self.timeout)
        response.raise_for_status()
        return True

    def rotate_and_verify(self, port: int,
                          timeout_seconds: int = ROTATE_TIMEOUT_SECONDS) -> dict:
        """Rotate, then wait for the address to actually change.

        Returns `{"before", "after", "changed", "seconds"}`. `changed` False is
        a real outcome, not an error: a mobile proxy can hand back the address
        it just released, and a caller that assumed otherwise would carry on
        believing the phone had a clean IP.
        """
        before = self.exit_ip(port)
        started = time.time()
        self.rotate(port)
        time.sleep(ROTATE_SETTLE_SECONDS)

        deadline = started + timeout_seconds
        after = before
        while time.time() < deadline:
            current = self.exit_ip(port)
            if current and current != before:
                after = current
                break
            time.sleep(ROTATE_POLL_SECONDS)
        else:
            after = self.exit_ip(port)

        return {
            "port": port,
            "before": before,
            "after": after,
            "changed": bool(after and after != before),
            "seconds": round(time.time() - started, 1),
        }
