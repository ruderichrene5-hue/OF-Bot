"""MLX proxy read/write: `proxy/check` and `phone/update`'s `proxy_config`."""

import json
import unittest
from unittest.mock import patch

from adb_bot.clients.multilogin.proxy import (
    PROTOCOL_SOCKS5,
    TYPE_SOCKS5,
    MultiloginProxyClient,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class ProxyClientTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _request(self, responses):
        queue = list(responses)

        def fake_request(method, url, headers=None, json=None, timeout=None):
            self.calls.append((method, url, json))
            return queue.pop(0) if queue else FakeResponse({"data": {}})
        return fake_request

    def _client(self, responses=()):
        client = MultiloginProxyClient("tok")
        return client, patch(
            "adb_bot.clients.multilogin.proxy.requests.request",
            self._request(responses))

    def test_check_proxy_hits_the_check_endpoint(self):
        client, ctx = self._client([FakeResponse({
            "data": {"city": "Berlin", "isp": "Vodafone GmbH",
                     "detect_status": True}})])
        with ctx:
            result = client.check_proxy("162.55.84.35", 54015, "user", "pw")
        method, url, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/mobile_profiles/proxy/check"))
        self.assertEqual(body["server"], "162.55.84.35")
        self.assertEqual(body["port"], 54015)
        self.assertEqual(result["data"]["isp"], "Vodafone GmbH")

    def test_set_proxy_writes_proxy_config_only(self):
        """Only `id` and `proxy_config` are sent -- a caller must never have
        to worry this clears a profile's name or tags as a side effect."""
        client, ctx = self._client([FakeResponse({"data": {}})])
        with ctx:
            client.set_proxy("123", "162.55.84.35", 54015, "user", "pw")
        method, url, body = self.calls[0]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/mobile_profiles/phone/update"))
        self.assertEqual(set(body.keys()), {"id", "proxy_config"})
        self.assertEqual(body["id"], "123")
        cfg = body["proxy_config"]
        self.assertEqual(cfg["server"], "162.55.84.35")
        self.assertEqual(cfg["port"], 54015)
        self.assertEqual(cfg["type_id"], TYPE_SOCKS5)
        self.assertEqual(cfg["protocol"], PROTOCOL_SOCKS5)
        self.assertFalse(cfg["use_proxy_cfg"])

    def test_a_dead_proxy_raises_rather_than_silently_saving(self):
        client, ctx = self._client([FakeResponse(
            {"status": {"message": "check proxy failed"}}, status_code=500)])
        with ctx:
            with self.assertRaises(Exception):
                client.set_proxy("123", "dead.example", 1, "u", "p")
