"""Name every posting failure and say which phones carry it.

    python -m adb_bot.automation.failure_taxonomy            # last 48h
    python -m adb_bot.automation.failure_taxonomy --hours 6
    python -m adb_bot.automation.failure_taxonomy --launches # capacity view

"51 failed" is not a troubleshooting result. It is one number hiding five
problems with five different owners, and the one that gets fixed is whichever
was noticed most recently rather than whichever costs most. This reads the
posting journal and answers two questions instead:

* **which causes fire, and on whose phones** -- so a fault concentrated on five
  profiles is not mistaken for a fleet-wide Instagram problem;
* **what each cause costs in launches** -- because a phone that cannot post
  still spends a launch, a two-minute boot and a slot out of the global ceiling
  every time it is retried.

The second question is the one that found the real problem on 2026-08-14. The
handle mismatch looked like 212 refusals out of 666 attempts -- bad, but a
minority. Counting launches instead showed the same five phones taking **53% of
the fleet's entire launch capacity** while producing nothing, at 4.0 launches
per confirmed post against 1.9 for everyone else. They were not merely failing;
they were the reason the healthy phones were not getting through.

Read-only: it runs `journalctl` and nothing else.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter, defaultdict

SERVICE = "adbbot-posting.service"

# (name, regex, note). The first capture group is whatever identifies the phone
# in that message -- a profile id for some, an ADB target for others, because
# the log reports each failure against whatever it had to hand.
CAUSES = (
    ("handle-not-on-phone",
     r"does not list @([A-Za-z0-9._]+)",
     "Airtable names an account the phone's switcher cannot prove"),
    ("device-never-ready",
     r"Device ([0-9.:]+) connected but never reached 'device' state",
     "ADB connected, the phone never became drivable"),
    ("launch-did-not-take",
     r"Profile (\d+) has reported 'not running'",
     "MultiLogin accepted the launch and the phone did not start"),
    ("adb-connect-refused",
     r"adb connect did not report success for ([0-9.:]+): failed to connect",
     "nothing listening on the port MultiLogin handed us"),
    ("adb-gave-up",
     r"ADB connection failed for profile (\d+) after",
     "every ADB attempt failed; the run never reached Instagram"),
    ("mlx-launcher-unreachable",
     r"Failed to launch profile (\d+):.*launcher\.mlx\.yt.*Connection refused",
     "the MultiLogin launcher itself refused the connection"),
    ("mlx-500",
     r"Failed to launch profile (\d+) -- MultiLogin-side 500",
     "their cloud; self-heals, still spends the row's retry budget"),
    ("share-unconfirmed",
     r"Workflow outcome UNCERTAIN for profile (\d+)",
     "Share was tapped, the post could not be proven in budget"),
    ("reel-tab-missing",
     r"REEL tab never became visible.*for ([0-9.:]+)",
     "composer opened without a REEL tab; media never selected"),
    ("profile-tab-unreadable",
     r"Could not open the profile tab on ([0-9.:]+)",
     "could not read which account is signed in"),
    ("checkpoint",
     r"Instagram flagged ([0-9.:]+) during .*human_verification",
     "Instagram asked for a person"),
    ("banned",
     r"Post result for .*\(profile (\d+)\): banned",
     "account suspended or disabled"),
)

LAUNCH_PATTERNS = (r"Launch(?:ed| response for) profile (\d+)",
                   r"Relaunch response for (\d+)")


def read_log(hours, service: str = SERVICE) -> str:
    return subprocess.run(
        ["journalctl", "-u", service, "--no-pager", "--since", f"{hours} hours ago"],
        capture_output=True, text=True).stdout


def names_by_profile(log: str) -> dict:
    """Profile id -> the handle it last posted as, so a report names phones."""
    out = {}
    for handle, pid in re.findall(r"Post result for ([^(]+)\(profile (\d+)\)", log):
        out[pid] = handle.strip()
    return out


def classify(log: str) -> tuple:
    """(totals, who) -- how often each cause fired, and on which phones."""
    names = names_by_profile(log)
    totals: Counter = Counter()
    who: dict = defaultdict(Counter)
    for cause, pattern, _ in CAUSES:
        for match in re.finditer(pattern, log):
            totals[cause] += 1
            key = match.group(1)
            who[cause][names.get(key, key)] += 1
    return totals, who


def outcomes(log: str) -> Counter:
    return Counter(re.findall(
        r"Post result for [^(]+\(profile \d+\): (\w+)", log))


def launches_by_profile(log: str) -> Counter:
    found: Counter = Counter()
    for pattern in LAUNCH_PATTERNS:
        found.update(re.findall(pattern, log))
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify posting failures by cause and by cost.")
    parser.add_argument("--hours", type=float, default=48)
    parser.add_argument("--launches", action="store_true",
                        help="also show where the launch budget went -- the "
                             "view that says which fault is worth fixing first")
    parser.add_argument("--top", type=int, default=3,
                        help="offenders to name per cause (default 3)")
    args = parser.parse_args(argv)

    log = read_log(args.hours)
    if not log.strip():
        print("Nothing in the posting journal for that window.")
        return 1

    result = outcomes(log)
    attempts = sum(result.values())
    done = result.get("done", 0)
    print(f"\nlast {args.hours:g}h: {attempts} post attempt(s), {done} confirmed "
          f"({done * 100 // max(attempts, 1)}%)\n")
    for outcome, n in result.most_common():
        print(f"  {n:5}  {outcome}")

    totals, who = classify(log)
    print(f"\n{'cause':26} {'count':>6}  worst offenders")
    print("-" * 88)
    for cause, n in totals.most_common():
        worst = ", ".join(f"{k} ({v})" for k, v in who[cause].most_common(args.top))
        print(f"{cause:26} {n:6}  {worst[:56]}")

    if args.launches:
        names = names_by_profile(log)
        launches = launches_by_profile(log)
        total = sum(launches.values())
        print(f"\nlaunch/relaunch calls: {total} "
              f"({total / max(done, 1):.1f} per confirmed post)")
        print(f"\n{'profile':24} {'launches':>9}  {'posted':>6}")
        print("-" * 45)
        posted_by = Counter(re.findall(
            r"Post result for ([^(]+)\(profile \d+\): done", log))
        for pid, n in launches.most_common(10):
            name = names.get(pid, pid)
            print(f"{name[:24]:24} {n:9}  {posted_by.get(name + ' ', 0):6}")
        print("\nA phone high on this list with nothing in the posted column is "
              "spending the fleet's capacity to achieve nothing -- fix that "
              "before anything lower down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
