"""Instagram flows on the iPhones WITHOUT WebDriverAgent (Rene 09.10: "alles mit XXTouch").

WDA showed "Automation Running" on the phone and is a test tool an app could notice. Here everything
runs through XXTouch on the phone itself:
  * reading the screen: screen.ocr_text (Apple Vision) -> words with their position
  * touching: the phone's own recorded thumb (iphone_xxtouch.Touch: tap/swipe/hold/drag)
  * typing: sys.input_text; apps: app.front_bid / app.run; media: image.video_to_album
Container + proxy are switched by the phone's own Shortcut (run over SSH), which also checks IP/DNS
and only then opens Instagram.

Coordinates: XXTouch OCR returns pixels, Touch takes points -> divided by the screen scale (3 on X).
"""
from __future__ import annotations

import json
import random
import re
import shlex
import subprocess
import time

from adb_bot.automation import iphone_farm_config, iphone_xxtouch
from adb_bot.automation.iphone_farm_containers import _dest, ssh

IG = "com.burbn.instagram"


class FlowError(RuntimeError):
    pass


class XXLicenceError(iphone_xxtouch.XXError):
    """XXTouch refuses every script: the Havoc licence on this phone is invalid, not a transient fault."""


class XX:
    def __init__(self, ref: str, log=print):
        self.ref, self.log = ref, log
        self.dest, self.phone = _dest(ref)
        # 12.10, G1/G10 live: iphone_xxtouch.library_for() expects the "iphoneN" stream name (it
        # regexes a trailing number off it to pick the phone's own recorded taps) -- called with
        # the raw Device record id instead (every caller here used to pass a plain "iphoneN" or
        # single-letter ref, until signup started resolving containers straight from Airtable and
        # passing the record id through unchanged), G's id happened to end in "24" and the regex
        # read that as phone 24, 'ABCDEFGHIJ'[23] -> IndexError. Resolve to the stream name first,
        # same as iphone_farm_wda.connect() already does for this exact purpose.
        self.t = iphone_xxtouch.for_phone(self.phone.get("stream") or ref)
        self.scale = self.t.scale
        # 11.10: XXTouch forgets its licence after a respring although XXTExplorer still has it
        # cached (same 403 "requires a valid licence" hit on iPhone E's signups today, fixed there
        # by opening XXTExplorer once via WDA). This class has no WDA of its own, and the fix can't
        # go through XXTouch's own /spawn (that is exactly what's 403ing), nor through plain
        # `uiopen <bundle-id>` (tried live, 11.10: exit 0 but did not actually bring XXTExplorer
        # to front). WDA directly is what's proven to work, so borrow it just for this.
        probe = self.t._post("/spawn", b"touch.init(0)\nsys.msleep(1)\n")
        if probe.get("code") == 403:
            self.log(f"{ref}: XXTouch licence restricted -- opening XXTExplorer via WDA to re-sync")
            try:
                from adb_bot.automation.iphone_farm_wda import connect as _wda_connect
                p = _wda_connect(ref, timeout=30)
                p.launch("ch.xxtou.XXTExplorer")
                time.sleep(8)
                if p.app_state("com.burbn.instagram") in (2, 3, 4):   # 12.10: never LAUNCH Instagram directly
                    p.activate("com.burbn.instagram")
                time.sleep(2)
            except Exception as exc:                          # noqa: BLE001
                self.log(f"{ref}: WDA re-sync failed ({exc})")
            # 22.10 (Rene: "was ist unsere Loesung, wenn die Lizenz mal nicht geht -- das ganze Posten bricht ab"):
            # re-probe. If the licence is really gone (Havoc login lost after a re-jailbreak, B/C 22.10) every tap
            # would 403 and the batch burnt 3 tries per account for nothing. Say so once, loudly, instead.
            probe = self.t._post("/spawn", b"touch.init(0)\nsys.msleep(1)\n")
            if probe.get("code") == 403:
                raise XXLicenceError(f"{ref}: XXTouch licence invalid (403 after re-sync) -- Havoc login in Sileo needed")

    # ---- plumbing
    def lua_out(self, body: str, timeout: float = 30) -> str:
        """Run Lua that sets the global `OUT` (string); return it."""
        path = f"/tmp/xxf_{random.randint(0, 1 << 30)}.txt"
        self.t.run(f'OUT = ""\n{body}\nlocal f = io.open("{path}", "w") f:write(tostring(OUT)) f:close() os.execute("chmod 666 {path}")')
        try:
            return ssh(self.dest, f"cat {path}; rm -f {path} 2>/dev/null; true", timeout=timeout).decode("utf-8", "replace")
        except Exception:                                    # noqa: BLE001
            return ""

    def front(self) -> str:
        return self.lua_out("OUT = app.front_bid()").strip()

    def ocr(self) -> list[dict]:
        """Every word/line on screen: {text, x, y, box} in POINTS (centre). [] when XXTouch is busy."""
        try:
            return self._ocr()
        except RuntimeError as exc:                          # "script still running after 30 s" while IG uploads
            self.log(f"ocr skipped: {exc}")
            time.sleep(3)
            return []

    def _ocr(self) -> list[dict]:
        raw = self.lua_out(
            'local ok, txt, items = pcall(screen.ocr_text)\n'
            'local rows = {}\n'
            'if ok and type(items) == "table" then for _, it in ipairs(items) do\n'
            '  local c, b = it.center or {}, it.bounding_box or {}\n'
            '  rows[#rows+1] = {t = it.recognized_text, x = c[1], y = c[2], b = b} end end\n'
            'OUT = json.encode(rows)')
        try:
            rows = json.loads(raw or "[]")
        except ValueError:
            return []
        s = self.scale
        return [{"text": str(r.get("t") or ""), "x": (r.get("x") or 0) / s, "y": (r.get("y") or 0) / s,
                 "box": [v / s for v in (r.get("b") or [])]} for r in rows if r.get("t")]

    def find(self, pattern: str, timeout: float = 0, region: tuple | None = None) -> dict | None:
        """First OCR hit whose text matches `pattern` (regex, case-insensitive)."""
        rx = re.compile(pattern, re.I)
        end = time.monotonic() + timeout
        while True:
            for w in self.ocr():
                if rx.search(w["text"]) and (not region or (region[0] <= w["x"] <= region[2] and region[1] <= w["y"] <= region[3])):
                    return w
            if time.monotonic() >= end:
                return None
            time.sleep(1.0)

    def tap_text(self, pattern: str, timeout: float = 8, region: tuple | None = None) -> bool:
        w = self.find(pattern, timeout, region)
        if not w:
            return False
        self.tap(w["x"], w["y"])
        return True

    # ---- gestures (points)
    def tap(self, x: float, y: float):
        self.t.tap(x + random.gauss(0, 1.2), y + random.gauss(0, 1.2))

    def swipe(self, x1, y1, x2, y2, seconds=None):
        self.t.swipe(x1, y1, x2, y2, seconds)

    def type(self, text: str):
        self.t.run(f"sys.input_text({json.dumps(text)})")

    @staticmethod
    def pause(lo: float, hi: float):
        time.sleep(lo + (hi - lo) * random.random() ** 1.6)

    def size(self) -> tuple[float, float]:
        raw = self.lua_out("local w, h = screen.size() OUT = w .. ',' .. h")
        try:
            w, h = (float(v) for v in raw.split(","))
            return w / self.scale, h / self.scale
        except ValueError:
            return 375.0, 812.0

    # ---- phone helpers
    def video_to_album(self, local_path: str) -> None:
        name = f"xxf_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
        remote = f"/var/mobile/Media/{name}"
        with open(local_path, "rb") as f:
            ssh(self.dest, f"cat > {shlex.quote(remote)}", data=f.read(), timeout=180)
        self.lua_out(f'local ok, err = pcall(image.video_to_album, "{remote}") OUT = tostring(ok) .. " " .. tostring(err)')
        ssh(self.dest, f"rm -f {shlex.quote(remote)}", timeout=30)

    _ALLOW_WORDS = ("allow", "erlauben")

    def _allow_ssh_script_prompt(self) -> bool:
        """12.10 live (A10, container used for the first time ever): the Shortcut asks several
        one-time permissions in a row the first time a container is ever opened -- "run an SSH
        script on 127.0.0.1", then a SEPARATE "connect to ..." dialog for every distinct host its
        IP-check step touches (api.ipify.org, ip-api.com, edns.ip-api.com, ... -- confirmed live:
        matching the exact domain name whack-a-mole style meant every new subdomain was its own
        4-launch, ~4-minute round trip) -- and nothing tapped ANY of them, so a brand-new
        container timed out every single launch while an already-granted one sailed straight
        through.

        12.10, B13: these are iOS system alerts, rendered in the PHONE's system language, not
        Instagram's (B's Instagram UI was English while its paste-permission dialog showed
        "Einsetzen erlauben" -- the fleet is not uniformly English). Matching specific English
        marker phrases is a losing whack-a-mole across hosts AND languages, so this only checks
        for a short, STANDALONE "Allow"/"Erlauben" button instead -- safe here because this runs
        only inside open_container's wait loop, strictly before Instagram itself is ever in the
        foreground, so any such button can only be one of the Shortcut's own required one-time
        grants, never an in-app permission ask. "Don't Allow"/"Nicht erlauben" normalize to a
        different, longer string and are never matched. iOS only asks each one once per
        container, so declining them (as the WDA path's old generic springboard handler used to)
        would permanently break that container instead of fixing anything."""
        words = self.ocr()
        allow = next((w for w in words if w["text"].strip().lower() in self._ALLOW_WORDS), None)
        if not allow:
            return False
        self.tap(allow["x"], allow["y"])
        self.log(f"allowed the container's one-time permission prompt ({allow['text'].strip()!r})")
        time.sleep(1.5)
        return True

    def open_container(self, container: str, timeout: float = 600) -> None:
        """The container's Shortcut: closes IG, switches Shadowrocket + Crane, checks IP/US/DNS, opens IG.
        12.10, Rene: a single passive wait is the wrong shape when the Shortcut itself never got going
        (USB/WLAN hiccup, iOS swallowed the uiopen) -- relaunch it again after a short look instead of
        sitting out the whole budget on one launch. Same ceiling overall, split into shorter looks.

        12.10 later, Rene live: a 60 s per-launch window was too short on a slow night -- checked C's
        screen right after a "failed" run gave up and Instagram was sitting there open and fine, just
        later than the cap allowed. Worse, relaunching every 60 s likely KILLED slow-but-progressing
        attempts outright (each relaunch restarts the Shortcut's own VPN-switch + IP/DNS check from
        scratch) rather than ever letting one finish. Patience over abort: fewer, much longer looks."""
        if not re.fullmatch(r"[A-Z]\d{1,3}", container):
            raise FlowError(f"bad container name {container!r}")
        if iphone_farm_config.relay().get("container_open") in ("cli", "cli-fast"):
            # 15.10, Rene: "ein paar Handys waren noch über Kurzbefehle" -- run_shortcut() honoured
            # `_relay.container_open: cli` since 14.10, but posting launched the Shortcut here directly,
            # so every post of the day still went through it. Same route as signup/warm-up now.
            from adb_bot.automation.iphone_farm_wda import resolve
            rec_id, _phone = resolve(self.ref)
            last = ""
            for attempt in range(3):                       # the DNS gate answers empty once in a while right after the tunnel came up (G, 15.10)
                from adb_bot.automation import iphone_farm_fastopen
                ok, last = iphone_farm_fastopen.open_container(rec_id, container, log=self.log)
                if ok:
                    break
                self.log(f"{container}: opening the container failed ({last}) -- try {attempt + 1}/3")
                time.sleep(20)
            else:
                raise FlowError(f"{container}: {last}")
            for i in range(40):
                if self.front() == IG:
                    self.pause(3, 5)
                    return
                if i in (2, 12):                           # iOS swallows `uiopen --bundleid` now and then (D1, 15.10): start it through WDA
                    try:
                        from adb_bot.automation.iphone_farm_wda import connect as _wda
                        _p = _wda(rec_id, timeout=30)
                        try:
                            _p.launch(IG)                  # the container is already selected, this only brings the app up
                        finally:
                            _p.close()
                    except Exception as exc:               # noqa: BLE001
                        self.log(f"{container}: WDA could not start Instagram ({str(exc)[:80]})")
                time.sleep(3)
            raise FlowError(f"{container}: container opened (exit IP ok) but Instagram is not in front")
        per_launch = max(timeout / 2, 180)
        end_all = time.monotonic() + max(timeout, per_launch)
        launches = 0
        while time.monotonic() < end_all:
            launches += 1
            for attempt in range(3):                      # same SSH path as XXTouch (the config's own path can lag)
                try:
                    ssh(self.dest, f"uiopen 'shortcuts://run-shortcut?name={container}'", timeout=30)
                    break
                except Exception as exc:                  # noqa: BLE001
                    if attempt == 2:
                        raise FlowError(f"{container}: shortcut did not start ({exc})")
                    time.sleep(10)
            end = min(time.monotonic() + per_launch, end_all)
            while time.monotonic() < end:
                time.sleep(3)
                if self.front() == IG:
                    self.pause(3, 5)
                    return
                self._allow_ssh_script_prompt()
            self.log(f"{container}: Instagram not up yet after launch {launches} -- relaunching the shortcut")
        raise FlowError(f"{container}: Instagram did not open within {timeout:.0f} s ({launches} launches) -- "
                        f"the Shortcut's IP/DNS check probably stopped it (see the notification on the phone)")


# ---- Instagram: post one reel (proven on A6 09.10) + scroll the feed

def _clip_seconds(path: str) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    return int(round(float(out or 0)))


def _keep_screen(x: XX, why: str, detail: str = "", container: str = "") -> str | None:
    """Screenshot + on-screen text of a screen the flow did not understand -> /opt/adbbot/logs/screens/<date>/.
    22.10 (Rene: "ueberall, wenn der Bot nicht weiter weiss, brauche ich einen Screenshot"): shared module,
    WDA picture first (works while XXTouch is 403/busy), XXTouch screen.image as fallback. Returns the path."""
    from adb_bot.automation import iphone_diag
    return iphone_diag.keep_screen(x.ref, why, detail, container=container, xx=x, log_fn=x.log)


def _dismiss_popups(x: XX) -> bool:
    """The interruptions seen on the way: notification nag, 'Edits' promo, generic OK/Not now."""
    words = x.ocr()
    # 14.10 live (B1, screenshot): iOS "Low Battery -- 20% battery remaining" (Low Power Mode / Close) sat over
    # Instagram and swallowed every tap -> "did not reach 'New reel'". Close it (B had come off its charger).
    if any(w["text"].strip() == "Low Battery" for w in words):
        close = next((w for w in words if w["text"].strip() == "Close"), None)
        if close:
            x.tap(close["x"], close["y"]); return True
    if any(re.search(r"to display notifications|wants to send you notifications", w["text"]) for w in words):
        allow = next((w for w in words if w["text"].strip() == "Allow" and w["y"] < 300), None)
        if allow:                                         # Shortcut's own one-time prompt, sits over the "+" button
            x.tap(allow["x"], allow["y"]); return True
    # 17.10, Rene (live photo): IG's in-app "Your notifications are off / Turn on / Not now" nag
    # sits over the feed and swallows every tap. Different from the iOS system permission above
    # (which keeps the "wants to send you notifications" wording and we tap Allow). Here we tap
    # "Not now" or, if OCR missed that small grey link, fall back to its known iPhone-X position.
    if any(re.search(r"notifications are off|miss new likes", w["text"]) for w in words):
        nn = next((w for w in words if re.fullmatch(r"(Not now|Not Now)", w["text"].strip())), None)
        if nn:
            x.tap(nn["x"], nn["y"]); return True
        x.tap(187, 710); return True                      # fallback: Not-now link's typical position
    # 11.10: "Instagram Would Like to Access Your Photos" (no preceding "Continue" screen, unlike
    # camera/mic) has its real button worded "Allow Access to All Photos" -- the exact match above
    # never caught it, so this sat unhandled behind every single "did not reach 'New reel'" failure
    # today (confirmed live on C10, same symptom also hit A1/B10/D2). Posting needs gallery access,
    # so this one is allowed, same stance as the Continue/Allow loop in post_reel.
    if any(re.fullmatch(r"Allow Access to All Photos", w["text"].strip()) for w in words):
        allow = next(w for w in words if w["text"].strip() == "Allow Access to All Photos")
        x.tap(allow["x"], allow["y"]); return True
    for w in words:
        t = w["text"].strip()
        if t == "Start new video":
            x.tap(w["x"], w["y"]); return True
        if re.fullmatch(r"(Not now|Not Now|OK|Allow)", t) and w["y"] > 450:
            x.tap(w["x"], w["y"]); return True
        if re.search(r"Level up your videos|Get on App Store", t):
            x.swipe(187, 450, 190, 790, 0.35); return True
    return False


def _full_photo_access(x: XX) -> None:
    """'You've given Instagram access to a select number of photos' -> Settings > Photos > All Photos."""
    x.tap_text(r"^Manage$", 4); x.pause(1.5, 2.5)
    x.tap_text(r"^Change settings$", 4); x.pause(2.5, 3.5)
    x.tap_text(r"^Photos$", 5); x.pause(1.5, 2.5)
    x.tap_text(r"^(All Photos|Full Access)$", 4); x.pause(1.5, 2.5)
    x.tap(45, 35); x.pause(3, 4)                         # status-bar "< Instagram"


# ---------------------------------------------------------------- human verification (text captcha)

def _vnc_screenshot(x: "XX", out_path: str, local_port: int = 0) -> str:
    """Full-screen PNG via CatVNC, tunnelled over `x.dest` (USB or WiFi, whichever
    is live) -- XXTouch's own OCR cannot read a *distorted* captcha image, this
    is the only way to actually see one. 12.10: no local port is hardcoded since
    the farm's live dashboard already holds 15901-15907 open against the same
    Mac; 0 asks the OS for a free one so this never collides with it."""
    import socket, struct, warnings
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    from adb_bot.automation.iphone_farm_vnc import Phone, _rx

    s = socket.socket(); s.bind(("127.0.0.1", local_port)); port = s.getsockname()[1]; s.close()
    proc = subprocess.Popen(
        ["ssh", "-i", iphone_farm_config.SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "ExitOnForwardFailure=yes", *iphone_farm_config.lan_ssh_opts(x.dest),
         "-N", "-L", f"127.0.0.1:{port}:127.0.0.1:5900", x.dest],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                break
            except OSError:
                if proc.poll() is not None:
                    raise FlowError(f"{x.ref}: VNC tunnel for captcha screenshot failed")
                time.sleep(0.2)
        p = Phone.__new__(Phone)
        p.sock = socket.create_connection(("127.0.0.1", port), 10)
        _rx(p.sock, 12)
        p.sock.sendall(b"RFB 003.008\n")
        _rx(p.sock, _rx(p.sock, 1)[0])
        p.sock.sendall(b"\x02")
        challenge = _rx(p.sock, 16)
        password = x.phone.get("vnc_password", "")
        key = bytes(int(f"{c:08b}"[::-1], 2) for c in password.encode()[:8].ljust(8, b"\0"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            enc = Cipher(TripleDES(key), modes.ECB()).encryptor()
        p.sock.sendall(enc.update(challenge) + enc.finalize())
        if _rx(p.sock, 4) != b"\0\0\0\0":
            raise FlowError(f"{x.ref}: wrong VNC password for captcha screenshot")
        p.sock.sendall(b"\x01")
        p.width, p.height = struct.unpack(">HH", _rx(p.sock, 4))
        _rx(p.sock, 16)
        _rx(p.sock, struct.unpack(">I", _rx(p.sock, 4))[0])
        return p.screenshot(out_path)
    finally:
        proc.terminate()


def _dismiss_paste_prompt(x: "XX") -> None:
    """`x.type()` goes through the pasteboard -- iOS asks 'Instagram would like
    to paste from "lua" -- Allow Paste' every single time (12.10, confirmed live:
    no way to pre-grant, same as the Shortcut's own one-time prompts)."""
    if x.tap_text(r"^Allow Paste$", 3):
        x.pause(1, 1.5)


def solve_human_verification_captcha(x: "XX", max_tries: int = 4, log=print) -> bool:
    """'Confirm you're human' + a distorted image -> read it with the real solver
    (2captcha, same one flows/verification.py uses) and type the answer in.
    12.10, built live solving @nikkiinbloom46 (C10): the crop is a fixed
    fraction of the VNC frame because every account gets the same Instagram
    template (confirmed across A7/B11/D13/C10 on 11.10) -- not a per-device
    pixel box. Returns True once the screen moves on (phone number / selfie /
    done), False if the solver can't crack it within `max_tries`."""
    from adb_bot.clients.captcha import build_solver
    from PIL import Image

    solver = build_solver(logger=None)
    for attempt in range(1, max_tries + 1):
        if not x.find(r"Confirm you.re human", 0):
            return True                                   # already past it
        shot = f"/tmp/xxf_captcha_{x.ref}_{attempt}.png"
        _vnc_screenshot(x, shot)
        img = Image.open(shot)
        w, h = img.size                                   # fixed template: image sits ~28%-38% down, ~8%-90% across
        crop = img.crop((round(w * 0.08), round(h * 0.28), round(w * 0.90), round(h * 0.38)))
        crop_path = shot.replace(".png", "_crop.png")
        crop.save(crop_path)
        answer = solver.solve_text(crop_path)
        if not answer:
            log(f"{x.ref}: captcha solver returned nothing (try {attempt}/{max_tries})")
            continue
        field = x.find(r"Enter the code from the image", 3)
        if field:
            x.tap(field["x"], field["y"]); x.pause(0.8, 1.2)
        x.type(answer)
        x.pause(1, 1.5)
        _dismiss_paste_prompt(x)
        x.pause(0.5, 1)
        if not x.tap_text(r"^Next$", 3):
            continue
        x.pause(3, 4)
        if x.find(r"Security Check Failed", 0):
            log(f"{x.ref}: captcha answer {answer!r} rejected (try {attempt}/{max_tries})")
            continue
        log(f"{x.ref}: captcha solved as {answer!r}")
        return True
    log(f"{x.ref}: captcha not solved after {max_tries} tries -- needs a human")
    return False


def _answer_permission_prompts(x: XX) -> None:
    """The first "+" on an account shows Instagram's own "Allow Instagram to access your camera and microphone"
    page (Continue), then iOS asks for the microphone, the camera and Photos, one after another.

    14.10, Rene: camera and microphone ALWAYS "Don't Allow" (posting from the gallery needs neither). Photos stays
    allowed -- without it Instagram cannot see the clip ("No media -- Enable photos access", D13 on 11.10).
    13.10 live (E20 re-enacted, screenshots): the old loop looked ~3 s for each alert while one OCR read took 10 s+
    on the slow farm WLAN, so the alerts were still open when it gave up -> "did not reach 'New reel'" on every
    account's first post (B12, C13, D3, E20 ...). Now: read the screen, answer what is there, repeat until clean."""
    for _ in range(8):
        words = x.ocr()
        text = " ".join(w["text"] for w in words)
        btn = lambda pat: next((w for w in words if re.fullmatch(pat, w["text"].strip())), None)  # noqa: E731
        if re.search(r"Access the (Microphone|Camera)", text):
            b = btn(r"Don.t Allow")
        elif re.search(r"Access Your Photos|Access to All Photos", text):
            b = btn(r"Allow (Full )?Access( to All Photos)?|Allow Access to All Photos|OK|Allow")
        elif re.search(r"access your camera and|camera and microphone", text):
            b = next((w for w in words if w["text"].strip() == "Continue" and w["y"] > 600), None)
        else:
            return
        if not b:
            time.sleep(1.5)
            continue
        x.tap(b["x"], b["y"])
        x.pause(2, 3)


def post_reel(x: XX, clip: str, log=print) -> None:
    """Clip already in the album (newest). From IG home: + -> REEL -> clip -> Next -> Next -> Share."""
    secs = _clip_seconds(clip)
    label = f"{secs // 60}:{secs % 60:02d}"
    # 2026-10-19 Rene: pre-flight check -- wenn Instagram ein Human-Verification screen zeigt,
    # bevor wir anfangen zu posten, sofort FlowError raise. post_batch's exception-handler erkennt
    # das Pattern und tagged Status='Human Verification' + skipped für heute, nächster Account auf
    # dem Phone übernimmt. Verhindert dass wir auf einem HV-Account versuchen zu posten.
    if x.front() == IG and x.find(r"confirm you.?re human|Enter your mobile number|confirm this mobile number|We detected unusual activity|Help us confirm", 0):
        raise FlowError("Human Verification on screen -- cannot post")
    for attempt in range(2):
        # 13.10 live (B12, screen read right at the failure): the home screen was in front, not Instagram -- IG had
        # slipped to the background (XXTouch licence dialog over the proxy, or video_to_album), and "+" was tapped
        # into the springboard: "did not reach 'New reel'" on B/C/D/E all evening. Bring a RUNNING Instagram back;
        # never launch it fresh (Rene: Instagram only via the container Shortcut) -- if it is gone, fail cleanly
        # so the next try reopens it through the Shortcut.
        if x.front() != IG:
            x.tap_text(r"^Dismiss$", 1)
            running = x.lua_out(f'OUT = tostring(app.is_running("{IG}"))').strip() == "true"
            if not running:
                raise FlowError("Instagram is not running (went away before posting) -- reopen via Shortcut")
            x.lua_out(f'app.run("{IG}") OUT = "ok"')
            x.pause(2.5, 3.5)
            log("  Instagram was in the background -- brought it back")
        _dismiss_popups(x)
        # 14.10 live (C2, screenshot): the feed scrolling between posts had wandered into a stranger's profile
        # (veloura.wavs) -- a full-screen page without the bottom tab bar, so the "Home" tap hit a grid tile and "+"
        # never showed. Walk back out with the top-left "<" first.
        for _ in range(4):
            back = x.find(r"^[<‹〈]$", 0, region=(0, 40, 70, 120))
            if not back:
                break
            x.tap(back["x"], back["y"]); x.pause(1.2, 2)
        x.tap(37, 752); x.pause(1.2, 2)                   # Home tab: back to the top, so the header with "+" shows
        x.tap(28, 69); x.pause(2.5, 3.5)                  # "+" top left of the home feed
        _answer_permission_prompts(x)                     # first time per container: camera/mic intro + iOS alerts
        # 14.10 live (E20): right after the first "Don't Allow" Instagram drops out of the create screen into the
        # Reels feed (the "reel viewer" seen on D on 13.10); a second "+" then opens it normally with POST/STORY/REEL.
        if not x.find(r"^New reel$", 0) and not x.find(r"POST|STORY|REEL|LIVE", 0, region=(0, 700, 375, 800)):
            x.tap(37, 752); x.pause(1.2, 2)
            x.tap(28, 69); x.pause(2.5, 3.5)
            _answer_permission_prompts(x)
        for _ in range(4):                                # mode bar POST STORY INSTANTS REEL LIVE -- REEL may be off-screen
            if x.find(r"^New reel$", 1.5):
                break
            line = x.find(r"POST|STORY|INSTANTS|REEL|LIVE", 0, region=(0, 700, 375, 800))
            if not line or len(line["box"]) != 4:
                _dismiss_popups(x); x.pause(1.5, 2.5)        # sheet/intro over the camera -- look again
                continue
            t, b = line["text"], line["box"]
            if "REEL" in t.upper():
                i = t.upper().find("REEL")
                x.tap(b[0] + (b[2] - b[0]) * ((i + 2) / len(t)), line["y"])
            else:                                         # step right: tap the last visible mode
                x.tap(b[2] - 12, line["y"])
            x.pause(2.5, 3.5)
        if not x.find(r"^New reel$", 20):
            _keep_screen(x, "no_new_reel")                # 14.10 Rene: "screen" -- keep what was really there
            if attempt == 0:
                # 17.10, Rene (B stuck live): if the pre-post scroll accidentally tapped the account's own
                # username/reel we land in a nested view with a back arrow top-left; tap that first (3x to unwind
                # a chain) BEFORE 14.10's Home-tab jump, otherwise Home-tab leaves you on Home with the nested
                # view still underneath after re-entering.
                for _ in range(3):
                    x.tap(37 + random.uniform(-5, 5), 67 + random.uniform(-5, 5))
                    x.pause(0.6, 1.1)
                x.tap(37, 752); x.pause(1.5, 2.5)         # 14.10: Home tab (bottom left) -> redo from +
                continue
            raise FlowError("did not reach 'New reel'")
        x.tap_text(r"^Start new video$", 1.5)              # leftover draft from an interrupted run
        x.pause(1, 2)
        if x.find(r"access to a select", 0):
            _full_photo_access(x)
            continue                                      # IG restarts after the permission change
        break
    # newest album item = first tile after the camera tile = the clip video_to_album just added.
    # Its duration label sits bottom-right of the tile (~(221,417)); OCR may glue burned-in subtitle text to it
    # and IG rounds differently than ffprobe -> match m:ss anywhere in the word.
    # 17.10 (Rene live, Phone A 4 Accounts × 3 Fehlversuche jeweils dasselbe Muster): the +-1 s tolerance
    # was too tight -- iOS Photos trims trailing silence off our Nikki clips (ffprobe 28.6-28.7 s was
    # consistently labelled "0:25" / "0:26" in the album). Rene saw 7 identical copies stacked up in the
    # gallery, confirming video_to_album worked and only this check kept rejecting them. Loosen to +-5 s
    # (next alternative tile would be another, clearly different clip, so false positives stay unlikely).
    def dur(t):
        m = re.search(r"(\d+):(\d\d)\s*$", t.strip())
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None
    labels = [(w, dur(w["text"])) for w in x.ocr() if w["y"] > 250 and dur(w["text"]) is not None]
    first = min(labels, key=lambda l: (round(l[0]["y"] / 60), l[0]["x"]), default=None)
    # If OCR missed the duration label entirely (first is None), give iOS one more beat to render it,
    # then re-OCR. video_to_album can finish server-side before iOS has rendered the tile.
    if not first:
        x.pause(2, 3)
        labels = [(w, dur(w["text"])) for w in x.ocr() if w["y"] > 250 and dur(w["text"]) is not None]
        first = min(labels, key=lambda l: (round(l[0]["y"] / 60), l[0]["x"]), default=None)
    if not first or abs(first[1] - secs) > 5:
        raise FlowError(f"newest gallery item is {first and first[0]['text']!r}, expected ~{label} -- not ours, stopped")
    hit = first[0]
    x.tap(hit["x"] - 34, hit["y"] - 87); x.pause(2.5, 4)  # duration label sits bottom-right of its tile
    if not x.tap_text(r"^Next", 40, region=(240, 700, 375, 790)):
        if not x.find(r"^1$", 0):
            raise FlowError("gallery: clip not selected")
        x.tap(327, 741)                                   # OCR missed the button; the clip is selected ("1")
    end = time.monotonic() + 40                          # editor (maybe behind the Edits promo)
    while time.monotonic() < end:
        x.pause(2, 3)
        if x.find(r"^Edit video$", 0):
            break
        _dismiss_popups(x)
    else:
        raise FlowError("editor did not open")
    x.tap_text(r"^Next", 4, region=(240, 700, 375, 790)); x.pause(3.5, 5)
    end = time.monotonic() + 90                          # some accounts get one more page (caption -> Next -> Share);
                                                           # 11.10: 40s was too tight on slow devices -- OCR dump on C3
                                                           # mid-failure showed the normal "New reel" editor with "Next"
                                                           # clearly visible and tappable, just not reached in time
                                                           # (6x "caption screen: no Share" today, all on this timeout)
    # 14.10, Rene: the last button is not always "Share" ("manchmal steht auch kein Share dort") -- accept the
    # other wordings Instagram uses there, and when none shows up, keep a screenshot + the screen text so the
    # real layout can be seen instead of guessed.
    SHARE = r"^(Share|Post|Share reel|Teilen|Posten)$"
    while not x.find(SHARE, 0, region=(0, 600, 375, 790)):      # also the one-time "About Reels" sheet
        if time.monotonic() > end:
            _keep_screen(x, "no_share")
            raise FlowError("caption screen: no Share")
        if x.find(r"^Add as overlays?$", 0):              # 14.10 live (B1, screenshot): a stray tap in the new editor
            x.tap_text(r"^[Xx×]$", 2, region=(0, 40, 80, 110)) or x.tap(28, 66)   # opened the overlay gallery
            x.pause(1.5, 2.5)
            x.tap_text(r"^Next", 2, region=(180, 700, 375, 790)); x.pause(3, 4.5); continue
        if x.tap_text(r"^Continue$", 0, region=(0, 650, 375, 790)):   # "Others can now download and share your reels"
            x.pause(2, 3); continue
        if x.find(r"Add a caption", 0) and x.tap_text(r"^Next", 0, region=(180, 700, 375, 790)):
            x.pause(3, 4.5); continue
        _dismiss_popups(x); x.pause(1.5, 2.5)
    x.pause(1.5, 4)
    x.tap_text(SHARE, 4, region=(0, 600, 375, 790))
    x.pause(2, 3)
    x.tap_text(SHARE, 2, region=(0, 600, 375, 790))        # "About Reels" sheet after the first Share
    # 21.10 Rene: don't idle-wait for "Done posting" -- that banner is unreliable and often never shows.
    # Once Share went through (SHARE button no longer sits on screen = tap registered + IG switched to the
    # upload view), IG keeps uploading in the background. Scroll the feed for ~60 s to look like a real
    # user, then return so the next clip can go. The per-batch verification happens later via media_count
    # on the profile -- not on this toast.
    end = time.monotonic() + 20                            # short probe: did Share actually register?
    shared = False
    while time.monotonic() < end:
        x.pause(2, 3)
        try:
            if not x.find(SHARE, 0, region=(0, 600, 375, 790)):     # Share button gone -> upload view
                shared = True; break
            if x.find(r"Done posting|Watch again|Watch more reels|Reel posted", 0):
                shared = True; break
        except RuntimeError:
            continue
    if not shared:
        _keep_screen(x, "share_not_registered")
        raise FlowError("Share tap did not register (button still on screen)")
    # Scroll ~60 s in the Reels tab while the upload runs in the background. The post_reel caller then
    # starts the next clip; the batch-level check reads media_count from the profile at the end.
    try:
        scroll_feed(x, 60)
    except Exception as exc:                              # noqa: BLE001 -- any scroll failure must not stop the batch
        log(f"  scroll during upload failed ({exc}) -- continuing")
    log("  shared (background upload, verified at batch end via media_count)")
    return


def scroll_feed(x: XX, seconds: float) -> None:
    """Human-ish reels browsing. Two paths: PRIMARY plays a random slice of Rene's own XXTouch
    recording (iphone_scroll_rec) -- real finger pressure/size variations IG actually fingerprints
    (17.10 Rene: "mit XXTouch echte Fingerstärke/-druck, bei Remote-Zugriff gibt es das nicht").
    FALLBACK is the synthetic 18-action mix below -- used when the recording file isn't on this
    server. Both start on the Reels tab (14.10 Rene: never scroll the post-upload "Watch more reels"
    screen; next post always starts from Home -> "+" -> REEL in post_reel).

    If a username / own-reel tap accidentally lands us in a nested view, the top-left back arrow is
    tapped before falling back to the Reels tab (same recovery pattern as post_reel's no_new_reel
    handler, 17.10 Rene). Everything goes via XXTouch (XX.tap / XX.swipe / Touch.run)."""
    x.tap(112, 752)                                       # Reels tab (bottom, 2nd icon)
    x.pause(2, 3)
    # 2026-10-19 Rene: "immer scannen ob wirklich im Reels-Tab" -- wenn Tap auf (112,752) in einem
    # vollbild-Profile landete (kein Tab-Bar), kam der Finger-Rec auf dem Profile-Grid raus und
    # scrollte pull-to-refresh. Max 3 recovery versuche: back-arrow, dann Reels-Tab nochmal.
    for _attempt in range(3):
        in_profile = bool(x.find(r"^Follow$|^Message$|^Following$|^Posts$|^Reels$|^Tagged$|^header-view$", 0, region=(0, 100, 375, 700)))
        if not in_profile:
            break
        x.log(f"scroll_feed: landed in a profile view, unwinding (try {_attempt+1}/3)")
        # top-left back-arrow (1-3 taps to unwind nested view), then Reels tab again
        for _ in range(2):
            back = x.find(r"^[<‹〈]$", 0, region=(0, 40, 70, 120))
            if back: x.tap(back["x"], back["y"]); x.pause(0.6, 1.0)
            else: break
        x.tap(112, 752); x.pause(2, 3)

    # ── primary path: play a random slice of Rene's recording (17.10) ───────────────────────
    try:
        from adb_bot.automation import iphone_scroll_rec as _rec
        lua = _rec.build_scroll_lua(seconds)
        if lua:
            # 17.10 Rene asked for proof the finger-rec is actually being used (vs the synthetic
            # fallback). The log tells him: slice size (bytes = density proxy), target duration.
            x.log(f"scroll_feed: using Rene's finger-rec, {len(lua)//1024} KB slice for {seconds:.0f}s")
            x.t.run(lua, wait=True)                       # blocks for ~seconds (recorded pace)
            return
        x.log("scroll_feed: finger-rec unavailable, falling back to synthetic")
    except Exception as _exc:                             # noqa: BLE001 -- fall through to synthetic
        x.log(f"scroll_feed: finger-rec failed ({str(_exc)[:60]}), falling back to synthetic")

    # Per-session "personality" -- caps on heavier actions, so most sessions never like, some like 1-2x
    cap_like      = random.choice([0, 0, 0, 1, 1, 2])     # max likes per session (Rene: "1-2 mal max")
    cap_profile   = random.choice([0, 0, 0, 1, 1, 2])     # username/profile peeks
    cap_bookmark  = random.choice([0, 0, 0, 0, 1])        # rare save
    cap_caption   = random.choice([0, 0, 1, 1, 2])
    cap_mute      = random.choice([0, 0, 1, 1, 2, 3])
    cap_center    = random.choice([0, 1, 1, 2, 3, 4])     # center-taps (pause/play, miss-taps)
    cap_detour    = random.choice([0, 0, 1, 1, 2])        # Home/Search tab detour
    cap_menu      = random.choice([0, 0, 0, 1])           # three-dot menu peek

    # Weighted actions. Each weight is a per-session random drift so the mix never repeats exactly.
    # Everything NOT matched falls through to a plain down-flick with a short dwell.
    w = {
        "burst_down":  random.uniform(0.05, 0.14),        # 2-5 quick next-flicks
        "burst_up":    random.uniform(0.02, 0.06),        # 2-3 quick back-flicks
        "back_single": random.uniform(0.04, 0.10),        # 1 back-swipe (previous reel)
        "back_multi":  random.uniform(0.02, 0.06),        # 2-3 back-swipes
        "dwell_med":   random.uniform(0.08, 0.16),        # 4-10 s on a reel
        "dwell_long":  random.uniform(0.06, 0.14),        # 10-25 s
        "dwell_watch": random.uniform(0.02, 0.06),        # 25-70 s "watching"
        "like_dbltap": random.uniform(0.012, 0.028),      # double-tap center -> like
        "like_heart":  random.uniform(0.008, 0.018),      # single-tap heart icon on right rail
        "mute_tap":    random.uniform(0.015, 0.035),      # mute/unmute toggle
        "pause_tap":   random.uniform(0.015, 0.035),      # single center-tap (pause/play)
        "save_tap":    random.uniform(0.004, 0.010),      # bookmark icon
        "see_more":    random.uniform(0.015, 0.030),      # expand caption
        "username":    random.uniform(0.015, 0.030),      # tap username -> profile -> back
        "menu_dots":   random.uniform(0.004, 0.010),      # three-dot menu peek -> dismiss
        "tab_detour":  random.uniform(0.015, 0.030),      # Home/Search tab for a few s, then back
        "idle":        random.uniform(0.02, 0.07),        # just... let the current reel play
        "overshoot":   random.uniform(0.015, 0.035),      # start a flick but let go early (half-swipe)
    }
    likes = profiles = bookmarks = captions = mutes = centers = detours = menus = 0
    end = time.monotonic() + seconds

    def _down(short=False):
        y1 = 620 + random.uniform(-50, 40)
        dy = random.uniform(140, 240) if short else random.uniform(280, 470)
        x.swipe(190 + random.uniform(-40, 40), y1,
                185 + random.uniform(-40, 40), y1 - dy,
                random.choice([None, None, 0.22, 0.32, 0.45]))   # vary flick speed

    def _up():
        y1 = 260 + random.uniform(-30, 30)
        dy = random.uniform(260, 440)
        x.swipe(185 + random.uniform(-25, 25), y1,
                190 + random.uniform(-25, 25), y1 + dy,
                random.choice([None, 0.28, 0.42]))

    def _back_arrow_if_stuck():
        """A profile/menu peek may land us in a nested view -- top-left back-arrow unwinds it (max 3x)."""
        for _ in range(3):
            x.tap(37 + random.uniform(-5, 5), 67 + random.uniform(-5, 5))
            time.sleep(random.uniform(0.5, 0.9))

    while time.monotonic() < end:
        remaining = end - time.monotonic()
        r = random.random(); acc = 0.0; chosen = "flick"
        for key, weight in w.items():
            acc += weight
            if r < acc:
                chosen = key
                break

        # Enforce caps -- hit the cap -> fall back to a plain flick
        if   chosen == "like_dbltap" and likes >= cap_like:         chosen = "flick"
        elif chosen == "like_heart"  and likes >= cap_like:         chosen = "flick"
        elif chosen == "username"    and profiles >= cap_profile:   chosen = "flick"
        elif chosen == "save_tap"    and bookmarks >= cap_bookmark: chosen = "flick"
        elif chosen == "see_more"    and captions >= cap_caption:   chosen = "flick"
        elif chosen == "mute_tap"    and mutes >= cap_mute:         chosen = "flick"
        elif chosen == "pause_tap"   and centers >= cap_center:     chosen = "flick"
        elif chosen == "tab_detour"  and detours >= cap_detour:     chosen = "flick"
        elif chosen == "menu_dots"   and menus >= cap_menu:         chosen = "flick"

        if chosen == "flick":
            _down()
            time.sleep(min(remaining, random.uniform(3, 8)))

        elif chosen == "overshoot":                                  # half-swipe that snaps back
            _down(short=True)
            time.sleep(min(remaining, random.uniform(0.8, 2.5)))

        elif chosen == "burst_down":
            for _ in range(random.randint(2, 5)):
                if time.monotonic() >= end: break
                _down()
                time.sleep(random.uniform(0.15, 0.6))
            time.sleep(min(max(0.0, end - time.monotonic()), random.uniform(1.5, 4.0)))

        elif chosen == "burst_up":
            for _ in range(random.randint(2, 3)):
                if time.monotonic() >= end: break
                _up()
                time.sleep(random.uniform(0.2, 0.8))

        elif chosen == "back_single":
            _up()
            time.sleep(min(remaining, random.uniform(2, 5)))

        elif chosen == "back_multi":
            for _ in range(random.randint(2, 3)):
                if time.monotonic() >= end: break
                _up()
                time.sleep(random.uniform(1.0, 2.5))

        elif chosen == "dwell_med":
            time.sleep(min(remaining, random.uniform(4, 10)))

        elif chosen == "dwell_long":
            time.sleep(min(remaining, random.uniform(10, 25)))

        elif chosen == "dwell_watch":
            time.sleep(min(remaining, random.uniform(25, 70)))

        elif chosen == "like_dbltap":
            cx = 190 + random.uniform(-40, 40); cy = 400 + random.uniform(-80, 80)
            x.tap(cx, cy); time.sleep(random.uniform(0.08, 0.18)); x.tap(cx, cy)
            likes += 1
            time.sleep(min(remaining, random.uniform(2, 5)))

        elif chosen == "like_heart":
            x.tap(350 + random.uniform(-5, 5), 410 + random.uniform(-18, 18))
            likes += 1
            time.sleep(min(remaining, random.uniform(2, 5)))

        elif chosen == "mute_tap":
            x.tap(345 + random.uniform(-10, 10), 730 + random.uniform(-10, 10))
            mutes += 1
            time.sleep(min(remaining, random.uniform(1.5, 4)))

        elif chosen == "pause_tap":
            x.tap(190 + random.uniform(-40, 40), 400 + random.uniform(-100, 100))
            centers += 1
            time.sleep(min(remaining, random.uniform(1.5, 6)))

        elif chosen == "save_tap":
            x.tap(350 + random.uniform(-5, 5), 580 + random.uniform(-15, 15))
            bookmarks += 1
            time.sleep(min(remaining, random.uniform(1.5, 3)))

        elif chosen == "see_more":
            x.tap(100 + random.uniform(-30, 30), 720 + random.uniform(-10, 10))
            captions += 1
            time.sleep(min(remaining, random.uniform(3, 8)))
            x.tap(190 + random.uniform(-30, 30), 300 + random.uniform(-30, 30))   # tap outside to collapse
            time.sleep(min(max(0.0, end - time.monotonic()), random.uniform(1, 3)))

        elif chosen == "username":
            x.tap(70 + random.uniform(-15, 15), 680 + random.uniform(-15, 15))
            profiles += 1
            time.sleep(min(remaining, random.uniform(2.5, 6)))
            _back_arrow_if_stuck()                                   # out of profile
            time.sleep(min(max(0.0, end - time.monotonic()), random.uniform(1.0, 2.5)))

        elif chosen == "menu_dots":
            x.tap(350 + random.uniform(-8, 8), 640 + random.uniform(-10, 10))
            menus += 1
            time.sleep(random.uniform(1.5, 3.0))
            x.tap(190 + random.uniform(-30, 30), 180 + random.uniform(-20, 20))   # dismiss sheet
            time.sleep(min(max(0.0, end - time.monotonic()), random.uniform(1, 2.5)))

        elif chosen == "tab_detour":
            if random.random() < 0.5: x.tap(37, 752)                 # Home
            else:                     x.tap(300, 752)                # DM/Search area
            detours += 1
            time.sleep(random.uniform(3, 10))
            x.tap(112, 752)                                          # back to Reels
            time.sleep(min(max(0.0, end - time.monotonic()), random.uniform(1.5, 3.0)))

        elif chosen == "idle":
            time.sleep(min(remaining, random.uniform(2, 8)))
