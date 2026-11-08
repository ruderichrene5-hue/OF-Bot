"""Grab Instagram's cross-device login QR code from a Multilogin X browser profile.

Half of a two-machine relay for logging a Multilogin browser profile into an
Instagram account that's already logged in on a phone, without touching
passwords or cookies:

  1. THIS script (runs on the machine where Multilogin X itself is open) opens
     instagram.com's login page in the target browser profile, clicks through
     to the "Log in with QR code" screen, and saves just the QR code as a PNG.
  2. Copy that PNG to wherever `push_qr_and_scan.py` runs (the box with ADB/
     GeeLark/MLX access to the phone).
  3. `push_qr_and_scan.py` pushes it into the phone's gallery and drives
     Instagram's own QR scanner to pick it from there and approve the login --
     confirmed working via the scanner's gallery-picker button, since a cloud
     phone has no real camera to point at a screen.
  4. Back here, this script polls the browser and reports once the login goes
     through (the page navigates away from the login screen on its own).

Why not just copy cookies: tried it first. Needs root (`su`) to read
Instagram's shared_prefs, and this fleet's phones don't have it -- confirmed
against a live phone (`run-as: package not debuggable`, `su: not found`). This
QR route needs no root; it's Meta's own supported cross-device login.

Instagram's login page markup isn't pinned to a stable selector, so the
"Log in with QR code" click is done by visible text/aria-label rather than a
CSS class, and falls back to telling you to click it yourself if it can't find
it (you have the browser open in front of you anyway).

Requirements (on the Multilogin machine):
    pip install selenium

Usage:
    python3 capture_login_qr.py <mlx_profile_id> [--out login_qr.png] [--api-base http://127.0.0.1:35000]
"""

from __future__ import annotations

import argparse
import time

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

DEFAULT_API_BASE = "http://127.0.0.1:35000"
LOGIN_URL = "https://www.instagram.com/accounts/login/"

# Candidate ways to find the "Log in with QR code" control across Instagram's
# shifting markup -- tried in order, first match wins.
QR_ENTRY_XPATHS = [
    "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
    "'abcdefghijklmnopqrstuvwxyz'), 'qr code')]",
    "//*[contains(@aria-label, 'QR')]",
]

# Once on the QR screen, the code itself is normally the only <canvas> or <svg>
# rendered there; <img> is a fallback for markup that renders it as a data URI.
QR_IMAGE_SELECTORS = ["canvas", "svg", "img[src*='qr']"]


def start_profile(api_base: str, profile_id: str) -> str:
    url = f"{api_base}/api/v1/profile/start"
    resp = requests.get(url, params={"automation": "true", "profileId": profile_id}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"MLX refused to start profile {profile_id}: {data}")
    return data["value"]


def attach_selenium(selenium_endpoint: str) -> webdriver.Remote:
    options = Options()
    options.add_experimental_option("debuggerAddress", selenium_endpoint.replace("http://", ""))
    return webdriver.Remote(command_executor=selenium_endpoint, options=options)


def click_qr_entry(driver: webdriver.Remote) -> bool:
    for xpath in QR_ENTRY_XPATHS:
        elements = driver.find_elements(By.XPATH, xpath)
        for el in elements:
            try:
                el.click()
                return True
            except Exception:
                continue
    return False


def find_qr_element(driver: webdriver.Remote):
    for selector in QR_IMAGE_SELECTORS:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            return elements[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mlx_profile_id")
    parser.add_argument("--out", default="login_qr.png")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--wait-for-login-seconds", type=int, default=300,
                        help="how long to poll for the phone-side approval "
                             "before giving up (default 5 min)")
    args = parser.parse_args()

    print(f"[1/4] starting MLX profile {args.mlx_profile_id} ...")
    selenium_endpoint = start_profile(args.api_base, args.mlx_profile_id)
    driver = attach_selenium(selenium_endpoint)

    print("[2/4] opening Instagram login page ...")
    driver.get(LOGIN_URL)
    time.sleep(3)

    print("    looking for 'Log in with QR code' ...")
    if not click_qr_entry(driver):
        print("    couldn't find it automatically -- click it yourself in the "
              "browser window now, then press Enter here.")
        input()
    time.sleep(2)

    print("[3/4] capturing the QR code ...")
    qr_element = find_qr_element(driver)
    if qr_element is None:
        print("[-] couldn't locate the QR code element on the page. Take a "
              "manual screenshot of it instead, crop it, and use that as the "
              "--qr-image for push_qr_and_scan.py.")
        return 1
    qr_element.screenshot(args.out)
    print(f"[+] saved {args.out}")
    print("    copy this file to the machine that runs push_qr_and_scan.py, then:")
    print(f"    python3 push_qr_and_scan.py <phone_id> --platform mlx --qr-image {args.out}")

    print(f"[4/4] waiting up to {args.wait_for_login_seconds}s for the phone-side "
          "approval (checking whether this page navigates away from login) ...")
    deadline = time.time() + args.wait_for_login_seconds
    while time.time() < deadline:
        if "accounts/login" not in driver.current_url:
            print(f"[+] logged in -- now on {driver.current_url}")
            return 0
        time.sleep(5)

    print("[-] still on the login page after the wait -- scan may not have gone "
          "through yet. The browser stays open; check it directly.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
