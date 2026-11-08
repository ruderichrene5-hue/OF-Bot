"""Run InstagramWarmUpDay1Flow (the 10-minute scroll/stories/follow pass) on a
batch of MLX phones directly, concurrently -- no Airtable, no run_loop's
multi-day Warmup Plan machinery. For accounts that never went through the
normal Airtable-tracked onboarding (like a fresh signup batch), that
machinery has nothing to key off anyway.

Usage:
    python3 run_warmup_batch.py <phone_id> [<phone_id> ...] [--workers=N]
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_ig_session import ready_profile_mlx  # noqa: E402
from adb_bot.clients.adb import ADBClient  # noqa: E402
from adb_bot.clients.multilogin import MultiloginShutdownClient  # noqa: E402
from adb_bot.automation.flows.instagram import InstagramWarmUpDay1Flow  # noqa: E402
from adb_bot.config import settings  # noqa: E402
from adb_bot.core.logger import get_logger  # noqa: E402


def _one(phone_id: str, shutdown) -> str:
    logger = get_logger(f"warmup.{phone_id}")
    try:
        profile = ready_profile_mlx(phone_id, max_attempts=10, wait_seconds=15)
        if profile is None:
            return f"{phone_id}: phone never became ready"

        adb = ADBClient()
        connected = False
        for _ in range(5):
            if adb.connect(profile) and adb.authenticate(profile):
                connected = True
                break
        if not connected:
            return f"{phone_id}: could not establish ADB"

        result = InstagramWarmUpDay1Flow().run(profile, adb_client=adb, logger=logger)
        return f"{phone_id}: {result}"
    except Exception as exc:
        return f"{phone_id}: ERROR: {exc}"
    finally:
        try:
            shutdown.shutdown_profiles([phone_id])
        except Exception as exc:
            print(f"  {phone_id}: shutdown failed: {exc}", flush=True)


def main() -> int:
    args = sys.argv[1:]
    workers = 5
    phone_ids = []
    for arg in args:
        if arg.startswith("--workers="):
            workers = int(arg.split("=", 1)[1])
        else:
            phone_ids.append(arg)

    if not phone_ids:
        print("usage: run_warmup_batch.py <phone_id> [...] [--workers=N]")
        return 1

    token = settings.get_saved_bearer_token()
    shutdown = MultiloginShutdownClient(token)

    print(f"warming up {len(phone_ids)} phone(s), {workers} at a time "
          f"(~10 min each)", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, pid, shutdown): pid for pid in phone_ids}
        for index, future in enumerate(as_completed(futures), start=1):
            print(f"[{index}/{len(phone_ids)}] {future.result()}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
