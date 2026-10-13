"""WebDriverAgent (WDA) on the physical iPhones: the iOS counterpart of ADB +
uiautomator. Read the screen as an element tree, find elements by text, tap,
type, swipe, launch apps, screenshot -- over plain HTTP from this server.

    .venv/bin/python -m adb_bot.automation.iphone_farm_wda status
    .venv/bin/python -m adb_bot.automation.iphone_farm_wda source  iphone1
    .venv/bin/python -m adb_bot.automation.iphone_farm_wda tap     iphone1 "Settings"
    .venv/bin/python -m adb_bot.automation.iphone_farm_wda shot    iphone1 /tmp/x.png
    .venv/bin/python -m adb_bot.automation.iphone_farm_wda setup   [--dry-run]

    from adb_bot.automation.iphone_farm_wda import connect
    with connect("iphone1") as p:
        p.launch("com.burbn.instagram")
        p.tap_text("Profile", timeout=10)

How it is wired (the relay = the Mac the iPhones are USB-cabled to):
  phone --USB--> relay: `ios runwda` (go-ios) starts WDA on the phone through
  XCTest, one LaunchAgent per phone (com.veralta.wdaN); `iproxy` forwards the
  phone's :8100 to <relay tailnet ip>:<wda_port> (com.veralta.wda-fwdN), bound
  to the tailnet address only -- WDA has no authentication at all.

Per phone in /etc/adbbot/iphone_farm.json: "udid" (from `ios list` on the
relay) and "wda_port" (8100 + N, assigned by `setup`). Phones without a udid
are skipped.

Why the phones accept our WDA build: it is Appium's prebuilt runner plus the
XCTest frameworks from the Developer Disk Image, signed ad hoc with ldid.
installd refuses that ("No code signature found") unless the jailbreak tweak
AppSync Unified is installed -- which needs root (`prepare-phone`). So like
Crane and SSH, WDA only works while the palera1n jailbreak holds.
Requirements on the phone that were already true 2026-10-06: Developer Mode on
(palera1n needs it too), iOS 16.7 (Developer Disk Image 16.6 works).
"""

from __future__ import annotations

import argparse
import base64
import json
import plistlib
import shlex
import subprocess
import sys
import time

from adb_bot.automation import iphone_farm_config

WDA_BUNDLE = "com.facebook.WebDriverAgentRunner.xctrunner"
WDA_RELEASE = "v16.14.0"                     # appium/WebDriverAgent, prebuilt real-device runner
GO_IOS_URL = "https://github.com/danielpaulus/go-ios/releases/latest/download/go-ios-mac.zip"
APPSYNC_REPO = "https://lukezgd.github.io/repo/"
APPSYNC_PKG = "ai.akemi.appsyncunified"
BASE_PORT = 8100

SSH_KEY = iphone_farm_config.SSH_KEY
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8"]


class WDAError(RuntimeError):
    pass


# ---------------------------------------------------------------- client

class Phone:
    """One phone's WDA. Coordinates are iOS points (iPhone X/8 Plus: 375x812 /
    414x736), or fractions of the screen when every value is <= 1."""

    def __init__(self, base_url: str, timeout: float = 30):
        import requests
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        self.sid: str | None = None
        self._size: tuple[float, float] | None = None

    # -- plumbing
    def _req(self, method: str, path: str, body: dict | None = None, timeout: float | None = None,
             _retried: bool = False):
        import requests
        try:
            r = self.http.request(method, self.base + path, json=body, timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise WDAError(f"WDA not reachable at {self.base} ({exc.__class__.__name__})") from None
        try:
            data = r.json()
        except ValueError:
            raise WDAError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}") from None
        value = data.get("value")
        if r.status_code >= 400 or (isinstance(value, dict) and value.get("error")):
            err = value if isinstance(value, dict) else {}
            # WDA drops sessions now and then (seen mid warm-up 2026-10-06): open a new one, retry once
            if err.get("error") == "invalid session id" and self.sid and f"/session/{self.sid}" in path \
                    and not _retried and not getattr(self, "_renewing", False):
                old, self.sid = self.sid, None
                self._renewing = True
                try:
                    self.start_session()
                finally:
                    self._renewing = False
                if "/element/" in path:
                    # element ids die with their session -- the caller has to look the element up again
                    raise WDAError(f"{method} {path}: stale element (WDA session was renewed)")
                return self._req(method, path.replace(f"/session/{old}", f"/session/{self.sid}"), body, timeout,
                                 _retried=True)
            raise WDAError(f"{method} {path}: {err.get('error', r.status_code)}: {str(err.get('message', ''))[:300]}")
        return value

    def _s(self, path: str) -> str:
        if not self.sid:
            self.start_session()
        return f"/session/{self.sid}{path}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.sid:
            try:
                self._req("DELETE", f"/session/{self.sid}", timeout=10)
            except WDAError:
                pass
            self.sid = None

    # -- session / device
    def status(self) -> dict:
        return self._req("GET", "/status", timeout=8)

    def start_session(self, bundle_id: str | None = None) -> str:
        caps = {"shouldWaitForQuiescence": False}
        if bundle_id:
            caps["bundleId"] = bundle_id
        v = self._req("POST", "/session", {"capabilities": {"alwaysMatch": caps}}, timeout=60)
        self.sid = v.get("sessionId") or (v.get("capabilities") or {}).get("sessionId")
        if not self.sid:
            raise WDAError(f"no session id in {v}")
        # don't wait for animations to settle before every query: the feed never settles
        self._req("POST", self._s("/appium/settings"),
                  {"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}})
        return self.sid

    def size(self) -> tuple[float, float]:
        if not self._size:
            v = self._req("GET", self._s("/window/size"))
            self._size = (float(v["width"]), float(v["height"]))
        return self._size

    def _pt(self, x: float, y: float) -> tuple[float, float]:
        if 0 <= x <= 1 and 0 <= y <= 1:
            w, h = self.size()
            return x * w, y * h
        return x, y

    def locked(self) -> bool:
        return bool(self._req("GET", "/wda/locked"))

    def unlock(self):
        """Wakes the screen and swipes past the lock screen (no passcode on the farm phones)."""
        self._req("POST", "/wda/unlock")

    def lock(self):
        self._req("POST", "/wda/lock")

    def home(self):
        self._req("POST", "/wda/homescreen")

    def press(self, button: str):
        """home | volumeUp | volumeDown"""
        self._req("POST", self._s("/wda/pressButton"), {"name": button})

    def screenshot(self) -> bytes:
        return base64.b64decode(self._req("GET", "/screenshot", timeout=30))

    def source(self, fmt: str = "xml") -> str:
        """The current screen as an element tree (like `uiautomator dump`).
        fmt: xml | description (compact text) | json"""
        v = self._req("GET", self._s(f"/source?format={fmt}"), timeout=60)
        return v if isinstance(v, str) else json.dumps(v, indent=1)

    def active_app(self) -> dict:
        return self._req("GET", "/wda/activeAppInfo")

    # -- apps
    def launch(self, bundle_id: str, args: list[str] | None = None):
        self._req("POST", self._s("/wda/apps/launch"), {"bundleId": bundle_id, "arguments": args or []}, timeout=60)

    def activate(self, bundle_id: str):
        self._req("POST", self._s("/wda/apps/activate"), {"bundleId": bundle_id}, timeout=60)

    def terminate(self, bundle_id: str) -> bool:
        return bool(self._req("POST", self._s("/wda/apps/terminate"), {"bundleId": bundle_id}))

    def app_state(self, bundle_id: str) -> int:
        """1 not running, 2 suspended, 3 background, 4 foreground"""
        return int(self._req("POST", self._s("/wda/apps/state"), {"bundleId": bundle_id}))

    def open_url(self, url: str):
        self._req("POST", self._s("/url"), {"url": url})

    # -- gestures
    def tap(self, x: float, y: float):
        x, y = self._pt(x, y)
        self._req("POST", self._s("/wda/tap"), {"x": x, "y": y})

    def double_tap(self, x: float, y: float):
        x, y = self._pt(x, y)
        self._req("POST", self._s("/wda/doubleTap"), {"x": x, "y": y})

    def long_press(self, x: float, y: float, seconds: float = 1.0):
        x, y = self._pt(x, y)
        self._req("POST", self._s("/wda/touchAndHold"), {"x": x, "y": y, "duration": seconds})

    def swipe(self, x1: float, y1: float, x2: float, y2: float, seconds: float = 0.3):
        (x1, y1), (x2, y2) = self._pt(x1, y1), self._pt(x2, y2)
        self._req("POST", self._s("/wda/dragfromtoforduration"),
                  {"fromX": x1, "fromY": y1, "toX": x2, "toY": y2, "duration": seconds})

    def type(self, text: str):
        """Type into whatever has keyboard focus."""
        self._req("POST", self._s("/wda/keys"), {"value": list(text)}, timeout=60)

    # -- elements
    def find_all(self, using: str, value: str) -> list["Element"]:
        """using: "predicate string" | "class chain" | "accessibility id" | "class name" | "xpath"
        (xpath is slow on big screens -- prefer predicates)."""
        v = self._req("POST", self._s("/elements"), {"using": using, "value": value})
        return [Element(self, next(iter(e.values()))) for e in v]

    def find(self, using: str, value: str) -> "Element | None":
        found = self.find_all(using, value)
        return found[0] if found else None

    def by_text(self, text: str, exact: bool = True) -> list["Element"]:
        """Elements whose label, name or value is `text` (exact) or contains it (case-insensitive)."""
        q = json.dumps(text)
        op = "==" if exact else "CONTAINS[c]"
        return self.find_all("predicate string", f"label {op} {q} OR name {op} {q} OR value {op} {q}")

    def wait_text(self, text: str, timeout: float = 10, exact: bool = True) -> "Element | None":
        end = time.monotonic() + timeout
        while True:
            hits = [e for e in self.by_text(text, exact) if e.visible()]
            if hits:
                return hits[0]
            if time.monotonic() >= end:
                return None
            time.sleep(0.5)

    def tap_text(self, text: str, timeout: float = 0, exact: bool = True) -> bool:
        """Tap the first visible element showing `text`. False when none appeared in time."""
        el = self.wait_text(text, timeout, exact)
        if el is None:
            return False
        el.click()
        return True

    # -- system alerts ("Allow notifications?", "Allow Photos access?")
    def alert_text(self) -> str | None:
        try:
            return self._req("GET", self._s("/alert/text"))
        except WDAError:
            return None

    def alert_buttons(self) -> list[str]:
        try:
            return self._req("GET", self._s("/wda/alert/buttons")) or []
        except WDAError:
            return []

    def alert_accept(self, button: str | None = None):
        self._req("POST", self._s("/alert/accept"), {"name": button} if button else {})

    def alert_dismiss(self, button: str | None = None):
        self._req("POST", self._s("/alert/dismiss"), {"name": button} if button else {})


class Element:
    def __init__(self, phone: Phone, eid: str):
        self.phone, self.id = phone, eid

    def _p(self, suffix: str) -> str:
        return self.phone._s(f"/element/{self.id}{suffix}")

    def click(self):
        self.phone._req("POST", self._p("/click"))

    def type(self, text: str):
        self.phone._req("POST", self._p("/value"), {"value": list(text)}, timeout=60)

    def clear(self):
        self.phone._req("POST", self._p("/clear"))

    def text(self) -> str:
        return self.phone._req("GET", self._p("/text")) or ""

    def attr(self, name: str):
        return self.phone._req("GET", self._p(f"/attribute/{name}"))

    def visible(self) -> bool:
        return bool(self.attr("visible"))

    def rect(self) -> dict:
        return self.phone._req("GET", self._p("/rect"))

    def __repr__(self):
        return f"<Element {self.id}>"


def _relay_host() -> str:
    """Where WDA's <wda_port> answers. `_relay.wda_tunnel: true` (13.10): the ports are brought here by
    adbbot-vnc-tunnel.service (ssh -L 127.0.0.1:810N:<phone lan ip>:8100), because a launchd agent on
    the Mac (iproxy/socat) gets no "Local Network" permission from macOS -- "No route to host"."""
    relay = iphone_farm_config.relay()
    if relay.get("wda_tunnel"):
        return "127.0.0.1"
    return (relay.get("ssh") or "").split("@")[-1]


def phones() -> dict:
    """Device record id -> phone config, for phones with a udid (WDA-capable)."""
    return {k: v for k, v in iphone_farm_config.load().items() if v.get("udid")}


def resolve(ref: str) -> tuple[str, dict]:
    """A phone by Device record id, stream ("iphone1"), udid (prefix) or ssh alias ("phone-c")."""
    for rec_id, p in phones().items():
        if ref in (rec_id, p.get("stream"), p.get("ssh"), p.get("ip"), p.get("name")) or p["udid"].startswith(ref):
            return rec_id, p
    raise WDAError(f"no WDA phone {ref!r} in {iphone_farm_config.CONFIG_PATH} (needs a udid)")


def connect(ref: str, timeout: float = 30) -> Phone:
    rec_id, p = resolve(ref)
    if not p.get("wda_port"):
        raise WDAError(f"{ref}: no wda_port yet -- run `iphone_farm_wda setup`")
    ph = Phone(f"http://{_relay_host()}:{p['wda_port']}", timeout=timeout)
    ph.ref = p.get("stream") or ref                       # lets iphone_ig.Human find the phone's XXTouch
    return ph


def dismiss_xxtouch_dialog(p: Phone) -> bool:
    """XXTouch Elite's own alerts sit on top of the foreground app (active app reads as
    springboard): "License Restricted / E430", and -- found live 11.10, iPhone D, after a warmup
    swipe -- a Lua "Runtime Error" ("Coordinate (x, y) exceeds coordinate space") when a tap/swipe
    lands a hair past the screen edge. Both leave a stuck dialog with no Shortcut progress behind
    it; this one sat through TWO full 240 s timeouts (D2, D3) before anything dismissed it. Shared
    by iphone_signup and iphone_ig -- moved here (11.10) so both use the one tested copy."""
    try:
        if p.find("predicate string",
                   "(label CONTAINS 'License Restricted' OR label CONTAINS 'Runtime Error') AND visible == 1") is None:
            return False
        b = p.find("predicate string", "type == 'XCUIElementTypeButton' AND label == 'Dismiss' AND visible == 1")
        if b is None:
            return False
        b.click()
        time.sleep(1)
        return True
    except WDAError:
        return False


def wait_for_foreground(p: Phone, bundle_id: str, container: str, seconds: float = 240, log=None) -> bool:
    """Wait until `bundle_id` is in front, tapping through the container Shortcut's own one-time
    prompts along the way (SSH-to-self, Shadowrocket, ipify/ip-api, notifications) -- iOS asks them
    on every run, no way to pre-grant (10-04). Shared by signup (open_fresh_container) and
    warmup/post/picture/pfp (open_account, iphone_ig.py) -- the latter used to just poll
    activeAppInfo with no prompt handling, so it timed out doing nothing on every single real
    farm phone (11.10). Returns True once `bundle_id` is the foreground app."""
    log = log or (lambda *a, **k: None)
    deadline = time.monotonic() + seconds
    allowed = 0
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            if dismiss_xxtouch_dialog(p):
                continue
            app = p.active_app().get("bundleId")
            if app == bundle_id:
                return True
            if app == "com.apple.springboard":
                # 12.10 live, C13/C14 (containers used for the first time ever): the Shortcut's
                # own one-time "Allow '<container>' to run an SSH script on 127.0.0.1?" prompt
                # sometimes attributes to springboard instead of com.apple.shortcuts/ShortcutsUI
                # (an iOS quirk -- the system alert itself renders under springboard regardless
                # of which app triggered it), so the generic "decline everything" rule below kept
                # clicking Don't Allow on it every ~9 s, forever -- a brand-new container never
                # got past it (confirmed: both C13 and C14 looped here for all 4 launches, the
                # VPN's active container never moving off the previous one). Needs the SAME
                # Allow this prompt gets below when it DOES arrive as com.apple.shortcuts.
                one_time_prompt = p.find("predicate string",
                                          "type == 'XCUIElementTypeStaticText' AND (label CONTAINS 'run an SSH script' "
                                          "OR label CONTAINS 'run a shell script' OR label CONTAINS 'api.ipify.org' "
                                          "OR label CONTAINS 'ip-api.com') AND visible == 1")
                if one_time_prompt is not None:
                    allow = p.find("predicate string", "type == 'XCUIElementTypeButton' AND label == 'Allow' AND visible == 1")
                    if allow is not None:
                        allow.click()
                        allowed += 1
                        log("shortcut %s: one-time permission prompt (via springboard) -> Allow (%d)" % (container, allowed))
                        time.sleep(1.5)
                        continue
                # some other app's own first-launch permission ask firing at the wrong moment,
                # not one of the Shortcut's own prompts (those arrive as com.apple.shortcuts) --
                # same "decline everything but the Shortcut's two required permissions" rule.
                btn = p.find("predicate string",
                             "type == 'XCUIElementTypeButton' AND (label == 'Cancel' OR label CONTAINS 'Don’t Allow') AND visible == 1")
                if btn is not None:
                    btn.click()
                    allowed += 1
                    log("shortcut %s: springboard alert dismissed (Cancel/Don't Allow) (%d)" % (container, allowed))
                    time.sleep(1.5)
                continue
            if app in ("com.apple.ShortcutsUI", "com.apple.shortcuts") and allowed < 8:
                conn = p.find("predicate string", "type == 'XCUIElementTypeButton' AND label == 'Connect' AND visible == 1")
                if conn is not None:
                    conn.click()
                    allowed += 1
                    log("shortcut %s: Unknown Host 127.0.0.1 -> Connect (%d)" % (container, allowed))
                    time.sleep(1.5)
                    continue
                btn = p.find("predicate string", "type == 'XCUIElementTypeButton' AND label == 'Allow' AND visible == 1")
                if btn is not None:
                    # Rene 08.10: "immer ablehnen" -- deny everything except the two permissions the
                    # Shortcut needs to work at all; these prompts are the farm Shortcut's OWN
                    # permissions (SSH, 127.0.0.1, Shadowrocket, ipify, its notification), denying
                    # any of them aborts the Shortcut.
                    btn.click()
                    allowed += 1
                    log("shortcut %s: Allow prompt (%d)" % (container, allowed))
                    time.sleep(1.5)
        except WDAError:
            pass
    return False


# ---------------------------------------------------------------- relay (the Mac) setup

def _ssh(target: str, cmd: str, stdin: bytes | None = None, timeout: int = 120) -> str:
    r = subprocess.run(["ssh", "-i", SSH_KEY, *SSH_OPTS, target, cmd], input=stdin,
                       capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise WDAError(f"ssh {target}: {(r.stderr or r.stdout).decode(errors='replace').strip()[-400:]}")
    return r.stdout.decode(errors="replace")


# Runs on the relay. Builds ~/farm/wda/build/WDA.ipa: the prebuilt runner + XCTest frameworks out of
# the Developer Disk Image (Xcode would embed the same ones), every Mach-O signed ad hoc with ldid.
BUILD_SH = r"""#!/bin/bash
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
cd ~/farm/wda
DDI=$(ls -d ddi/*/DeveloperDiskImage.dmg | sort -V | tail -1)
rm -rf build && mkdir -p build/Payload
cp -R WebDriverAgentRunner-Runner.app build/Payload/
APP=build/Payload/WebDriverAgentRunner-Runner.app
MNT=$(mktemp -d)
hdiutil attach -readonly -nobrowse -mountpoint "$MNT" "$DDI" >/dev/null
trap 'hdiutil detach "$MNT" >/dev/null 2>&1 || true' EXIT
mkdir -p $APP/Frameworks
for f in Frameworks/XCTest.framework PrivateFrameworks/XCTestCore.framework PrivateFrameworks/XCUnit.framework \
         PrivateFrameworks/XCUIAutomation.framework PrivateFrameworks/XCTAutomationSupport.framework \
         PrivateFrameworks/XCTestSupport.framework; do
  cp -R "$MNT/Library/$f" $APP/Frameworks/
done
cp "$MNT"/usr/lib/libXCTestSwiftSupport.dylib "$MNT"/usr/lib/libXCTestBundleInject.dylib $APP/Frameworks/
cat > build/ent.plist <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>application-identifier</key><string>WDA.com.facebook.WebDriverAgentRunner.xctrunner</string>
<key>com.apple.developer.team-identifier</key><string>WDA</string>
<key>get-task-allow</key><true/>
</dict></plist>
P
rm -rf $APP/_CodeSignature $APP/PlugIns/*/_CodeSignature
# the XCTest frameworks keep Apple's signature: their hashes are in the developer image's trust cache.
# Re-signed with ldid they get new hashes and the runner dies at load (dyld exit code 9).
find $APP/PlugIns -name "*.dylib" -exec ldid -S {} \;
find $APP/PlugIns -name "*.framework" | while read fw; do
  n=$(basename "$fw" .framework); [ -f "$fw/$n" ] && ldid -S "$fw/$n"; done
ldid -S $APP/PlugIns/WebDriverAgentRunner.xctest/WebDriverAgentRunner
ldid -Sbuild/ent.plist $APP/WebDriverAgentRunner-Runner
(cd build && zip -qr WDA.ipa Payload)
echo built build/WDA.ipa
"""

# Runs on the relay, once per phone start: developer image (lost on every phone reboot), then WDA.
# go-ios injects /Developer/usr/lib/libMainThreadChecker.dylib, which the 16.6 developer image does
# not have (crash report: DYLD code 9 "no such file") -- inject the image's XCTestBundleInject instead.
RUN_SH = r"""#!/bin/bash
cd ~/farm/wda
./ios image auto --basedir="$HOME/farm/wda/ddi" --udid="$1" >/dev/null 2>&1
exec ./ios runwda --bundleid=%(b)s --testrunnerbundleid=%(b)s --xctestconfig=WebDriverAgentRunner.xctest \
  --env=DYLD_INSERT_LIBRARIES=/Developer/usr/lib/libXCTestBundleInject.dylib --udid="$1"
""" % {"b": WDA_BUNDLE}


def bootstrap_host(target: str) -> None:
    """One-time tools on the relay Mac: go-ios, ldid, the WDA runner, the developer image."""
    _ssh(target, f"""set -e; export PATH=/opt/homebrew/bin:$PATH; mkdir -p ~/farm/wda ~/farm/logs; cd ~/farm/wda
[ -x ios ] || {{ curl -fsSL -o go-ios.zip {GO_IOS_URL}; unzip -o -q go-ios.zip ios; xattr -d com.apple.quarantine ios 2>/dev/null || true; }}
# ldid drags in a newer libplist; relink the libimobiledevice tools (iproxy!) right after, or they stop starting
command -v ldid >/dev/null || {{ brew install ldid libimobiledevice >/dev/null; brew upgrade libusbmuxd libimobiledevice libimobiledevice-glue >/dev/null 2>&1 || true; }}
command -v iproxy >/dev/null || brew install libimobiledevice >/dev/null
[ -d WebDriverAgentRunner-Runner.app ] || {{ curl -fsSL -o wda.zip https://github.com/appium/WebDriverAgent/releases/download/{WDA_RELEASE}/WebDriverAgentRunner-Runner.zip; unzip -o -q wda.zip; }}
U=$(./ios list 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["deviceList"][0])' 2>/dev/null || true)
ls ddi/*/DeveloperDiskImage.dmg >/dev/null 2>&1 || {{ [ -n "$U" ] && ./ios image auto --basedir=$HOME/farm/wda/ddi --udid=$U >/dev/null 2>&1; }}
ls ddi/*/DeveloperDiskImage.dmg >/dev/null""", timeout=900)
    _ssh(target, "cat > ~/farm/wda/build_wda.sh && chmod +x ~/farm/wda/build_wda.sh", stdin=BUILD_SH.encode())
    _ssh(target, "cat > ~/farm/wda/run-wda.sh && chmod +x ~/farm/wda/run-wda.sh", stdin=RUN_SH.encode())
    # rebuild when the build recipe changed since the last WDA.ipa
    _ssh(target, "cd ~/farm/wda && { [ build/WDA.ipa -nt build_wda.sh ] || ./build_wda.sh; }", timeout=300)


def _agent(label: str, args: list[str], home: str) -> bytes:
    return plistlib.dumps({
        "Label": label, "ProgramArguments": args,
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"},
        "StandardOutPath": f"{home}/farm/logs/{label}.log", "StandardErrorPath": f"{home}/farm/logs/{label}.log",
        # an unplugged phone makes these exit at once; retry every 20 s instead of spinning
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 20,
    })


def installed(target: str, udid: str) -> bool:
    out = _ssh(target, f"cd ~/farm/wda && ./ios apps --list --udid={shlex.quote(udid)} 2>/dev/null || true")
    return WDA_BUNDLE in out


def install(target: str, udid: str) -> str:
    out = subprocess.run(["ssh", "-i", SSH_KEY, *SSH_OPTS, target,
                          f"cd ~/farm/wda && ./ios install --path=build/WDA.ipa --udid={shlex.quote(udid)} 2>&1 | tail -1"],
                         capture_output=True, text=True, timeout=300).stdout
    if "InstallationProhibited" in out:
        # seen on iPhone 2 (2026-10-06): app installs are switched off on the phone itself
        return ("REFUSED -- installing apps is blocked on the phone (Screen Time > Content & Privacy > "
                "iTunes & App Store Purchases > Installing Apps, or a profile)")
    if "not properly signed" in out or "No code signature" in out:
        return "REFUSED -- AppSync Unified missing on the phone (run prepare-phone)"
    if "ERROR" in out.upper():
        return "FAILED -- " + out.strip()[-300:]
    return "installed"


def setup(dry_run: bool = False, bootstrap: bool = True) -> list[str]:
    """Make the relay match the config: tools, a wda_port per phone, the two LaunchAgents per
    phone, WDA installed where possible. Returns a log of what it did."""
    raw = iphone_farm_config._raw()
    relay = raw.get("_relay") or {}
    target = relay.get("ssh")
    if not target or relay.get("platform", "macos") != "macos":
        raise SystemExit("_relay.ssh must name the macOS relay (Linux relay not implemented)")
    host = target.split("@")[-1]
    log: list[str] = []
    if bootstrap and not dry_run:
        bootstrap_host(target)
        log.append("relay tools ok (go-ios, ldid, WDA.ipa, developer image)")

    wda = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict) and v.get("udid")}
    used = {v["wda_port"] for v in wda.values() if v.get("wda_port")}
    for rec_id, p in sorted(wda.items(), key=lambda kv: str(kv[1].get("stream", "zz"))):
        if not p.get("wda_port"):
            n = int(str(p.get("stream", "")).removeprefix("iphone") or 0) if str(p.get("stream", "")).startswith("iphone") else 0
            port = BASE_PORT + n if n and BASE_PORT + n not in used else next(
                BASE_PORT + i for i in range(1, 200) if BASE_PORT + i not in used)
            p["wda_port"] = port
            used.add(port)
            log.append(f"{rec_id}: wda_port {port}")
    if not dry_run:
        with open(iphone_farm_config.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)

    home = _ssh(target, "echo $HOME").strip()
    files = {}
    for rec_id, p in wda.items():
        n = p["wda_port"] - BASE_PORT
        files[f"{home}/Library/LaunchAgents/com.veralta.wda{n}.plist"] = _agent(
            f"com.veralta.wda{n}", [f"{home}/farm/wda/run-wda.sh", p["udid"]], home)
        # bound to the tailnet address only: WDA answers anyone who can reach it
        if p.get("ip"):
            # 13.10: phones talk over WiFi since 12.10; the usbmuxd forward (iproxy) to :8100 has been
            # dead since then (connects, then closes at once) while the LAN port answers in <1 s.
            # socat relays tailnet:<wda_port> -> <lan ip>:8100 exactly like the SSH aliases do.
            files[f"{home}/Library/LaunchAgents/com.veralta.wda-fwd{n}.plist"] = _agent(
                f"com.veralta.wda-fwd{n}", ["/opt/homebrew/bin/socat", f"TCP-LISTEN:{p['wda_port']},bind={host},fork,reuseaddr",
                                            f"TCP:{p['ip']}:8100"], home)
        else:
            files[f"{home}/Library/LaunchAgents/com.veralta.wda-fwd{n}.plist"] = _agent(
                f"com.veralta.wda-fwd{n}", ["/opt/homebrew/bin/iproxy", "-u", p["udid"], "-s", host,
                                            f"{p['wda_port']}:8100"], home)
    changed = []
    for path, content in files.items():
        current = subprocess.run(["ssh", "-i", SSH_KEY, *SSH_OPTS, target, f"cat {shlex.quote(path)} 2>/dev/null"],
                                 capture_output=True, timeout=30).stdout
        if current != content:
            changed.append(path)
    log += [f"agent {'would change' if dry_run else 'changed'}: {c.rsplit('/', 1)[1]}" for c in changed]
    if dry_run:
        return log

    for rec_id, p in wda.items():
        if not installed(target, p["udid"]):
            log.append(f"{p.get('stream') or rec_id}: {install(target, p['udid'])}")

    for path in changed:
        _ssh(target, f"mkdir -p $(dirname {shlex.quote(path)}) && cat > {shlex.quote(path)}", stdin=files[path])
    script = "U=$(id -u); "
    for path in changed:
        label = path.rsplit("/", 1)[1].removesuffix(".plist")
        script += (f"launchctl bootout gui/$U/{label} 2>/dev/null; "
                   f"for i in 1 2 3 4 5 6; do sleep 1; launchctl bootstrap gui/$U {shlex.quote(path)} 2>/dev/null && break; done; ")
    for path in set(files) - set(changed):          # unchanged but not loaded (e.g. booted out by hand)
        label = path.rsplit("/", 1)[1].removesuffix(".plist")
        script += f"launchctl print gui/$U/{label} >/dev/null 2>&1 || launchctl bootstrap gui/$U {shlex.quote(path)}; "
    _ssh(target, script, timeout=120)
    return log


def prepare_phone(ref: str, sudo_password: str) -> str:
    """Install AppSync Unified on one phone (root via sudo), so installd accepts our ad-hoc
    signed WDA. Needs the `mobile` password set in Sileo after the jailbreak."""
    rec_id, p = resolve(ref)
    dest = p.get("ssh") or f"mobile@{p['ip']}"
    # the password comes in on stdin, so it never shows up in a process list
    cmd = (f"export PATH=/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:$PATH; read -r PW; "
           f"S() {{ printf '%s\\n' \"$PW\" | sudo -S -p '' \"$@\"; }}; S true </dev/null || exit 3; "
           f"dpkg -s {APPSYNC_PKG} >/dev/null 2>&1 && {{ echo already; exit 0; }}; "
           # the repo is unsigned, hence Trusted (only for this source).
           # Not `printf | S tee`: S feeds the password to sudo's stdin, which tee would then write out
           f"S sh -c \"printf 'Types: deb\\\\nURIs: {APPSYNC_REPO}\\\\nSuites: ./\\\\nComponents: \\\\nTrusted: yes\\\\n' "
           f"> /var/jb/etc/apt/sources.list.d/appsync.sources\"; "
           f"S apt-get update -qq >/dev/null 2>&1; S apt-get install -y -qq {APPSYNC_PKG} 2>&1 | tail -2; "
           f"S killall -9 installd 2>/dev/null; dpkg -s {APPSYNC_PKG} | grep -i '^version'")
    r = subprocess.run(["ssh", "-i", SSH_KEY, *SSH_OPTS, dest, cmd], input=sudo_password + "\n",
                       capture_output=True, text=True, timeout=300)
    if r.returncode == 3:
        return "sudo password rejected"
    return (r.stdout + r.stderr).strip()[-400:]


# ---------------------------------------------------------------- CLI

def _status_line(rec_id: str, p: dict) -> str:
    name = p.get("stream") or p.get("ssh") or rec_id
    if not p.get("wda_port"):
        return f"{name:10} udid {p['udid'][:8]}  no port yet (run setup)"
    try:
        s = connect(rec_id, timeout=6).status()
        ios = (s.get("os") or {}).get("version", "?")
        return f"{name:10} :{p['wda_port']}  READY  iOS {ios}  {s.get('build', {}).get('version', '')}"
    except WDAError as exc:
        return f"{name:10} :{p['wda_port']}  down   {exc}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="which phones answer")
    s = sub.add_parser("setup", help="install/refresh WDA + agents on the relay from the config")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-bootstrap", action="store_true", help="skip the tool check on the relay")
    s = sub.add_parser("prepare-phone", help="install AppSync Unified (needs the phone's sudo password)")
    s.add_argument("phone")
    s.add_argument("--password", help="default: $IPHONE_SUDO_PASSWORD or asked interactively")
    for name, extra in (("source", None), ("tap", "text"), ("shot", "out"), ("launch", "bundle"), ("home", None)):
        s = sub.add_parser(name)
        s.add_argument("phone")
        if extra:
            s.add_argument(extra)
    a = ap.parse_args(argv)

    if a.cmd == "status":
        for rec_id, p in phones().items():
            print(_status_line(rec_id, p))
        return 0
    if a.cmd == "setup":
        print("\n".join(setup(dry_run=a.dry_run, bootstrap=not a.no_bootstrap)) or "nothing to do")
        return 0
    if a.cmd == "prepare-phone":
        import getpass
        import os
        pw = a.password or os.environ.get("IPHONE_SUDO_PASSWORD") or getpass.getpass("sudo password of mobile: ")
        print(prepare_phone(a.phone, pw))
        return 0
    with connect(a.phone) as p:
        if a.cmd == "source":
            print(p.source())
        elif a.cmd == "tap":
            print("tapped" if p.tap_text(a.text, timeout=5) else f"no visible element {a.text!r}")
        elif a.cmd == "shot":
            with open(a.out, "wb") as f:
                f.write(p.screenshot())
            print(a.out)
        elif a.cmd == "launch":
            p.launch(a.bundle)
        elif a.cmd == "home":
            p.home()
    return 0


if __name__ == "__main__":
    sys.exit(main())
