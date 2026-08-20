"""The Geelark tab: what it must show, and what it must never claim.

Geelark is a second cloud-phone host under evaluation, not a replacement, so the
filing rule this pins is separation. Its phones have no Airtable row, no model
and no posting history; counting them beside MultiLogin phones would be wrong in
both directions, and the failure would be quiet -- an inventory that reads two
phones larger than the fleet really is.

The other thing pinned here is the tab's honesty about cost. Geelark's OpenAPI
exposes no balance, quota or usage endpoint at all, so any "minutes remaining"
on this page would be invented. The page has been read as authoritative before.
"""

import unittest
from unittest.mock import patch

from adb_bot.automation import report, report_html


def _billing(parallels=4, minutes_left=4194, credit=29.36, time_addon_minutes=0,
             plan="Pro", profiles=400, profiles_available=398):
    return {"parallels": parallels, "minutes_left": minutes_left,
            "credit": credit, "time_addon_minutes": time_addon_minutes,
            "plan": plan, "profiles": profiles,
            "profiles_available": profiles_available, "balance": 0.0,
            "gift": credit, "monthly_rentals": 0, "monthly_fee": 298.6,
            "expires_at": 0}


def _geelark(configured=True, phones=(), error="", tags=(), proxies=(), billing=None):
    running = sum(1 for p in phones if p.get("status") == "started")
    stopped = sum(1 for p in phones if p.get("status") == "stopped")
    reachable = sum(1 for p in phones if p.get("adb") == "active")
    return {
        "configured": configured,
        "phones": list(phones),
        "tags": list(tags),
        "proxies": list(proxies),
        "billing": {} if billing is None else billing,
        "counts": {"phones": len(phones), "running": running, "stopped": stopped,
                   "adb_enabled": reachable, "proxies": len(proxies),
                   "gateways": len({p["endpoint"].split(":")[0] for p in proxies})},
        "error": error,
    }


def _phone(name="YT Nikki 1", status="started", adb="adb-not-enabled",
           country="Germany", proxy="162.55.84.35:54028", tags=("nikki",)):
    return {"id": "633", "name": name, "status": status, "adb": adb,
            "country": country, "os": "Android 15", "device": "OPPO Reno12",
            "timezone": "Europe/Berlin", "proxy": proxy, "tags": list(tags),
            "group": ""}


class GeelarkSectionTest(unittest.TestCase):
    def test_missing_credentials_say_so_instead_of_looking_empty(self):
        """"No phones" and "no credentials" are different answers, and the
        second one is a thing somebody can fix."""
        html = report_html._section_geelark(_geelark(configured=False,
                                                     error="Not configured -- set GEELARK_APP_ID"))
        self.assertIn("not configured", html)
        self.assertIn("GEELARK_APP_ID", html)

    def test_an_empty_dict_does_not_raise(self):
        """The renderer is handed `data.get('geelark') or {}`, so the empty case
        is reached on any page rendered before the collector ran."""
        self.assertIn("not configured", report_html._section_geelark({}))

    def test_adb_off_is_shown_as_not_reachable(self):
        """A phone can be started and still be undrivable. That distinction is
        the whole question of whether the bot can use Geelark, so it gets its
        own column rather than being folded into status."""
        html = report_html._section_geelark(_geelark(phones=[_phone(adb="adb-not-enabled")]))
        self.assertIn("ADB off", html)
        self.assertNotIn("reachable</span>", html.replace("ADB off", ""))

    def test_a_reachable_phone_reads_as_reachable(self):
        html = report_html._section_geelark(_geelark(phones=[_phone(adb="active")]))
        self.assertIn("reachable", html)

    def test_running_and_stopped_are_counted_separately(self):
        """A started phone bills by the minute; the count is the cost signal."""
        html = report_html._section_geelark(_geelark(phones=[
            _phone(name="a", status="started"),
            _phone(name="b", status="stopped"),
        ]))
        self.assertIn("2 cloud phone(s)", html)
        self.assertIn("1 started", html)
        self.assertIn("1 stopped", html)

    def test_running_beyond_the_parallel_slots_is_called_out(self):
        """Parallel slots run a phone at no per-minute charge, but they are not
        a cap -- phones past the slot count start silently billing instead of
        failing. That is the expensive surprise this tab exists to prevent.
        """
        html = report_html._section_geelark(_geelark(
            phones=[_phone(name=f"p{i}", status="started") for i in range(6)],
            billing=_billing(parallels=4)))
        self.assertIn("2 over", html)
        self.assertIn("billing per minute", html)

    def test_within_the_slots_reads_as_costing_nothing(self):
        html = report_html._section_geelark(_geelark(
            phones=[_phone(status="started")], billing=_billing(parallels=4)))
        self.assertIn("within slots", html)

    def test_no_credit_and_no_bought_minutes_is_a_hard_warning(self):
        """With no runway a phone outside a slot cannot start at all, and the
        MultiLogin precedent is that this gets misread as a server fault."""
        html = report_html._section_geelark(_geelark(
            phones=[_phone()],
            billing=_billing(minutes_left=0, credit=0.0, time_addon_minutes=0)))
        self.assertIn("no runway", html)

    def test_an_unreadable_wallet_does_not_invent_a_number(self):
        """Both money endpoints are rate limited hard (the plan one to one call
        a minute), so failing to read them is normal and must not be papered
        over with a zero that reads as "no money"."""
        html = report_html._section_geelark(_geelark(phones=[_phone()], billing={}))
        self.assertIn("no billing read", html)
        self.assertNotIn("no runway", html)

    def test_a_partial_read_still_shows_the_phones(self):
        """If the ADB state cannot be read, the phone table is still worth
        having -- the error goes above it rather than replacing it."""
        html = report_html._section_geelark(
            _geelark(phones=[_phone()], error="ADB state could not be read: boom"))
        self.assertIn("partial", html)
        self.assertIn("YT Nikki 1", html)

    def test_shared_proxy_endpoints_are_called_out(self):
        """Endpoint density is a standing risk on the MLX fleet and would be far
        worse with a handful of endpoints, so sharing is surfaced early."""
        html = report_html._section_geelark(_geelark(
            phones=[_phone()],
            proxies=[{"endpoint": "1.2.3.4:1000", "profiles": 3}]))
        self.assertIn("shared", html)

    def test_the_gateway_host_is_never_called_an_exit_ip(self):
        """Four ports on one gateway host egress from four different addresses
        on this account, so counting hosts as exit IPs said "one IP" about four.

        That is a migration-shaped mistake: IP clustering across models is a
        real risk, and a page that understates diversity would send somebody
        buying proxies they already have.
        """
        html = report_html._section_geelark(_geelark(
            phones=[_phone()],
            proxies=[{"endpoint": "162.55.84.35:54015", "profiles": 1},
                     {"endpoint": "162.55.84.35:54018", "profiles": 1}]))
        self.assertIn("gateway host(s)", html)
        self.assertIn("not</em> the exit IP", html)
        self.assertNotIn("exit IP(s)", html)

    def test_values_are_escaped(self):
        """Names come from a third party and land in HTML."""
        html = report_html._section_geelark(
            _geelark(phones=[_phone(name="<script>x</script>")]))
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)


class GeelarkCollectorTest(unittest.TestCase):
    def test_it_reports_failure_instead_of_raising(self):
        """`_slow` requires its builder to report failures in the value, and the
        house rule is that one panel's outage costs one panel."""
        with patch("adb_bot.clients.geelark.GeelarkTransport") as transport:
            transport.return_value.is_configured = True
            with patch("adb_bot.clients.geelark.GeelarkPhoneClient") as phones:
                phones.return_value.list_phones.side_effect = RuntimeError("down")
                out = report.geelark_status()
        self.assertIn("down", out["error"])
        self.assertEqual(out["phones"], [])

    def test_unconfigured_is_not_an_exception(self):
        with patch("adb_bot.clients.geelark.GeelarkTransport") as transport:
            transport.return_value.is_configured = False
            out = report.geelark_status()
        self.assertFalse(out["configured"])
        self.assertIn("GEELARK_APP_ID", out["error"])


class GeelarkTabWiringTest(unittest.TestCase):
    """The tab is CSS-only state, so a missing selector shows a blank panel
    rather than an error -- which is exactly the kind of break nobody notices.
    """

    def _page(self):
        import tests.test_report as tr
        data = tr.RenderTest._data(tr.RenderTest())
        data["geelark"] = _geelark(phones=[_phone()])
        return report_html.render(data)

    def test_the_tab_is_selectable_and_has_a_panel(self):
        page = self._page()
        for needle in ('id="tab-geelark"', 'for="tab-geelark"', 'id="panel-geelark"'):
            self.assertIn(needle, page)

    def test_every_css_list_includes_the_new_tab(self):
        """Three separate selector lists control display, the active underline
        and the focus ring. Two existing tabs are already missing from two of
        them, so this is a live trap rather than a hypothetical one."""
        page = self._page()
        self.assertIn("#tab-geelark:checked ~ #panel-geelark", page)
        self.assertIn('#tab-geelark:checked ~ .tabs label[for="tab-geelark"]', page)
        self.assertIn('#tab-geelark:focus-visible ~ .tabs label[for="tab-geelark"]', page)

    def test_geelark_phones_are_not_added_to_the_mlx_inventory(self):
        """The separation rule. Geelark phones have no Airtable row, no model
        and no posting history, so adding them to any MultiLogin tally would
        overstate the fleet -- quietly, since nothing else would look wrong.

        Asserts the MultiLogin panel is byte-identical with and without Geelark
        data, rather than counting a substring that could drift.
        """
        import tests.test_report as tr

        def _mlx_panel(page: str) -> str:
            start = page.index('<section class="panel" id="panel-profiles">')
            return page[start:page.index("</section>", start)]

        data = tr.RenderTest._data(tr.RenderTest())
        without = _mlx_panel(report_html.render(data))
        data["geelark"] = _geelark(phones=[_phone(), _phone(name="two")])
        with_geelark = _mlx_panel(report_html.render(data))

        self.assertEqual(without, with_geelark,
                         "the Geelark tab changed the MultiLogin phone inventory")


if __name__ == "__main__":
    unittest.main()
