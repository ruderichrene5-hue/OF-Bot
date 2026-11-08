"""Install Instagram on a batch of GeeLark/MLX phones via the live console UI's
App Store panel (the only install path that actually works -- MLX's REST
`/mobile_profiles/app/install` accepts the call but never delivers anything,
confirmed via logcat; the console's own "Apps" button does).

Usage:
    python3 bulk_install_instagram.py <phone_id> [<phone_id> ...]

Requires: chromium + matching chromedriver on this machine (see
/snap/chromium/*/usr/lib/chromium-browser/{chrome,chromedriver}), and the
venv at /root/.adb_bot/venv-browser (has selenium).
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium import webdriver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adb_bot.clients.multilogin import MultiloginLauncherClient, MultiloginShutdownClient
from adb_bot.config import settings

CHROME_BIN = "/snap/chromium/3530/usr/lib/chromium-browser/chrome"
CHROMEDRIVER_BIN = "/snap/chromium/3530/usr/lib/chromium-browser/chromedriver"

# Apps icon: last item in the left toolbar, found empirically at this pixel
# position for the console's default 336px-wide phone view.
APPS_BUTTON = (358, 648)
# Instagram's row in the "All" category grid, right column, top row.
INSTAGRAM_INSTALL_BUTTON = (973, 135)


def make_driver() -> webdriver.Chrome:
    options = Options()
    options.binary_location = CHROME_BIN
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,1000")
    service = Service(CHROMEDRIVER_BIN)
    return webdriver.Chrome(service=service, options=options)


def install_on(console_url: str) -> None:
    driver = make_driver()
    try:
        driver.get(console_url)
        time.sleep(20)
        ActionChains(driver).move_by_offset(*APPS_BUTTON).click().perform()
        time.sleep(4)
        ActionChains(driver).move_by_offset(
            INSTAGRAM_INSTALL_BUTTON[0] - APPS_BUTTON[0],
            INSTAGRAM_INSTALL_BUTTON[1] - APPS_BUTTON[1]).click().perform()
        # The install itself keeps running after this click; give it time to
        # actually land before the phone gets shut down, or the trigger fires
        # but nothing is there next time the phone boots. Longer than the
        # original 75s -- under 10-way concurrency, several installs hadn't
        # finished by then (confirmed: they showed up as `no-instagram` in the
        # very next signup run), so this errs longer, especially since it now
        # runs at lower concurrency and can afford to.
        time.sleep(110)
    finally:
        driver.quit()


def _one(phone_id: str, launcher, shutdown) -> str:
    result = launcher.start_profiles([phone_id])
    details = (result.get("data") or {}).get("success_details") or []
    url = next((d.get("url") for d in details if d.get("id") == phone_id), None)
    if not url:
        return f"{phone_id}: launch failed: {result}"
    try:
        install_on(url)
        return f"{phone_id}: install triggered and settled"
    except Exception as exc:
        return f"{phone_id}: ERROR: {exc}"
    finally:
        try:
            shutdown.shutdown_profiles([phone_id])
        except Exception as exc:
            print(f"  {phone_id}: shutdown failed: {exc}", flush=True)


def main() -> int:
    phone_ids = sys.argv[1:]
    if not phone_ids:
        print("usage: bulk_install_instagram.py <phone_id> [...] "
              "(prefix a number with --workers=N to change concurrency, default 10)")
        return 1

    workers = 10
    phone_ids = [p for p in phone_ids if not p.startswith("--workers=")]
    for arg in sys.argv[1:]:
        if arg.startswith("--workers="):
            workers = int(arg.split("=", 1)[1])

    token = settings.get_saved_bearer_token()
    launcher = MultiloginLauncherClient(token)
    shutdown = MultiloginShutdownClient(token)

    print(f"installing on {len(phone_ids)} phone(s), {workers} at a time", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, pid, launcher, shutdown): pid for pid in phone_ids}
        for index, future in enumerate(as_completed(futures), start=1):
            print(f"[{index}/{len(phone_ids)}] {future.result()}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
