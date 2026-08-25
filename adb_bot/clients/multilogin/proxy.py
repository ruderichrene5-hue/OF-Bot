"""Set a MultiLogin profile's proxy, and pre-flight-check one before that.

The repo only ever *read* `proxy` off `mobile_profiles/phone/list`
(`mlx_sync.py`) -- there was no write path at all. Two endpoints from the
MultiLogin API guide's Mobile Profile Management section:

* ``POST /mobile_profiles/proxy/check`` -- tests an endpoint from MLX's own
  network before anything is saved, and reports where it actually comes out
  (city/ISP/timezone), the same way Geelark's own `/proxy/check` does.
* ``PUT /mobile_profiles/phone/update`` -- writes `proxy_config` onto one
  profile. MLX validates the proxy server-side before saving: an endpoint it
  cannot reach comes back ``500 INTERNAL_SERVER_ERROR / "check proxy
  failed"`` and nothing is written, so a failed call is evidence about the
  *proxy*, not the payload.

Unlike Geelark, MLX has no registered "proxy book" a profile can point at by
id -- every profile carries its own full server/port/credentials, set here
directly.
"""

from __future__ import annotations

import requests

# type_id: 1=SOCKS5, 2=HTTP, 3=HTTPS (20-23 are IPIDEA/IPHTML/Kookeey/Lumatuo).
# protocol: 1=SOCKS5, 2=HTTP. The two are asked for separately by the API but
# always move together for the plain-SOCKS5 proxies this bot uses.
TYPE_SOCKS5 = 1
PROTOCOL_SOCKS5 = 1


class MultiloginProxyClient:
    def __init__(self, bearer_token: str,
                base_url: str = "https://api.multilogin.com") -> None:
        self.bearer_token = bearer_token
        self.base_url = base_url.rstrip("/")

    def _call(self, method: str, path: str, payload: dict) -> dict:
        response = requests.request(
            method, f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.bearer_token}",
                     "Content-Type": "application/json"},
            json=payload, timeout=30)
        if response.status_code >= 400:
            print(f"[-] MLX {method} {path} -> {response.status_code} "
                  f"{response.text[:300]}")
            response.raise_for_status()
        return response.json() or {}

    def check_proxy(self, server: str, port: int, username: str = "",
                    password: str = "", proxy_type: str = "socks5",
                    detect_type: str = "IP2Location") -> dict:
        """Test an endpoint from MLX's network, without touching a profile.

        Returns whatever MLX's detector saw (city/isp/timezone/etc, shape
        depends on `detect_type`) -- the cheap way to confirm a proxy is
        alive and to see what identity it reports before assigning it
        anywhere.
        """
        return self._call("POST", "/mobile_profiles/proxy/check", {
            "detect_type": detect_type,
            "proxy_type": proxy_type,
            "server": server,
            "port": int(port),
            "username": username,
            "password": password,
        })

    def set_proxy(self, profile_id: str, server: str, port: int,
                  username: str = "", password: str = "",
                  type_id: int = TYPE_SOCKS5,
                  protocol: int = PROTOCOL_SOCKS5) -> dict:
        """Point `profile_id` at this proxy. Only `id` and `proxy_config` are
        sent, so nothing else on the profile (tags, name) is touched."""
        return self._call("PUT", "/mobile_profiles/phone/update", {
            "id": str(profile_id),
            "proxy_config": {
                "use_proxy_cfg": False,
                "type_id": int(type_id),
                "protocol": int(protocol),
                "server": server,
                "port": int(port),
                "username": username,
                "password": password,
            },
        })
