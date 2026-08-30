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

    def test_a_duplicate_port_deterministically_picks_the_lowest_serial_no(self):
        """Confirmed live 2026-08-30: serialNo 11/12 duplicate 6/5 on the
        same ports. Without pinning this, whichever the API lists first
        silently gets used, splitting real usage across both rows for the
        same physical modem and making the duplicate look "still in use"
        -- blocking its deletion. Order-independent: the duplicate can
        appear before or after the original in list_proxies()'s response."""
        original = {"id": "p54015-original", "server": "162.55.84.35",
                   "port": 54015, "serialNo": 6}
        duplicate = {"id": "p54015-duplicate", "server": "162.55.84.35",
                    "port": 54015, "serialNo": 11}
        for ordering in ([duplicate, original], [original, duplicate]):
            with patch("adb_bot.clients.geelark.proxies.GeelarkProxyClient",
                      lambda transport: FakeProxyClient(ordering)), \
                 patch.object(lifecycle, "load_reboot_config", lambda: REBOOT_CONFIG):
                result = lifecycle._real_proxies(transport=None)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["id"], "p54015-original")

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
        # A phone's own tags are {"name": ...} dicts on the real API, never
        # bare strings -- confirmed live 2026-08-30 when a fixture shaped
        # like this masked a crash that hit 51 of 53 real profiles.
        phones = [{"id": "ph1", "tags": [{"name": "Warmup"},
                                        {"name": "gmail connected"}]}]
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
    def setUp(self):
        # Screen-health pre-check is exercised separately in
        # ChallengeAbortTest; a healthy screen (None) here so these tests
        # keep exercising just the flow-outcome branches.
        patcher = patch.object(lifecycle, "_check_for_challenge_and_abort",
                               return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        """An exception is retryable, so this now relaunches -- one
        stop_session per attempt, not one total."""
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session") as stop, \
             patch("adb_bot.automation.flows.instagram.InstagramWarmUpDay1Flow.run",
                  side_effect=RuntimeError("boom")):
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object(),
                                             max_attempts=1)
        self.assertEqual(out["result"], "error")
        stop.assert_called_once()


class WarmupCycleRetryTest(unittest.TestCase):
    """A launch that hits an infrastructure hiccup (error / could not reach
    over ADB) relaunches the same profile instead of being left for someone
    to notice and rerun by hand -- confirmed gap 2026-08-30, after 47/155
    profiles sat on a lease-timeout error in one unattended run."""

    def setUp(self):
        patcher = patch.object(lifecycle, "_check_for_challenge_and_abort",
                               return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_up_to_max_attempts_then_gives_up(self):
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), None)), \
             patch.object(lifecycle, "stop_session") as stop:
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object(),
                                             max_attempts=3)
        self.assertEqual(out["result"], "could_not_reach_over_adb")
        self.assertEqual(out["attempt"], 3)
        self.assertEqual(stop.call_count, 3)

    def test_a_later_success_stops_the_retries(self):
        attempts = {"n": 0}

        def launch(*_a, **_kw):
            attempts["n"] += 1
            target = None if attempts["n"] < 2 else "1.2.3.4:5555"
            return _fake_session(), target

        with patch.object(lifecycle, "_launch", side_effect=launch), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_retag") as retag, \
             patch("adb_bot.automation.flows.instagram.InstagramWarmUpDay1Flow.run",
                  return_value={"aborted": False}):
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object(),
                                             max_attempts=3)
        self.assertEqual(out["result"], "warmed_up")
        self.assertEqual(out["attempt"], 2)
        retag.assert_called_once()

    def test_a_challenge_abort_is_never_retried(self):
        """Not in _RETRYABLE_RESULTS -- relaunching won't clear a captcha."""
        calls = {"n": 0}

        def launch(*_a, **_kw):
            calls["n"] += 1
            return _fake_session(), "1.2.3.4:5555"

        with patch.object(lifecycle, "_launch", side_effect=launch), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_check_for_challenge_and_abort",
                          return_value="human verification"), \
             patch.object(lifecycle, "_retag"):
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client=object(),
                                             max_attempts=3)
        self.assertEqual(out["result"], "aborted_human_verification")
        self.assertEqual(calls["n"], 1)


class ActivePostingCycleTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(lifecycle, "_check_for_challenge_and_abort",
                               return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

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


class ChallengeAbortTest(unittest.TestCase):
    """A challenge screen showing up before a cycle even starts must abort
    it and retag, not let it scroll/post through the screen. Confirmed
    launch-readiness gap 2026-08-30 -- this previously did nothing."""

    def _run_warmup(self, screen_tag):
        adb_client = unittest.mock.MagicMock()
        with patch.object(lifecycle, "_launch",
                         return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_retag") as retag, \
             patch("adb_bot.automation.flows.verification_driver.AdbChallengeDriver.read_screen",
                  return_value="whatever"), \
             patch.object(lifecycle, "simplified_status_tag", return_value=screen_tag), \
             patch("adb_bot.automation.flows.instagram.InstagramWarmUpDay1Flow.run") as flow_run:
            out = lifecycle.run_warmup_cycle("ph1", "Test 1", adb_client)
        return out, retag, flow_run

    def test_human_verification_screen_aborts_warmup_before_it_starts(self):
        out, retag, flow_run = self._run_warmup("human verification")
        self.assertEqual(out["result"], "aborted_human_verification")
        flow_run.assert_not_called()
        retag.assert_called_once_with("ph1", unittest.mock.ANY,
                                      remove=lifecycle.TAG_WARMUP,
                                      add="human verification", logger=None)

    def test_banned_screen_aborts_warmup_and_retags(self):
        out, retag, flow_run = self._run_warmup(lifecycle.TAG_BANNED)
        self.assertEqual(out["result"], "aborted_banned")
        flow_run.assert_not_called()
        retag.assert_called_once_with("ph1", unittest.mock.ANY,
                                      remove=lifecycle.TAG_WARMUP,
                                      add=lifecycle.TAG_BANNED, logger=None)

    def test_healthy_screen_lets_warmup_proceed(self):
        out, retag, flow_run = self._run_warmup(None)
        flow_run.assert_called_once()
        retag.assert_not_called()

    def test_human_verification_screen_aborts_active_posting_before_it_starts(self):
        adb_client = unittest.mock.MagicMock()
        with patch.object(lifecycle, "_launch",
                         return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_retag") as retag, \
             patch("adb_bot.automation.flows.verification_driver.AdbChallengeDriver.read_screen",
                  return_value="whatever"), \
             patch.object(lifecycle, "simplified_status_tag",
                         return_value="human verification"), \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.run") as scroll_run:
            out = lifecycle.run_active_posting_cycle("ph1", "Test 1", adb_client)
        self.assertEqual(out["result"], "aborted_human_verification")
        scroll_run.assert_not_called()
        retag.assert_called_once_with("ph1", unittest.mock.ANY,
                                      remove=lifecycle.TAG_ACTIVE_POSTING,
                                      add="human verification", logger=None)


class InReviewRecheckCycleTest(unittest.TestCase):
    """Daily look at an `in review` profile: read-only, no challenge-solving,
    then retag by what the screen actually shows. Confirmed protocol
    2026-08-30."""

    def _run(self, screen_tag, resolved_tag=lifecycle.TAG_ACTIVE_POSTING):
        adb_client = unittest.mock.MagicMock()
        with patch.object(lifecycle, "_launch",
                         return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_open_instagram"), \
             patch.object(lifecycle, "_retag") as retag, \
             patch("adb_bot.automation.flows.verification_driver.AdbChallengeDriver.read_screen",
                  return_value="whatever is on screen"), \
             patch.object(lifecycle, "simplified_status_tag", return_value=screen_tag):
            out = lifecycle.run_in_review_recheck_cycle(
                "ph1", "Test 1", adb_client, resolved_tag=resolved_tag)
        return out, retag, adb_client

    def test_healthy_feed_clears_to_the_resolved_tag(self):
        out, retag, _ = self._run(screen_tag=None)
        self.assertEqual(out["result"], "cleared")
        retag.assert_called_once_with("ph1", unittest.mock.ANY,
                                      remove=lifecycle.TAG_IN_REVIEW,
                                      add=lifecycle.TAG_ACTIVE_POSTING, logger=None)

    def test_healthy_feed_can_clear_to_warmup_instead(self):
        out, retag, _ = self._run(screen_tag=None, resolved_tag=lifecycle.TAG_WARMUP)
        self.assertEqual(retag.call_args.kwargs["add"], lifecycle.TAG_WARMUP)

    def test_still_in_review_leaves_the_tag_untouched(self):
        out, retag, _ = self._run(screen_tag=lifecycle.TAG_IN_REVIEW)
        self.assertEqual(out["result"], "still_in_review")
        retag.assert_not_called()

    def test_logged_out_screen_retags_to_logged_out(self):
        out, retag, _ = self._run(screen_tag=lifecycle.TAG_LOGGED_OUT)
        self.assertEqual(out["result"], "logged_out")
        retag.assert_called_once_with("ph1", unittest.mock.ANY,
                                      remove=lifecycle.TAG_IN_REVIEW,
                                      add=lifecycle.TAG_LOGGED_OUT, logger=None)

    def test_banned_screen_retags_to_banned(self):
        out, retag, _ = self._run(screen_tag=lifecycle.TAG_BANNED)
        self.assertEqual(out["result"], "banned")
        retag.assert_called_once()

    def test_unrecognised_screen_leaves_the_tag_untouched(self):
        """Only the 3 specified outcomes act -- a captcha or code screen
        showing up here is not one of them."""
        out, retag, _ = self._run(screen_tag="human verification")
        self.assertEqual(out["result"], "unchanged")
        retag.assert_not_called()

    def test_always_force_stops_instagram_before_closing(self):
        _, _, adb_client = self._run(screen_tag=None)
        calls = [str(c) for c in adb_client.run_command.call_args_list]
        self.assertTrue(any("force-stop com.instagram.android" in c for c in calls))

    def test_unreachable_over_adb_never_retags(self):
        adb_client = unittest.mock.MagicMock()
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), None)), \
             patch.object(lifecycle, "stop_session"), \
             patch.object(lifecycle, "_retag") as retag:
            out = lifecycle.run_in_review_recheck_cycle("ph1", "Test 1", adb_client)
        self.assertEqual(out["result"], "could_not_reach_over_adb")
        retag.assert_not_called()


class ActivePostingCycleMediaTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(lifecycle, "_check_for_challenge_and_abort",
                               return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_media_path_posts_after_scrolling(self):
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.run",
                  return_value={"aborted": False}), \
             patch("adb_bot.automation.flows.instagram_reel.InstagramReelUploadU2Flow.run",
                  return_value={"success": True}) as post:
            out = lifecycle.run_active_posting_cycle(
                "ph1", "Test 1", adb_client=object(), media_path="/tmp/x.mp4")
        self.assertEqual(out["result"], "posted")
        post.assert_called_once()

    def test_a_conclusive_post_failure_is_retried(self):
        """error dialog / draft prompt / stuck composer -- post_ledger has
        nothing recorded yet, so a fresh attempt can't double-post."""
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.run",
                  return_value={"aborted": False}), \
             patch("adb_bot.automation.flows.instagram_reel.InstagramReelUploadU2Flow.run",
                  return_value={"success": False, "uncertain": False}) as post:
            out = lifecycle.run_active_posting_cycle(
                "ph1", "Test 1", adb_client=object(), media_path="/tmp/x.mp4",
                max_attempts=3)
        self.assertEqual(out["result"], "post_failed")
        self.assertEqual(post.call_count, 3)

    def test_an_uncertain_post_is_never_retried(self):
        """Share was tapped but nothing proved it landed or failed -- a
        retry risks a duplicate post, so this must attempt only once
        regardless of max_attempts."""
        with patch.object(lifecycle, "_launch", return_value=(_fake_session(), "1.2.3.4:5555")), \
             patch.object(lifecycle, "stop_session"), \
             patch("adb_bot.automation.flows.instagram.InstagramScrollFlow.run",
                  return_value={"aborted": False}), \
             patch("adb_bot.automation.flows.instagram_reel.InstagramReelUploadU2Flow.run",
                  return_value={"success": False, "uncertain": True}) as post:
            out = lifecycle.run_active_posting_cycle(
                "ph1", "Test 1", adb_client=object(), media_path="/tmp/x.mp4",
                max_attempts=3)
        self.assertEqual(out["result"], "post_uncertain")
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
