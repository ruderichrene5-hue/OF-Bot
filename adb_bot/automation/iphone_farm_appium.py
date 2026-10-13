"""Appium sessions for the iPhone farm (Rene, 13.10: "Appium auf jeden Fall mit einbauen").

Appium 3 + the XCUITest driver run ON THE SERVER (adbbot-appium.service, 127.0.0.1:4723, APPIUM_HOME
/opt/appium/home). Appium does not build, install or start WebDriverAgent: every phone already has its
own WDA runner on the Mac mini, and adbbot-vnc-tunnel brings each phone's :8100 here as
127.0.0.1:<wda_port> (see iphone_farm_wda._relay_host). A session therefore only needs
`appium:webDriverAgentUrl` -- the driver skips the device/usbmuxd checks when that is set.

Why not on the Mac: a node process there gets no "Local Network" permission from macOS (connect
EHOSTUNREACH 192.168.1.x:8100), the same trap as every launchd forward. On the server it is a
plain localhost connection.

    .venv/bin/python -m adb_bot.automation.iphone_farm_appium status            # every phone
    .venv/bin/python -m adb_bot.automation.iphone_farm_appium source iphone2    # element tree (XML)
    .venv/bin/python -m adb_bot.automation.iphone_farm_appium shot iphone2 /tmp/b.png

Taps stay with XXTouch inside Instagram (Rene: never WDA taps there); Appium is the reading side and
the standard client API (Python/Java/JS) for whoever writes flows next.
"""
from __future__ import annotations

import argparse
import sys
import time

from adb_bot.automation import iphone_farm_config
from adb_bot.automation.iphone_farm_wda import _relay_host, phones, resolve

APPIUM_URL = "http://127.0.0.1:4723"


def options(ref: str, new_command_timeout: int = 300):
    """XCUITest capabilities for one phone, pointed at its tunnelled WDA."""
    from appium.options.ios import XCUITestOptions

    rec_id, p = resolve(ref)
    if not p.get("wda_port"):
        raise RuntimeError(f"{ref}: no wda_port -- run iphone_farm_wda setup")
    o = XCUITestOptions()
    caps = {
        "appium:udid": p.get("udid") or rec_id,
        "appium:deviceName": p.get("name") or p.get("stream") or ref,
        "appium:webDriverAgentUrl": f"http://{_relay_host()}:{p['wda_port']}",
        "appium:usePrebuiltWDA": True,
        "appium:skipServerInstallation": True,
        # without platformVersion the driver asks the device for its iOS version over usbmuxd, which the
        # server does not have -> "usbmuxd socket path does not exist"
        "appium:platformVersion": p.get("ios") or ("15.8" if p.get("model") == "iPhone 7" else "16.7"),
        "appium:noReset": True,                     # never reinstall / reset the app under test
        "appium:wdaConnectionTimeout": 60000,
        "appium:newCommandTimeout": new_command_timeout,
        "appium:shouldTerminateApp": False,
    }
    for k, v in caps.items():
        o.set_capability(k, v)
    return o


def session(ref: str, **kw):
    """A connected Appium driver for `ref` (stream id like "iphone2", letter, name or record id).
    Use as a context manager or call .quit()."""
    from appium import webdriver

    return webdriver.Remote(APPIUM_URL, options=options(ref, **kw))


def _status_line(rec_id: str, p: dict) -> str:
    name = p.get("name") or p.get("stream") or rec_id
    t = time.time()
    try:
        d = session(p.get("stream") or rec_id)
        try:
            # page_source is pure WDA; "mobile: activeAppInfo" & co. want usbmuxd, which the server has not
            import re
            m = re.search(r'XCUIElementTypeApplication[^>]*name="([^"]*)"', d.page_source)
            app = m.group(1) if m else "?"
        finally:
            d.quit()
        return f"{name:12} :{p['wda_port']}  OK   {app}  ({time.time() - t:.1f} s)"
    except Exception as exc:  # noqa: BLE001
        return f"{name:12} :{p.get('wda_port')}  down {str(exc).splitlines()[0][:120]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="open and close one session per phone")
    s = sub.add_parser("source", help="print the element tree"); s.add_argument("phone")
    s = sub.add_parser("shot", help="save a screenshot"); s.add_argument("phone"); s.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        for rec_id, p in phones().items():
            print(_status_line(rec_id, p))
        return 0
    d = session(a.phone)
    try:
        if a.cmd == "source":
            print(d.page_source)
        elif a.cmd == "shot":
            d.get_screenshot_as_file(a.out)
            print(a.out)
    finally:
        d.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
