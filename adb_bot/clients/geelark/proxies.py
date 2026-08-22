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

    def check_proxy(self, server: str, port: int, username: str = "",
                    password: str = "", proxy_type: str = "socks5",
                    channel: str = "IP-API") -> dict:
        """Test an endpoint and report where it actually comes out.

        This is more useful than its name suggests: it returns
        ``outboundIP`` -- the **real exit address** -- plus country, city,
        timezone and ISP. So Geelark *can* tell you the exit IP; it just does
        not put it on the proxy record. Reading the gateway host off
        `/proxy/list` and calling that the IP is what makes four distinct
        addresses look like one.

        It also answers without routing traffic through the proxy from here,
        which matters when this server cannot reach the endpoint but the phones
        can.
        """
        return self.transport.post(CHECK_PATH, {
            "proxyQueryChannel": channel,
            "proxyType": proxy_type,
            "server": server,
            "port": int(port),
            "username": username,
            "password": password,
        })

    def exit_ips(self) -> dict[int, dict]:
        """{port: {ip, country, city, isp, ok}} for every proxy on the account.

        Uses Geelark's own detection rather than tunnelling from this host, so
        the answer is what *Geelark* sees -- which is what the phones will use.
        """
        out: dict[int, dict] = {}
        for proxy in self.list_proxies():
            port = int(proxy.get("port") or 0)
            if not port:
                continue
            try:
                data = self.check_proxy(
                    str(proxy.get("server")), port,
                    str(proxy.get("username") or ""),
                    str(proxy.get("password") or ""),
                    proxy_type=str(proxy.get("scheme") or "socks5"))
            except Exception as error:  # a dead proxy must not kill the sweep
                out[port] = {"ok": False, "ip": None, "error": str(error)}
                continue
            out[port] = {
                "ok": bool(data.get("detectStatus")),
                "ip": data.get("outboundIP"),
                "country": data.get("countryName"),
                "city": data.get("city"),
                "isp": data.get("isp"),
            }
        return out

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
        mobile IPs. Reading the host as the identity would say "one IP" about
        four. Use `exit_ips()` for the real addresses.
        """
        clusters: dict[str, list[str]] = {}
        for proxy in self.list_proxies():
            key = f"{proxy.get('server')}:{proxy.get('port')}"
            clusters.setdefault(key, []).append(str(proxy.get("id")))
        return clusters
