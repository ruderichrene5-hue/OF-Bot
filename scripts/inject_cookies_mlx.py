"""Load an extracted Instagram session into a Multilogin X browser profile.

Pairs with `extract_ig_session.py` (run on the server, pulls cookies off a
GeeLark phone). This half runs wherever Multilogin X itself is installed and
open, since it talks to MLX's Local API on 127.0.0.1 and there is no way to
reach that from the remote server.

What it does:
  1. Starts the target MLX browser profile via the Local API with
     `automation=true`, which hands back a Selenium/CDP debug endpoint.
  2. Attaches Selenium to that running browser (does not launch a new one).
  3. Injects the Instagram cookies via CDP's `Network.setCookie`, which -
     unlike Selenium's plain `add_cookie` - can set them before ever loading
     instagram.com and can set `secure`/`httpOnly` correctly.
  4. Navigates to instagram.com so you can see straight away whether it took.

Requirements (on the Multilogin machine):
    pip install selenium requests

Multilogin X Local API needs a Scale/Custom-Scale plan and the local agent
running (it ships with the desktop app). Default port is 35000; change
--api-base if yours differs.

Usage:
    python3 inject_cookies_mlx.py ig_session_<id>.json <mlx_profile_id>
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

DEFAULT_API_BASE = "http://127.0.0.1:35000"

# name -> (secure, httpOnly). Instagram sets sessionid/csrftoken as HttpOnly;
# being wrong about that just means the cookie is a little more visible to JS
# than the real one, not a functional problem, but worth getting right.
COOKIE_FLAGS = {
    "sessionid": (True, True),
    "csrftoken": (True, False),
    "ds_user_id": (True, False),
    "mid": (True, True),
    "ig_did": (True, True),
    "rur": (True, True),
}


def start_profile(api_base: str, profile_id: str) -> str:
    url = f"{api_base}/api/v1/profile/start"
    resp = requests.get(url, params={"automation": "true", "profileId": profile_id}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"MLX refused to start profile {profile_id}: {data}")
    return data["value"]  # e.g. "http://127.0.0.1:XXXXX"


def attach_selenium(selenium_endpoint: str) -> webdriver.Remote:
    options = Options()
    options.add_experimental_option("debuggerAddress", selenium_endpoint.replace("http://", ""))
    return webdriver.Remote(command_executor=selenium_endpoint, options=options)


def set_cookie(driver: webdriver.Remote, name: str, value: str) -> None:
    secure, http_only = COOKIE_FLAGS.get(name, (True, False))
    driver.execute_cdp_cmd("Network.setCookie", {
        "name": name,
        "value": value,
        "domain": ".instagram.com",
        "path": "/",
        "secure": secure,
        "httpOnly": http_only,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_file", help="JSON written by extract_ig_session.py")
    parser.add_argument("mlx_profile_id", help="Multilogin X browser profile id")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    args = parser.parse_args()

    session = json.loads(open(args.session_file, "r", encoding="utf-8").read())
    cookies = session["cookies"]

    print(f"[1/4] starting MLX profile {args.mlx_profile_id} ...")
    selenium_endpoint = start_profile(args.api_base, args.mlx_profile_id)
    print(f"    got debug endpoint: {selenium_endpoint}")

    print("[2/4] attaching Selenium ...")
    driver = attach_selenium(selenium_endpoint)

    # Network domain (which Network.setCookie belongs to) needs at least one
    # navigation/page load in this tab before CDP will accept it on some
    # Chromium builds; an empty page is enough and doesn't touch Instagram yet.
    driver.get("about:blank")

    print(f"[3/4] injecting cookies: {list(cookies.keys())}")
    for name, value in cookies.items():
        set_cookie(driver, name, value)

    print("[4/4] loading instagram.com ...")
    driver.get("https://www.instagram.com/")
    time.sleep(3)

    if "login" in driver.current_url:
        print("[-] landed on the login page -- session likely didn't take "
              "(often means the browser profile's IP doesn't match the IP "
              "the mobile session was created on; try running the MLX "
              "profile through the same proxy as the GeeLark phone).")
        return 1

    print("[+] looks logged in. Browser stays open for you to test the extension.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
