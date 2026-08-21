"""Forcing a new exit IP on the mobile proxies behind the Geelark phones.

Geelark itself has **no rotate-now endpoint**. What it has is a `refreshUrl`
field you can attach to a phone at creation time; the rotation is performed by
the *proxy vendor*, not by Geelark. So rotation is a plain GET to a vendor URL,
made from here -- Geelark is not needed for it at all.

**The working format puts the token in the path, and needs the trailing
slash**::

    https://proxy-seller.com/modem/reboot/<token>/

Confirmed end to end on 2026-08-20: port 54015 had been sitting on
``94.219.47.10`` for 49 minutes across 33 consecutive samples; the call was made
at 16:46:58 and the address was ``109.41.112.239`` by 16:47:23 -- **about 25
seconds**. Without the trailing slash the URL 301s, so follow redirects or send
it with the slash.

**The earlier `/api/proxy/reboot?token=...` links were simply the wrong URL**,
and diagnosing them wasted a day, so the tell is written down: that path
answered **HTTP 400 for every request shape** -- any method, any HTTP version,
real token, garbage token or none -- and the response carried **no ``SRVID``
cookie**, while ``/api/proxy/list`` on the same host returned a normal 401
*with* ``SRVID``. ``SRVID`` is the load balancer's backend-affinity cookie, so
its absence proves the request never reached a backend at all. **A 400 with no
SRVID means the URL is wrong; it says nothing about the token**, because nothing
ever reached the code that validates tokens.

**Two different 400s, and only one of them is fatal.** The working endpoint also
answers 400 -- but with ``Content-Type: text/plain`` and the body ``ERROR`` --
when called again too soon. That is a **cooldown**, not a broken URL: a second
rotation about a minute after the first was refused this way. So judge these
responses by body and content type, never by status code alone, which is exactly
what `probe_link` does.

`probe_link` exists for precisely this: given a candidate URL, say whether it is
a live link for a real modem *before* anything depends on it.

The API alternative, once an account has a Mobile CRM key (header is
``Authorization: <key>`` with **no** ``Bearer`` prefix)::

    PATCH /api/v1/modems/{id}/change-ip
    PATCH /api/v1/modems/{id}/reboot
    POST  /api/v1/modems/{id}/rotation?rotation=5   # minutes; a standing timer

That last one may remove the need for on-demand rotation entirely.

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

**The exit IP cannot be changed from the phone.** This was tested rather than
assumed, because "just toggle airplane mode over ADB" is the obvious idea and it
does not work here:

* The phone carries **no device-level proxy** -- ``settings get global
  http_proxy`` is null. Geelark applies the proxy *outside* the Android
  container, so the phone cannot see it, let alone rotate it.
* The phone's default route is the container's own ``wlan0`` (a 10.x address).
  Its radios are virtual; cycling them cycles the container's interfaces, not
  the proxy's upstream mobile modem.
* Measured: with the phone egressing as ``94.219.47.10``, ``svc data
  disable/enable`` and a full airplane-mode off/on cycle both left the exit IP
  **unchanged**. ADB survived both.
* Calling the vendor's rotation URL *from the phone* -- so the request leaves
  through the proxy's own address, in case the vendor whitelists it -- returned
  the same HTTP 400 as calling it from the server.

Rotation is therefore a vendor-side operation reachable only over the vendor's
own API, from anywhere. What the phone is good for is *verifying* the result:
``adb shell curl -s https://api.ipify.org`` is the ground truth for what
Instagram actually sees, and it matched the proxy's exit exactly.

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

    @staticmethod
    def probe_link(url: str, timeout: int = 25) -> dict:
        """Classify what a rotation URL answered.

        .. warning::
           **This is not a dry run.** This vendor exposes no read-only status
           endpoint, so the only way to learn whether a link works is to *use*
           it -- fetching a rotation URL rotates the IP. An earlier version of
           the CLI advertised this as "changes nothing" and rotated all four
           production proxies in one go.

        Classifying by status code alone is not enough, because 400 means two
        opposite things here:

        * **400 with an HTML body and no ``SRVID`` cookie** -- the load balancer
          rejected the URL before reaching a backend. The URL is wrong, and this
          says nothing about the token.
        * **400 with ``Content-Type: text/plain`` and the body ``ERROR``** -- the
          real endpoint refusing because it was called again too soon. A
          cooldown, i.e. "not yet", on a URL that works perfectly well.

        A success is **200** with body ``OK``.
        """
        try:
            response = requests.get(url, timeout=timeout)
        except Exception as error:
            return {"ok": False, "status": None, "detail": str(error)}

        body = (response.text or "").strip()
        looks_like_error = body.upper().startswith("ERROR")
        return {
            "ok": response.status_code == 200 and not looks_like_error,
            "status": response.status_code,
            "detail": body[:200] or "(empty body)",
        }

    def probe_all(self) -> dict[int, dict]:
        """Probe every configured rotation URL."""
        return {port: self.probe_link(entry["reboot"])
                for port, entry in sorted(self.reboot_config.items())}

    def rotate(self, port: int) -> dict:
        """Fire the vendor's rotation URL.

        Returns `{"accepted", "status", "detail"}`. Says nothing about whether
        the address actually moved -- `rotate_and_verify` is what checks that.

        Deliberately does not raise on a refusal, because the common one is a
        **cooldown**: called again too soon, the vendor answers HTTP 400 with the
        plain-text body ``ERROR``. That is "not yet", not "broken", and a caller
        that treats it as an exception will retry itself into a loop.
        """
        entry = self.reboot_config.get(port)
        if not entry:
            raise ProxyRotationError(
                f"no reboot URL configured for port {port}; set {REBOOT_ENV}")

        # allow_redirects: the URL 301s to a trailing-slash form.
        response = requests.get(entry["reboot"], timeout=self.timeout,
                                allow_redirects=True)
        body = (response.text or "").strip()
        accepted = response.status_code == 200 and not body.upper().startswith("ERROR")
        return {
            "accepted": accepted,
            "status": response.status_code,
            "detail": body[:200] or "(empty body)",
        }

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
        fired = self.rotate(port)
        if not fired["accepted"]:
            return {
                "port": port, "before": before, "after": before,
                "changed": False, "seconds": round(time.time() - started, 1),
                "accepted": False, "detail": fired["detail"],
            }
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
            "accepted": True,
            "detail": fired["detail"],
        }
