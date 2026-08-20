"""Geelark's proxy book.

This is the piece the repo never had for MultiLogin -- there is no MLX proxy
client here at all, only an Airtable row. Geelark exposes real CRUD plus
`/proxy/check`, which validates an endpoint **server-side before it is saved**,
so a dead proxy can be caught without spending a phone launch to discover it.
"""

from __future__ import annotations

from .transport import GeelarkTransport

LIST_PATH = "/proxy/list"
ADD_PATH = "/proxy/add"
UPDATE_PATH = "/proxy/update"
DELETE_PATH = "/proxy/delete"
CHECK_PATH = "/proxy/check"


class GeelarkProxyClient:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def list_proxies(self) -> list[dict]:
        return self.transport.paged(LIST_PATH)

    def check_proxy(self, scheme: str, server: str, port: int,
                    username: str = "", password: str = "") -> dict:
        """Ask Geelark to test an endpoint before it is bound to anything."""
        return self.transport.post(CHECK_PATH, {
            "scheme": scheme,
            "server": server,
            "port": int(port),
            "username": username,
            "password": password,
        })

    def add_proxies(self, proxies: list[dict]) -> dict:
        """Add proxies. Each entry needs scheme/server/port (+ credentials)."""
        if not proxies:
            raise ValueError("refusing to call proxy add with an empty list")
        return self.transport.post(ADD_PATH, {"list": proxies})

    def delete_proxies(self, proxy_ids: list[str]) -> dict:
        if not proxy_ids:
            raise ValueError("refusing to call proxy delete with an empty id list")
        return self.transport.post(DELETE_PATH, {"ids": list(proxy_ids)})

    def endpoint_clusters(self) -> dict[str, list[str]]:
        """Group proxy ids by `server:port`.

        Shared endpoints across models are a standing risk on the MultiLogin
        fleet, and a handful of endpoints for a whole fleet would be far denser
        sharing than today, so the density is worth seeing before it is designed
        in.

        **This is not the exit IP.** Several ports on one proxy host routinely
        egress from completely different addresses -- on this account all four
        ports share the host `162.55.84.35` but leave from four different German
        mobile IPs. Nothing in Geelark's API reports the exit address, so the
        only way to know it is to make a request through the proxy and ask.
        Reading the host as the identity would say "one IP" about four.
        """
        clusters: dict[str, list[str]] = {}
        for proxy in self.list_proxies():
            key = f"{proxy.get('server')}:{proxy.get('port')}"
            clusters.setdefault(key, []).append(str(proxy.get("id")))
        return clusters
