"""Phone half of the QR login relay: feed a captured login QR into Instagram's
own scanner via its gallery-picker, so it approves a browser login with no
camera, no root, and no cookie extraction.

Pairs with `capture_login_qr.py` (run on the Multilogin machine, saves the
QR code Instagram shows on its login page). This script pushes that image
into the target phone's gallery and drives the app:

    Profile tab -> "Share profile" (opens your own nametag/QR) -> the
    "Scan QR code" button (top-right, confirmed present via
    content-desc="Scan QR code") -> grant camera permission if asked ->
    the scanner's gallery-picker icon (top-right of the scanner itself; it
    carries no accessible label, so this taps proportionally to screen size
    rather than a fixed pixel, then verified with a screenshot if it misses)
    -> the pushed image (top-left thumbnail, i.e. most recent).

Why this works without a real camera: confirmed live against a clean
(non-challenged) account -- the scanner has a real gallery picker
("No recent photos or videos" shown against an empty gallery), so a pushed
image is picked up the same as a live scan would be.

What happens after picking the image is untested past this point (never had
a real login QR to complete the loop with) -- the script dumps whatever
screen appears and tries to tap an obvious confirm/approve button by text; if
it doesn't recognize the screen, it prints everything on it and stops so you
can finish by hand once and tell me what it said.

Usage:
    python3 push_qr_and_scan.py <phone_id> --platform mlx --qr-image login_qr.png
    python3 push_qr_and_scan.py <phone_id> --platform geelark --qr-image login_qr.png

Requires the same environment extract_ig_session.py does (credentials sourced
from /etc/adbbot/env) and adb on PATH.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_ig_session import ready_profile_geelark, ready_profile_mlx  # noqa: E402

PHONE_GALLERY_DIR = "/sdcard/Pictures"
CONFIRM_KEYWORDS = ["log in", "confirm", "this was me", "yes,", "approve", "continue"]


def adb(target: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", "-s", target, *args], capture_output=True, text=True)


def connect_robustly(profile, attempts: int = 5, wait_seconds: int = 5) -> bool:
    """`ADBClient.connect`/`authenticate` are sometimes unreliable right after a
    fresh ADB-enable on this fleet (seen going straight to `device offline`
    even though a manual disconnect+connect+glogin succeeds seconds later).
    Redoing the sequence by hand, with retries, is what actually works.
    """
    target = profile.target
    for _ in range(attempts):
        subprocess.run(["adb", "disconnect", target], capture_output=True, text=True)
        time.sleep(1)
        connect = subprocess.run(["adb", "connect", target], capture_output=True, text=True)
        if "connected to" not in connect.stdout and "already connected" not in connect.stdout:
            time.sleep(wait_seconds)
            continue
        time.sleep(1)
        login = adb(target, "shell", "glogin", profile.pwd)
        if "success" in login.stdout.lower():
            return True
        time.sleep(wait_seconds)
    return False


def screen_size(target: str) -> tuple[int, int]:
    out = adb(target, "shell", "wm", "size").stdout
    match = re.search(r"(\d+)x(\d+)", out)
    if not match:
        return (1080, 2376)  # fallback: the resolution this was validated against
    return int(match.group(1)), int(match.group(2))


def dump_ui(target: str) -> str:
    adb(target, "shell", "uiautomator", "dump", "/sdcard/ui.xml")
    return adb(target, "shell", "cat", "/sdcard/ui.xml").stdout


def bounds_center(bounds: str) -> tuple[int, int]:
    nums = [int(n) for n in re.findall(r"\d+", bounds)]
    x1, y1, x2, y2 = nums
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_bounds_by_label(xml: str, label: str) -> str | None:
    pattern = (rf'<node[^>]*(?:content-desc|text)="{re.escape(label)}"'
               r'[^>]*bounds="([^"]*)"')
    match = re.search(pattern, xml)
    return match.group(1) if match else None


def tap_label(target: str, xml: str, label: str) -> bool:
    bounds = find_bounds_by_label(xml, label)
    if not bounds:
        return False
    x, y = bounds_center(bounds)
    adb(target, "shell", "input", "tap", str(x), str(y))
    return True


def all_texts(xml: str) -> list[str]:
    texts = re.findall(r'text="([^"]+)"', xml)
    descs = re.findall(r'content-desc="([^"]+)"', xml)
    return [t for t in texts + descs if t.strip()]


def push_to_gallery(target: str, image_path: Path) -> None:
    remote_path = f"{PHONE_GALLERY_DIR}/{image_path.name}"
    subprocess.run(["adb", "-s", target, "push", str(image_path), remote_path], check=True)
    # MediaStore won't show a pushed file until scanned -- without this the
    # gallery picker's "No recent photos" screen stays empty forever.
    adb(target, "shell", "am", "broadcast", "-a",
        "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{remote_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_id")
    parser.add_argument("--platform", choices=["geelark", "mlx"], required=True)
    parser.add_argument("--qr-image", required=True, type=Path)
    args = parser.parse_args()

    if not args.qr_image.exists():
        print(f"[-] {args.qr_image} not found")
        return 1

    print(f"[1/6] readying {args.profile_id} ({args.platform}) ...")
    profile = ready_profile_geelark(args.profile_id) if args.platform == "geelark" \
        else ready_profile_mlx(args.profile_id)
    if profile is None:
        return 1

    print(f"[2/6] connecting to {profile.target} ...")
    if not connect_robustly(profile):
        print("[-] could not establish a stable ADB connection")
        return 1
    target = profile.target

    print("[3/6] pushing QR image to the gallery ...")
    push_to_gallery(target, args.qr_image)
    time.sleep(2)

    print("[4/6] opening Instagram to your profile ...")
    adb(target, "shell", "am", "force-stop", "com.instagram.android")
    time.sleep(1)
    adb(target, "shell", "am", "start", "-n",
        "com.instagram.android/.activity.MainTabActivity")
    time.sleep(6)
    width, height = screen_size(target)
    adb(target, "shell", "input", "tap", str(int(width * 0.9)), str(int(height * 0.91)))
    time.sleep(2)

    xml = dump_ui(target)
    if not tap_label(target, xml, "Share profile"):
        print("[-] couldn't find 'Share profile' on the profile page -- app UI "
              "may have changed, or this account is mid-challenge. Dumped texts:")
        print(all_texts(xml))
        return 1
    time.sleep(2)

    print("[5/6] opening the QR scanner ...")
    xml = dump_ui(target)
    if not tap_label(target, xml, "Scan QR code"):
        print("[-] couldn't find the 'Scan QR code' button. Dumped texts:")
        print(all_texts(xml))
        return 1
    time.sleep(2)

    # The camera-permission dialog only appears the first time; harmless to
    # attempt this tap even when there's nothing there to hit.
    xml = dump_ui(target)
    tap_label(target, xml, "WHILE USING THE APP")
    time.sleep(2)

    # The scanner's gallery-picker icon carries no accessible label (confirmed
    # live), so this taps its on-screen position proportionally -- top-right
    # corner, same spot across the resolutions Instagram's scanner uses.
    adb(target, "shell", "input", "tap", str(int(width * 0.933)), str(int(height * 0.055)))
    time.sleep(2)

    print("[6/6] selecting the pushed image ...")
    # Most-recent-first grid -- the image we just pushed is the top-left tile.
    adb(target, "shell", "input", "tap", str(int(width * 0.15)), str(int(height * 0.22)))
    time.sleep(3)

    xml = dump_ui(target)
    texts = all_texts(xml)
    print("Screen after selecting the image:", texts)

    lowered = " ".join(texts).lower()
    for keyword in CONFIRM_KEYWORDS:
        if keyword in lowered:
            for text in texts:
                if keyword in text.lower():
                    if tap_label(target, xml, text):
                        print(f"[+] tapped {text!r} to approve")
                        return 0
    print("[-] didn't recognize a confirm button -- finish this one screen by "
          "hand and tell me what it said, so the next run can handle it "
          "automatically.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
