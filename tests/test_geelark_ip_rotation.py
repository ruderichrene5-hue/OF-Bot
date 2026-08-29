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
    """Classifying what a rotation URL answered.

    400 means two opposite things on this vendor, which is the whole reason
    this classification exists rather than a bare status-code check.
    """

    def test_the_wrong_url_is_unusable(self):
        """HTML body, no SRVID: the load balancer rejected it before any
        backend. The URL is wrong; it says nothing about the token."""
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("<h1>400 Bad request</h1>", 400)):
            result = ProxyRotator.probe_link("https://example/rotate")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 400)

    def test_a_cooldown_is_reported_as_refused(self):
        """400 with body ERROR on the *working* endpoint means "called again
        too soon". The link is fine; retrying instantly is not."""
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("ERROR", 400)):
            result = ProxyRotator.probe_link("https://example/rotate")
        self.assertFalse(result["ok"])

    def test_a_two_hundred_saying_ERROR_is_still_unusable(self):
        """Status 200 with an ERROR body would pass a status-code-only check
        and rotate nothing, silently, for ever."""
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("ERROR_MODEM_NOT_FOUND", 200)):
            result = ProxyRotator.probe_link("https://example/rotate")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertIn("ERROR_MODEM_NOT_FOUND", result["detail"])

    def test_a_genuine_success_is_usable(self):
        """What the working link actually returns: 200, body OK."""
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
        self.assertTrue(result["accepted"])
        self.assertEqual(result["before"], result["after"])

    def test_a_cooldown_returns_immediately_without_polling(self):
        """A refused call must not then sit and poll for a change that cannot
        come -- and must be distinguishable from "fired but the IP stayed"."""
        rotator = self._rotator()
        with patch.object(ProxyRotator, "exit_ip", return_value="1.1.1.1"), \
             patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("ERROR", 400)), \
             patch("adb_bot.clients.geelark.ip_rotation.time.sleep",
                   lambda *_: None):
            result = rotator.rotate_and_verify(54015)
        self.assertFalse(result["accepted"])
        self.assertFalse(result["changed"])
        self.assertIn("ERROR", result["detail"])

    def test_rotate_reports_acceptance_rather_than_raising(self):
        """The vendor's refusal is an answer, not an exception; raising would
        make callers retry into the cooldown they just hit."""
        rotator = self._rotator()
        with patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("ERROR", 400)):
            fired = rotator.rotate(54015)
        self.assertFalse(fired["accepted"])
        self.assertEqual(fired["status"], 400)

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

    def test_rotate_until_changed_retries_on_an_unchanged_ip(self):
        """Confirmed protocol 2026-08-29: an unchanged IP gets one more rotate
        call after a pause, not a silent pass-through to the next profile."""
        rotator = self._rotator()
        seen = iter(["1.1.1.1", "1.1.1.1", "1.1.1.1", "2.2.2.2"])
        sleeps = []
        with patch.object(ProxyRotator, "exit_ip", side_effect=lambda *_a: next(seen)), \
             patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("OK", 200)), \
             patch("adb_bot.clients.geelark.ip_rotation.time.sleep",
                   side_effect=lambda s: sleeps.append(s)):
            result = rotator.rotate_until_changed(
                54015, max_retries=2, retry_pause_seconds=30.0, timeout_seconds=0)
        self.assertTrue(result["changed"])
        self.assertEqual(result["after"], "2.2.2.2")
        self.assertEqual(result["retries"], 1)
        self.assertIn(30.0, sleeps)

    def test_rotate_until_changed_gives_up_after_max_retries(self):
        """A proxy that genuinely cannot produce a new address must not wedge
        the caller forever -- report the unchanged result, don't raise."""
        rotator = self._rotator()
        with patch.object(ProxyRotator, "exit_ip", return_value="1.1.1.1"), \
             patch("adb_bot.clients.geelark.ip_rotation.requests.get",
                   return_value=FakeResponse("OK", 200)), \
             patch("adb_bot.clients.geelark.ip_rotation.time.sleep",
                   lambda *_: None):
            result = rotator.rotate_until_changed(
                54015, max_retries=2, retry_pause_seconds=30.0, timeout_seconds=0)
        self.assertFalse(result["changed"])
        self.assertEqual(result["retries"], 2)

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
