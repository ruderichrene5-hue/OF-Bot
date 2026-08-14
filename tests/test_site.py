"""The public dashboard: it must not open without the password, and must not
rebuild itself once per visitor."""

import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from adb_bot.automation import site


class PasswordTest(unittest.TestCase):
    def test_a_password_verifies_against_its_own_hash(self):
        encoded = site.hash_password("hunter2")
        self.assertTrue(site.verify_password("hunter2", encoded))
        self.assertFalse(site.verify_password("hunter3", encoded))

    def test_every_hash_is_salted_differently(self):
        self.assertNotEqual(site.hash_password("same"), site.hash_password("same"))

    def test_a_broken_hash_denies_rather_than_raising(self):
        for junk in ("", "nonsense", "scrypt$zz$zz", "md5$aa$bb", None):
            self.assertFalse(site.verify_password("hunter2", junk))


class SessionTest(unittest.TestCase):
    def test_a_fresh_token_is_valid_and_a_foreign_one_is_not(self):
        token = site.make_session("secret-a")
        self.assertTrue(site.valid_session(token, "secret-a"))
        self.assertFalse(site.valid_session(token, "secret-b"))

    def test_an_expired_token_is_refused(self):
        old = site.make_session("s", now=time.time() - 100, lifetime=10)
        self.assertFalse(site.valid_session(old, "s"))

    def test_a_tampered_expiry_does_not_extend_the_session(self):
        token = site.make_session("s", now=time.time() - 100, lifetime=10)
        _, signature = token.rsplit(".", 1)
        forged = f"{int(time.time()) + 9999}.{signature}"
        self.assertFalse(site.valid_session(forged, "s"))

    def test_junk_is_refused_without_raising(self):
        for junk in ("", "no-dot", "abc.def", "12x3.deadbeef", None):
            self.assertFalse(site.valid_session(junk, "s"))


class ThrottleTest(unittest.TestCase):
    def test_an_address_is_locked_after_the_limit(self):
        throttle = site.Throttle(limit=3, window=60)
        for _ in range(3):
            self.assertEqual(throttle.locked("1.2.3.4"), 0)
            throttle.failed("1.2.3.4")
        self.assertGreater(throttle.locked("1.2.3.4"), 0)
        self.assertEqual(throttle.locked("5.6.7.8"), 0, "one address, not everybody")

    def test_the_lock_expires(self):
        throttle = site.Throttle(limit=1, window=60)
        throttle.failed("1.2.3.4", now=1000.0)
        self.assertGreater(throttle.locked("1.2.3.4", now=1001.0), 0)
        self.assertEqual(throttle.locked("1.2.3.4", now=1061.0), 0)

    def test_a_success_clears_the_strikes(self):
        throttle = site.Throttle(limit=2, window=60)
        throttle.failed("1.2.3.4")
        throttle.passed("1.2.3.4")
        throttle.failed("1.2.3.4")
        self.assertEqual(throttle.locked("1.2.3.4"), 0)


class PageCacheTest(unittest.TestCase):
    def _cache(self, ttl=300):
        self.built = 0

        def collect(airtable=None, use_cache=True):
            self.built += 1
            return {"n": self.built}

        def render(data, live=True, title="", refresh_seconds=0):
            self.refresh_seconds = refresh_seconds
            return f"page {data['n']}"

        return site.PageCache(ttl=ttl, collect=collect, render=render)

    def test_one_render_serves_the_whole_interval(self):
        cache = self._cache(ttl=300)
        self.assertEqual(cache.page(now=1000.0), "page 1")
        self.assertEqual(cache.page(now=1200.0), "page 1")
        self.assertEqual(self.built, 1, "a second visitor must not trigger a sweep")

    def test_it_rebuilds_once_the_interval_passes(self):
        cache = self._cache(ttl=300)
        cache.page(now=1000.0)
        self.assertEqual(cache.page(now=1301.0), "page 2")

    def test_the_page_is_told_the_real_interval(self):
        cache = self._cache(ttl=300)
        cache.page(now=1000.0)
        self.assertEqual(self.refresh_seconds, 300,
                         "the header must not promise a freshness it lacks")

    def test_concurrent_visitors_share_one_build(self):
        cache = self._cache(ttl=300)
        pages = []
        threads = [threading.Thread(target=lambda: pages.append(cache.page()))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.built, 1)
        self.assertEqual(set(pages), {"page 1"})


class ServerTest(unittest.TestCase):
    """A real socket: the point is what an unauthenticated request receives."""

    password = "correct horse"

    def setUp(self):
        cache = site.PageCache(ttl=300, collect=lambda **kw: {},
                               render=lambda *a, **kw: "<h1>SECRET REPORT</h1>")
        handler = type("TestHandler", (site.SiteHandler,), {
            "cache": cache,
            "throttle": site.Throttle(),
            "password_hash": site.hash_password(self.password),
            "secret": "test-secret",
            "page_title": "ADB bot",
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _get(self, path="/", cookie=""):
        request = urllib.request.Request(self.url + path)
        if cookie:
            request.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), exc.headers

    def _post(self, password):
        request = urllib.request.Request(
            self.url + "/login", data=f"password={password}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers

    def test_the_report_is_not_served_without_a_login(self):
        status, body, _ = self._get("/")
        self.assertEqual(status, 200)
        self.assertNotIn("SECRET REPORT", body)
        self.assertIn("Sign in", body)

    def test_a_forged_cookie_does_not_open_it(self):
        _, body, _ = self._get("/", cookie=f"{site.COOKIE_NAME}=9999999999.deadbeef")
        self.assertNotIn("SECRET REPORT", body)

    def test_the_right_password_hands_back_a_working_session(self):
        status, headers = self._post(self.password)
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        token = cookie.split(";")[0]
        _, body, _ = self._get("/", cookie=token)
        self.assertIn("SECRET REPORT", body)

    def test_the_wrong_password_is_refused_and_eventually_throttled(self):
        for _ in range(site.MAX_FAILURES):
            status, _ = self._post("wrong")
            self.assertEqual(status, 401)
        self.assertEqual(self._post(self.password)[0], 429,
                         "a locked address waits even with the right password")

    def test_health_needs_no_password(self):
        status, body, _ = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), "ok")

    def test_unknown_paths_are_not_the_report(self):
        status, body, _ = self._get("/admin")
        self.assertEqual(status, 404)
        self.assertNotIn("SECRET REPORT", body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()


class ClearHumanFlagTest(unittest.TestCase):
    """`AirtableClient.clear_human_flag`, the write the issue-tag mirror makes
    when a person takes the `Issue` tag off a phone in MultiLogin.

    `Needs Human Check` is half of a handshake: `profiles_awaiting_recovery`
    finds profiles by the pair "unchecked but `Flagged At` still stamped", and
    the recovery pass is what then sets Status back to Active and re-queues what
    was stuck. A button that wrote the rest of that state itself would leave the
    profile invisible to every loop -- the failure that needed two profiles
    un-parked by hand on 2026-08-06.
    """

    class FakeClient:
        def __init__(self, flagged=True, notes=""):
            self.fields = {"Needs Human Check": flagged, "Issue Notes": notes}
            self.patched = None

        def _get_field(self, table, record_id, field):
            return self.fields.get(field)

        def _patch_in(self, table, record_id, fields, typecast=True):
            self.patched = (table, record_id, fields)
            return True

    def _clear(self, client, note="looked"):
        from adb_bot.clients.airtable import AirtableClient
        return AirtableClient.clear_human_flag(client, "recX", note=note)

    def test_it_unticks_the_checkbox(self):
        client = self.FakeClient()
        self.assertTrue(self._clear(client))
        self.assertIs(client.patched[2]["Needs Human Check"], False)

    def test_it_writes_nothing_else_that_the_recovery_pass_reads(self):
        client = self.FakeClient()
        self._clear(client)
        written = set(client.patched[2])
        self.assertNotIn("Status", written)
        self.assertNotIn("Issue Reason", written)
        self.assertNotIn("Flagged At", written)

    def test_a_profile_nobody_flagged_is_left_alone(self):
        """A stale page or a double click must not un-flag something that is not
        flagged -- and this is also what makes the endpoint safe with any record
        id at all: the worst it can do is untick a ticked box."""
        client = self.FakeClient(flagged=False)
        self.assertFalse(self._clear(client))
        self.assertIsNone(client.patched)

    def test_the_note_is_prepended_not_replaced(self):
        client = self.FakeClient(notes="[old] Device Unreachable: adb offline")
        self._clear(client, note="Cleared from the dashboard")
        notes = client.patched[2]["Issue Notes"]
        self.assertIn("Cleared from the dashboard", notes.splitlines()[0])
        self.assertIn("adb offline", notes)

    def test_a_read_failure_does_not_write(self):
        class Exploding(self.FakeClient):
            def _get_field(inner, table, record_id, field):
                raise RuntimeError("429")
        client = Exploding()
        self.assertFalse(self._clear(client))
        self.assertIsNone(client.patched)
