"""Rotating the exit IP of the proxies behind the Geelark phones.

Everything here encodes something measured on 2026-08-20, and the two probe
tests exist because both failure modes are actively misleading:

* proxy-seller's **retail** host answers HTTP **400** for a real token, a
  garbage token and no token alike -- it is HAProxy's parse-layer page, emitted
  before routing, so the request never reaches a token check. Reading that 400
  as "our token is invalid" sends you to the wrong fix entirely.
* the **Mobile CRM** host answers HTTP **200** with a plain-text body
  ``ERROR_MODEM_NOT_FOUND``. A caller that checks only the status code would
  call that a working rotation link and quietly never rotate anything.
"""

import unittest
from unittest.mock import patch

from adb_bot.clients.geelark.ip_rotation import (
    ProxyRotationError,
    ProxyRotator,
    load_reboot_config,
)


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


PROXIES = [{"id": "1", "server": "162.55.84.35", "port": 54015,
            "username": "u", "password": "p"}]


class ConfigTest(unittest.TestCase):
    def test_a_bare_string_is_the_reboot_url(self):
        config = load_reboot_config('{"54015": "https://example/rotate"}')
        self.assertEqual(config[54015]["reboot"], "https://example/rotate")

    def test_the_http_port_is_derived_from_the_socks_port(self):
        """This vendor exposes the same tunnel on 44015 and 54015; verified on
        all four endpoints here."""
        config = load_reboot_config('{"54015": "https://example/rotate"}')
        self.assertEqual(config[54015]["http_port"], 44015)

    def test_an_explicit_http_port_wins(self):
        config = load_reboot_config(
            '{"54015": {"reboot": "https://x", "http_port": 9999}}')
        self.assertEqual(config[54015]["http_port"], 9999)

    def test_no_configuration_is_empty_not_an_error(self):
        """An unconfigured host must not raise -- rotation is optional."""
        self.assertEqual(load_reboot_config(""), {})

    def test_broken_json_is_reported_clearly(self):
        with self.assertRaises(ProxyRotationError):
            load_reboot_config("{not json")


class ProbeTest(unittest.TestCase):
    def _rotator(self):
        return ProxyRotator(PROXIES, reboot_config={
            54015: {"reboot": "https://example/rotate", "http_port": 44015}})

    def test_a_four_hundred_is_unusable(self):
        """The retail host's HAProxy page. Not a verdict on the token."""
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("<h1>400 Bad request</h1>", 400)):
            result = ProxyRotator.probe_link("https://example/rotate")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 400)

    def test_a_two_hundred_saying_ERROR_is_still_unusable(self):
        """The regression that matters: status 200, body ERROR_MODEM_NOT_FOUND.

        Checking only the status code would call this a working link.
        """
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("ERROR_MODEM_NOT_FOUND", 200)):
            result = ProxyRotator.probe_link("https://example/rotate")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertIn("ERROR_MODEM_NOT_FOUND", result["detail"])

    def test_a_genuine_success_is_usable(self):
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("OK", 200)):
            self.assertTrue(ProxyRotator.probe_link("https://example/rotate")["ok"])

    def test_an_unreachable_url_is_reported_not_raised(self):
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   side_effect=OSError("no route")):
            result = ProxyRotator.probe_link("https://example/rotate")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])


class RotateTest(unittest.TestCase):
    def _rotator(self):
        return ProxyRotator(PROXIES, reboot_config={
            54015: {"reboot": "https://example/rotate", "http_port": 44015}})

    def test_rotating_an_unconfigured_port_refuses(self):
        with self.assertRaises(ProxyRotationError):
            self._rotator().rotate(59999)

    def test_an_unchanged_ip_is_a_result_not_an_error(self):
        """A mobile proxy can hand back the address it just released. Reporting
        that as success would leave a phone on the IP it was meant to leave."""
        rotator = self._rotator()
        with patch.object(ProxyRotator, "exit_ip", return_value="1.1.1.1"), \
             patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("OK", 200)), \
             patch("adb_bot.clients.geelark.ip_rotation.time.sleep",
                   lambda *_: None):
            result = rotator.rotate_and_verify(54015, timeout_seconds=0)
        self.assertFalse(result["changed"])
        self.assertEqual(result["before"], result["after"])

    def test_a_changed_ip_is_reported_with_both_addresses(self):
        rotator = self._rotator()
        seen = iter(["1.1.1.1", "2.2.2.2", "2.2.2.2"])
        with patch.object(ProxyRotator, "exit_ip", side_effect=lambda *_a: next(seen)), \
             patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("OK", 200)), \
             patch("adb_bot.clients.geelark.ip_rotation.time.sleep",
                   lambda *_: None):
            result = rotator.rotate_and_verify(54015)
        self.assertTrue(result["changed"])
        self.assertEqual(result["before"], "1.1.1.1")
        self.assertEqual(result["after"], "2.2.2.2")

    def test_rotatable_ports_needs_both_a_proxy_and_a_url(self):
        """A URL for a port we do not own, or a port with no URL, is not
        rotatable -- and saying otherwise invites a call that cannot work."""
        rotator = ProxyRotator(PROXIES, reboot_config={
            54015: {"reboot": "https://x", "http_port": 44015},
            59999: {"reboot": "https://y", "http_port": 49999},
        })
        self.assertEqual(rotator.rotatable_ports(), [54015])


if __name__ == "__main__":
    unittest.main()
