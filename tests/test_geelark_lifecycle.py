"""Warmup -> Active_Posting tag lifecycle for Geelark profiles.

No Airtable, no MultiLogin -- tags are the only state, confirmed protocol
2026-08-29. Real HTTP is faked throughout; the point of these tests is the
sequencing (retag only on success, no retag for Active_Posting, real-proxy
filtering by GEELARK_PROXY_REBOOT_URLS) not the API clients themselves.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adb_bot.automation import geelark_lifecycle as lifecycle
from adb_bot.clients.geelark.session import GeelarkSession
from adb_bot.core.models import Profile

REAL_PROXIES = [
    {"id": "p54015", "server": "162.55.84.35", "port": 54015},
    {"id": "p54018", "server": "162.55.84.35", "port": 54018},
]
MLX_PROXY = {"id": "p1080", "server": "gate.multilogin.com", "port": 1080}
REBOOT_CONFIG = {54015: {"reboot": "https://x/1", "http_port": 44015},
                 54018: {"reboot": "https://x/2", "http_port": 44018}}

FAKE_PROFILE = Profile(id="ph1", status="ready", ip="1.2.3.4", port="5555")


class FakeProxyClient:
    def __init__(self, proxies):
        self._proxies = proxies

    def list_proxies(self):
        return list(self._proxies)


class FakeTagClient:
    def __init__(self, existing_tags=None):
        self._by_name = dict(existing_tags or {})

    def tag_ids_by_name(self, refresh=False):
        return dict(self._by_name)

    def ensure_tag(self, name, color="blue"):
        self._by_name.setdefault(name, f"id-{name}")
        return self._by_name[name]


class FakePhoneClient:
    def __init__(self, phones):
        self._phones = phones
        self.update_calls = []

    def list_phones(self):
        return list(self._phones)

    def update_phone(self, phone_id, **kwargs):
        self.update_calls.append((phone_id, kwargs))


class RealProxiesTest(unittest.TestCase):
    def test_filters_to_real_ports_only(self):
        with patch("adb_bot.clients.geelark.proxies.GeelarkProxyClient",
                  lambda transport: FakeProxyClient(REAL_PROXIES + [MLX_PROXY])), \
             patch.object(lifecycle, "load_reboot_config", lambda: REBOOT_CONFIG):
            result = lifecycle._real_proxies(transport=None)
        self.assertEqual([p["port"] for p in result], [54015, 54018])

    def test_dedupes_a_repeated_port(self):
        dup = REAL_PROXIES + [dict(REAL_PROXIES[0], id="p54015-dup")]
        with patch("adb_bot.clients.geelark.proxies.GeelarkProxyClient",
                  lambda transport: FakeProxyClient(dup)), \
             patch.object(lifecycle, "load_reboot_config", lambda: REBOOT_CONFIG):
            result = lifecycle._real_proxies(transport=None)
        self.assertEqual(len(result), 2)

    def test_raises_with_no_reboot_config(self):
        """A missing GEELARK_PROXY_REBOOT_URLS must fail loudly, not silently
        fall back to treating every saved proxy (including MLX's relay) as
        one of ours."""
        with patch("adb_bot.clients.geelark.proxies.GeelarkProxyClient",
                  lambda transport: FakeProxyClient(REAL_PROXIES)), \
             patch.object(lifecycle, "load_reboot_config", lambda: {}):
            with self.assertRaises(RuntimeError):
                lifecycle._real_proxies(transport=None)


class NextRealProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adbbot-lifecycle-")
        self.state_file = Path(self.tmp) / "rotation.json"
        patcher = patch.object(lifecycle, "_ROTATION_STATE_FILE", self.state_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_robins_across_calls(self):
        with patch("adb_bot.clients.geelark.proxies.GeelarkProxyClient",
                  lambda transport: FakeProxyClient(REAL_PROXIES)), \
             patch.object(lifecycle, "load_reboot_config", lambda: REBOOT_CONFIG):
            first = lifecycle._next_real_proxy(transport=None)
            second = lifecycle._next_real_proxy(transport=None)
            third = lifecycle._next_real_proxy(transport=None)
        self.assertNotEqual(first["port"], second["port"])
        self.assertEqual(first["port"], third["port"])


class PhonesByTagTest(unittest.TestCase):
    def test_filters_by_tag_name(self):
        phones = [
            {"id": "1", "tags": [{"name": "Warmup"}]},
            {"id": "2", "tags": [{"name": "Active_Posting"}]},
            {"id": "3", "tags": []},
        ]
        with patch.object(lifecycle, "GeelarkPhoneClient",
                         lambda transport: FakePhoneClient(phones)):
            result = lifecycle.phones_by_tag("Warmup", transport=None)
        self.assertEqual([p["id"] for p in result], ["1"])


class RetagTest(unittest.TestCase):
    def test_swaps_warmup_for_active_posting_keeping_other_tags(self):
        phones = [{"id": "ph1", "tags": ["Warmup", "gmail connected"]}]
        phone_client = FakePhoneClient(phones)
        tag_client = FakeTagClient({"gmail connected": "id-gmail"})
        with patch.object(lifecycle, "GeelarkPhoneClient", lambda transport: phone_client), \
             patch.object(lifecycle, "GeelarkTagClient", lambda transport: tag_client):
            lifecycle._retag("ph1", transport=None, remove=lifecycle.TAG_WARMUP,
                            add=lifecycle.TAG_ACTIVE_POSTING)
        phone_id, kwargs = phone_client.update_calls[0]
        self.assertEqual(phone_id, "ph1")
        # Active_Posting's id and gmail connected's id, Warmup's gone.
        self.assertIn(tag_client._by_name["Active_Posting"], kwargs["tag_ids"])
        self.assertIn("id-gmail", kwargs["tag_ids"])
        self.assertNotIn("id-warmup-does-not-exist", kwargs["tag_ids"])


def _fake_session():
    return GeelarkSession(profile=FAKE_PROFILE, lease=None, rotation={},
                          phone_id="ph1", transport=None)


class WarmupCycleTest(unittest.TestCase):
    def test_success_retags_and_reports_warmed_up(self):
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_retag") as retag, \
             patch("adb_bot.automation.flows.instagram.InstagramWarmUpDay1Flow.run",
                  return_value={"aborted": False}):
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object())
        self.assertEqual(out["result"], "warmed_up")
        retag.assert_called_once()
        self.assertEqual(retag.call_args.kwargs["remove"], lifecycle.TAG_WARMUP)
        self.assertEqual(retag.call_args.kwargs["add"], lifecycle.TAG_ACTIVE_POSTING)

    def test_aborted_run_does_not_retag(self):
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_retag") as retag, \
             patch("adb_bot.automation.flows.instagram.InstagramWarmUpDay1Flow.run",
                  return_value={"aborted": True}):
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object())
        self.assertEqual(out["result"], "aborted")
        retag.assert_not_called()

    def test_unreachable_over_adb_never_touches_the_tag(self):
        """A phone that never came up over ADB says nothing about whether it
        is actually warmed up -- retagging here would be a false positive."""
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), None)), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_retag") as retag:
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object())
        self.assertEqual(out["result"], "could_not_reach_over_adb")
        retag.assert_not_called()

    def test_session_is_always_stopped_even_on_exception(self):
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session") as stop, \
             patch("adb_bot.automation.flows.instagram.InstagramWarmUpDay1Flow.run",
                  side_effect=RuntimeError("boom")):
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object())
        self.assertEqual(out["result"], "error")
        stop.assert_called_once()


class ActivePostingCycleTest(unittest.TestCase):
    def test_no_media_path_only_scrolls_no_retag_available(self):
        """Active_Posting is permanent -- this cycle has no retag call at
        all, unlike warmup."""
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.run",
                  return_value={"aborted": False}) as scroll:
            out = lifecycle.run_active_posting_cycle("ph1", "Test 1", adb_client=object())
        self.assertEqual(out["result"], "scrolled_only")
        scroll.assert_called_once()

    def test_uses_the_short_scroll_duration(self):
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.__init__",
                  return_value=None) as init, \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.run",
                  return_value={"aborted": False}):
            lifecycle.run_active_posting_cycle("ph1", "Test 1", adb_client=object())
        init.assert_called_once_with(scroll_seconds=lifecycle.ACTIVE_POSTING_SCROLL_SECONDS)

    def test_media_path_posts_after_scrolling(self):
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.run",
                  return_value={"aborted": False}), \
             patch("adb_bot.automation.flows.instagram_reel.InstagramReelUploadFlow.run",
                  return_value={"success": True}) as post:
            out = lifecycle.run_active_posting_cycle(
                "ph1", "Test 1", adb_client=object(), media_path="/tmp/x.mp4")
        self.assertEqual(out["result"], "posted")
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
