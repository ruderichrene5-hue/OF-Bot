"""What the scheduled loops are, independent of how any OS schedules them.

A leaf module so both backends (Windows Task Scheduler in `scheduler_admin`,
systemd timers in `systemd_admin`) and the `scheduling` façade can share it
without importing each other.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TASK_PREFIX = "ADBBot-"
UNIT_PREFIX = "adbbot-"

# The loops `run_loop` can run today, in the same order as `run_loop.LOOPS`. A
# test keeps this from naming a loop the CLI cannot run; the other direction is
# handled at runtime (`scheduling.cli_loops`), so a loop that lands in the CLI
# first is still scheduled -- that gap is how `recheck` stayed unscheduled and
# left Posting Queue rows parked in `Verifying` forever.
LOOPS = ("posting", "recheck", "warmup", "pipeline", "mlx-sync", "cleanup")

# Loops that are being written now and are not CLI commands yet. They are listed
# so their cadence is already agreed and `install_units.sh` picks them up the
# moment they land -- but nothing installs a timer for a loop the CLI cannot run
# (that would just fail every tick), so these are skipped until then.
PLANNED_LOOPS = ("queue", "retry")

# Everything an unattended server should have scheduled.
#
# This tuple is not a wish list: `scheduling.installable_loops()` filters it by
# what the CLI can run and `install_units.sh` installs and `systemctl enable
# --now`s the result, with `--apply` appended by `loop_arguments` for anything
# outside READ_ONLY_COMMANDS. So a name landing here is, in practice, an armed
# writing timer on the next routine run of the installer -- whatever that run
# was actually for.
RECOMMENDED_LOOPS = ("pipeline", "queue", "posting", "recheck", "retry", "recovery",
                     "warmup", "warmup-state", "mlx-sync", "cleanup", "digest",
                     "doctor", "reap-phones", "second-accounts", "verification",
                     "mlx-minutes", "mlx-guard")

# Loops the CLI can run that are deliberately NOT scheduled: arming them has to
# be a separate, deliberate act by a person, not a side effect of running the
# installer for some unrelated reason.
#
# `issue-tags` writes to the shared MultiLogin workspace -- a system this bot
# does not own, where a person's hand-applied tags live and where a wrong
# `unassign` is not recoverable from Airtable. Its safety rests on a per-profile
# ledger under the app data dir; a timer would put the first `--apply` of that
# ledger's whole life on a machine at 15-minute intervals with nobody watching.
# It is written to be run by hand (dry-run first, then `--apply`) until somebody
# has read a few days of its plans and decided to arm it -- and arming it means
# installing its unit deliberately, not adding a name to the tuple above.
#
# Everything else about a manual-only loop stays defined here: it keeps its
# entry in RECOMMENDED_INTERVALS (the cadence a hand-installed unit should use,
# and what `loop_watchdog.stall_after_seconds` reads), DESCRIPTIONS and
# WHAT_IT_DOES.
MANUAL_ONLY_LOOPS = ("issue-tags",)

# Recommended cadence in minutes. The UI can override per loop; these are what
# `install_units.sh` installs. Ordered by the flow a reel goes through, because
# the intervals only make sense relative to each other:
RECOMMENDED_INTERVALS = {
    # Spoofing is the expensive step (ffmpeg encodes, capped at 20 variants a
    # run). Half-hourly keeps a same-day buffer of variants ahead of posting
    # without stacking encodes -- and an overrun is dropped, not queued.
    "pipeline": 30,
    # Fills the Posting Queue. Must run several times per posting slot so a slot
    # never opens on an empty queue; cheap (Airtable only), so 15 min.
    "queue": 15,
    # Slots are fixed wall-clock times, so this only has to be fine-grained
    # enough to catch one soon after it opens. 5 min bounds the lateness of a
    # post at ~5 min, which is inside the jitter the slots already tolerate.
    "posting": 5,
    # Rows become eligible RECHECK_DELAY_SECONDS (15 min, airtable.py) after an
    # unproven post. Matching that delay means a row waits at most one extra
    # tick: eligible at +15, rechecked by +30. Faster would burn profile
    # launches on rows that are not eligible yet; slower leaves `Verifying` rows
    # sitting there, which is the failure this schedule exists to fix.
    "recheck": 15,
    # Resets retryable Failed rows to Pending. Failures are mostly transient
    # (device offline, MLX hiccup); half-hourly recovers them well within the
    # posting day while still spacing out retries against a genuinely broken
    # account instead of hammering it.
    "retry": 30,
    # Resumes a profile a person has un-flagged: hands its dead queue rows back
    # to the retry pass above. Paced to the person, not the machine -- somebody
    # clearing a checkbox expects the bot to notice within minutes, and the run
    # is one filtered Airtable read that almost always returns nothing. Runs
    # ahead of `retry` in the flow, so a row it revives is picked up on retry's
    # next tick rather than waiting a whole cycle.
    "recovery": 15,
    # Lifecycle day plan (Day 1-4) spreads actions across the day; hourly gives
    # the plan enough ticks to place them and to pick up a profile that only
    # became due mid-day.
    "warmup": 60,
    # Publishes each profile's warm-up day into Airtable and onto its MLX tags.
    # The warm-up tick already calls this itself, so the timer is the backstop
    # for the cases the tick cannot cover: a run that died before it could
    # publish, a day rolling over with no profile due, and a person editing a
    # tag by hand. Half-hourly because it is a reconciler -- two Airtable list
    # calls and one MLX list, then nothing at all unless something moved.
    "warmup-state": 30,
    # Mirrors Airtable's Needs Human Check onto the MultiLogin `Issue` tag.
    # MANUAL_ONLY_LOOPS: this cadence is what a hand-installed unit should use,
    # not something `install_units.sh` will arm. Paced to the person, like
    # `recovery`: somebody clearing the checkbox expects the tag to follow within
    # minutes, and somebody opening the workspace expects this morning's flags to
    # be on it. 15 min is one MLX list plus one Airtable read, then nothing at
    # all unless a flag moved.
    "issue-tags": 15,
    # Full MultiLogin -> Airtable inventory sweep: every new profile, and every
    # rename, folder move and tag change on an existing one.
    #
    # It ran nightly while it only ever *created* rows -- a phone that appeared
    # during the day could wait until 23:30 without anyone noticing. Now that it
    # also reconciles name, folder and tags it is the only thing that carries a
    # person's edit in the MultiLogin workspace across to Airtable, and a day of
    # lag there is a day of the dashboard, the spoof pipeline and the queue
    # working from a name or a model folder that is no longer true. 3 h is the
    # cadence asked for, and the run is cheap and phone-free: two MLX list calls
    # and one Airtable list, then nothing at all unless something moved.
    "mlx-sync": 180,
    # Disk housekeeping (old used media). Once a night, off-peak (04:00).
    "cleanup": 1440,

    # Once a day, and the only loop whose whole output is a chat message. It
    # reads Airtable and sends; it changes nothing, so there is no harm in it
    # running on a schedule the way the rest do.
    "digest": 1440,
    # Preflight, on a timer rather than only when a person asks. Its checks are
    # what the loops silently depend on -- the MLX agent listening, Airtable
    # readable, Drive reachable -- and on 2026-08-04 a dead agent went unnoticed
    # for ~1 h because nothing probed it. 30 min bounds that to one tick; the
    # run is cheap (no phones, no encodes, bounded Airtable reads).
    "doctor": 30,
    # Close phones no loop owns any more. Nothing else reaps them: the workflow
    # that would have closed them died with the run, and a stale phone keeps a
    # MultiLogin cloud session alive on a real account for hours. It only acts
    # on phones older than the 45-minute lock TTL, so a 20-minute cadence never
    # races a live run and still catches a leak within the hour.
    "reap-phones": 20,
    # Watches the phones carrying two Instagram accounts. Every failure it looks
    # for is silent -- a second account never scheduled, one clip on both
    # accounts, a phone stuck on the wrong account -- and all of them leave a
    # queue row saying Posted, so nothing else will ever raise them. Hourly:
    # the state it reads changes at posting speed, the run is three Airtable
    # reads and a log grep, and its alerts are about a trend rather than a tick.
    "second-accounts": 60,
    # Answers Instagram's verification challenge on the profiles flagged for it
    # -- the other end of `issue-tags`, which until now had no other end but a
    # person running a CLI. A profile flagged at 02:00 stayed flagged.
    #
    # 30 minutes, and the pacing is not about latency. This is the only loop
    # that spends money and the only one that drives a phone through somebody
    # else's challenge flow, so the cadence is set by what a bad half-hour may
    # cost: two profiles a tick, six numbers a tick, twelve numbers a rolling
    # day (`LOOP_EXTRA_ARGS` below), which is ~$2.40 against a wallet that has
    # held about ten dollars. Faster would not clear the backlog any sooner
    # either -- a worked profile has a six-hour cool-off, so the population of
    # things this loop *can* do is refilled by flags arriving, not by ticks.
    "verification": 30,
    # Four hours. The balance moves at roughly 2,000 minutes a day across the
    # fleet, so nothing is learned by asking more often -- and the alert
    # de-duplicates itself anyway, so a tighter beat would only cost log noise.
    # It is also what catches a fleet that has stopped launching, which is the
    # more urgent of its two alerts; 2026-08-18 went unnoticed for three hours.
    "mlx-minutes": 240,
    # The guard stops the fleet when MultiLogin proxy traffic or minutes run
    # out, so its cadence is the worst case for how long the loops keep paying
    # for launches that cannot work. 2026-08-21 burned ~2h of them before a
    # person noticed. 10 min bounds that, and the check is nearly free: four
    # small GETs through the gateway, whose own traffic is the thing being
    # protected -- more often would spend the allowance to watch it.
    "mlx-guard": 10,
}

# Historical name -- the UI, both backends and install_units.sh read this.
DEFAULT_INTERVALS = RECOMMENDED_INTERVALS

# Cadence for a loop nobody has given a recommendation for. Deliberately slow:
# an unknown loop should still get scheduled rather than error out, but it
# should not be the thing that hammers Airtable.
FALLBACK_INTERVAL_MIN = 30

# For a daily task (interval a whole number of days) we need a start time.
# `mlx-sync` is deliberately absent: it moved off a daily schedule to every 3 h,
# so it has no start time any more. Leaving a stale entry here is harmless to
# the interval maths but would have the installer keep writing `OnCalendar=
# *-*-* 23:30:00` into its timer.
DEFAULT_DAILY_START = {"warmup": "08:00", "cleanup": "04:00",
                       # Before the VAs start, so the backlog is the first thing
                       # in the topic rather than something they scroll back for.
                       "digest": "08:00"}

# A loop that overruns this is considered wedged and is killed, so the next
# cycle gets a clean start. Matches the Windows ExecutionTimeLimit of PT2H.
MAX_RUNTIME_SECONDS = 2 * 60 * 60

DESCRIPTIONS = {
    "posting": "ADB bot posting loop (Posting Queue -> IG)",
    "recheck": "ADB bot recheck loop (Verifying -> Posted/Failed)",
    "queue": "ADB bot queue loop (variants -> Posting Queue rows)",
    "retry": "ADB bot retry loop (retryable Failed -> Pending)",
    "recovery": "ADB bot recovery loop (un-flagged profiles -> retryable again)",
    "warmup": "ADB bot warmup loop (lifecycle Day 1-4)",
    "warmup-state": "ADB bot warm-up state publisher (Run Log -> Airtable + MLX tags)",
    "issue-tags": "ADB bot issue-tag mirror (Needs Human Check -> MLX 'Issue' tag)",
    "pipeline": "ADB bot spoofing pipeline (Drive/raw -> Spoof Variants)",
    "mlx-sync": "ADB bot MultiLogin->Airtable profile sync",
    "cleanup": "ADB bot cleanup loop (old used media)",
    "digest": "ADB bot daily digest (backlog -> Telegram)",
    "doctor": "ADB bot preflight checks (alerts on failures)",
    "reap-phones": "ADB bot orphan-phone reaper (closes abandoned phones)",
    "second-accounts": "ADB bot two-account watch (both accounts of a phone posting?)",
    "verification": "ADB bot verification loop (flagged profiles -> challenge answered)",
    "mlx-minutes": "ADB bot MultiLogin minute check (warns before the fleet stops)",
    "mlx-guard": "ADB bot MultiLogin guard (stops the fleet when proxy traffic "
                 "or minutes run out)",
}

# The same loops in words, for a person rather than a unit file. DESCRIPTIONS
# above is the systemd `Description=` line -- a label, written in the vocabulary
# of the table names it moves rows between, which only helps someone who already
# knows what those tables are. This is what the dashboard shows when you ask
# what a loop does, so each one says what it acts on, what it produces, and the
# thing that would be surprising if you assumed otherwise.
WHAT_IT_DOES = {
    "posting": "Takes the Posting Queue rows that are due, launches each profile's "
               "phone and posts its reel. Runs up to 10 phones at once, so a slot "
               "of 40 posts is not 40 times one post.",
    "recheck": "Settles posts that could not be proven at the time. The phone is "
               "reopened ~15 minutes later and the row moves from Verifying to "
               "Posted or Failed — without this they sit in Verifying forever.",
    "queue": "Turns spoofed variants into Posting Queue rows at each model's "
             "scheduled times. Runs several times per slot so a slot never opens "
             "on an empty queue.",
    "recovery": "Resumes a profile after you clear its Needs Human Check. Its "
                "dead posts are handed back to the retry loop and their clips "
                "freed -- without it, clearing the box changes nothing.",
    "retry": "Puts retryable Failed rows back to Pending. Most failures are "
             "transient — a device offline, an MLX hiccup — so they are worth "
             "one more go; after 3 attempts the row is left for a person.",
    "warmup": "Works a new account through its Day 1-4 lifecycle plan: the "
              "browsing and liking that make an account look used before it is "
              "asked to post anything.",
    "warmup-state": "Writes each profile's warm-up day into Airtable and onto its "
                    "MultiLogin tag, reading the Run Log rather than the calendar — "
                    "so a profile whose day has advanced without the runs landing "
                    "shows the day it actually finished, not the day it is on.",
    "issue-tags": "Puts a profile's Needs Human Check onto its MultiLogin 'Issue' "
                  "tag, and takes it off again when you clear the box — so the "
                  "phones waiting on you are visible in the workspace you fix them "
                  "in. It only ever removes tags it put there itself; an 'Issue' "
                  "somebody applied by hand is left alone.",
    "pipeline": "Spoofs new raw clips from Drive — one unique encode per active "
                "profile, because two accounts posting the same file is what gets "
                "them flagged. The expensive loop: it is the one that pins the CPU.",
    "mlx-sync": "Copies the MultiLogin workspace into Airtable every 3 hours: new "
                "phones become Profile rows, and an existing row's name, folder "
                "and tags are made to match MultiLogin again. MultiLogin wins "
                "every time — edit a phone there, not here. The one thing it "
                "never touches is Status, which is your park switch and stays "
                "yours.",
    "digest": "Sends one message a day to the VAs' Telegram topic: how many phones "
              "are waiting on a person, broken down by reason, how long the oldest "
              "has been waiting, and what posted in the last 24 hours. It reads "
              "Airtable and writes nothing; if Telegram is not configured it does "
              "nothing at all. The per-phone alert is a different loop -- this one "
              "is the backlog, so a problem nobody picked up gets louder with age.",
    "cleanup": "Deletes media the bot has finished with (older than 2 days). It is "
               "the only thing standing between the spoofed-video folder and a "
               "full disk.",
    "doctor": "The preflight checks, on a timer instead of only when somebody asks: "
              "MLX agent listening, Airtable readable, Drive reachable, spoofer "
              "configured. It raises the alert rather than waiting to be noticed.",
    "second-accounts": "Watches the phones that carry two Instagram accounts. Every way "
                       "that can go wrong is silent — a second account never scheduled, "
                       "one clip posted on both accounts, a phone stuck on the wrong "
                       "one — and each leaves a queue row saying Posted. This is what "
                       "raises them.",
    "reap-phones": "Closes phones no loop owns any more. Nothing else does — the "
                   "run that would have closed them died — and a leaked phone holds "
                   "a MultiLogin session open on a real account for hours.",
    "mlx-guard": "Stops the posting loops when MultiLogin runs out of proxy "
                 "traffic or minutes, instead of letting them launch phones "
                 "into a wall for hours — which is what happened on "
                 "2026-08-18, 08-20 and 08-21. There is no API for either "
                 "balance, so it asks the proxy gateway directly: an empty "
                 "allowance answers 402. Alerts on Telegram when it stops the "
                 "fleet, when the traffic estimate drops near empty, and again "
                 "when the gateway comes back.",
    "mlx-minutes": "Counts the MultiLogin phone-minutes the fleet has spent this "
                   "billing period, out of its own launcher log, because there is "
                   "no API that reports the balance. Warns on Telegram when what "
                   "is left falls below the threshold, and separately when two "
                   "consecutive posting runs attempted launches and none "
                   "succeeded \u2014 which is what an empty balance looks like "
                   "from the inside, and what nothing noticed for three hours on "
                   "2026-08-18. Reads logs only; it launches nothing and spends "
                   "nothing.",
    "verification": "Answers Instagram's verification challenge on the phones flagged "
                    "for it: launches each one, reads the screen, and where the ask "
                    "is an SMS code or an image captcha, rents a number or buys a "
                    "solve and types it in. A phone it genuinely clears is untagged, "
                    "which hands it back to posting. This is the only loop that "
                    "spends money, so it is bounded twice — a few numbers per tick "
                    "and a dozen per day — and it never touches a profile already "
                    "recorded as banned or held for a supervised run, because a "
                    "suspended account still renders a feed that looks healthy.",
}


def task_name(loop: str) -> str:
    """Windows Task Scheduler name."""
    return f"{TASK_PREFIX}{loop}"


def unit_name(loop: str, kind: str = "service") -> str:
    """systemd unit name. `kind` is 'service' or 'timer'."""
    return f"{UNIT_PREFIX}{loop}.{kind}"


def repo_root() -> Path:
    # A checkout that is only *running* the code -- the public site serves from
    # its own worktree so a session cleanup cannot take the website with it --
    # still has to read the live box's logs. Without this it would report on its
    # own empty `logs/` and swear the machine had done nothing all day.
    override = os.environ.get("ADBBOT_REPO_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # adb_bot/automation/schedule_spec.py -> repo root is two parents up from adb_bot.
    return Path(__file__).resolve().parents[2]


def python_exe() -> str:
    """Prefer the project venv's interpreter; fall back to the current one.

    The venv layout differs by platform: Windows puts the interpreter in
    `Scripts\\python.exe`, POSIX in `bin/python`.
    """
    root = repo_root()
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


# Flags a scheduled loop needs that its CLI default does not give it. A timer
# gets no arguments beyond the loop name, so a default that is wrong for THIS
# deployment makes the timer a silent no-op rather than an error.
#
# `--targets profiles`: both loops default to `accounts`, i.e. Airtable Accounts
# at Lifecycle Stage Active. Here the real targets are the ~90 MLX profiles --
# only 11 Accounts rows exist and none is Active -- so on the default the
# pipeline builds no variants and the queue finds no targets, forever, quietly.
#
# `--slots` is now only a FALLBACK (2026-08-06). Posting times live in Airtable,
# per model: `Models.Reel Post Times`. As soon as that field exists in the base,
# every model's own pick decides its day and this grid is not consulted at all --
# a model with nothing picked posts whenever it has a spoofed video instead. The
# times below therefore only still apply to a base without the field.
#
# The original note, for why these three times were chosen:
# `--slots`: TONIGHT'S GRID ONLY (2026-08-05). 140 fresh variants finish
# spoofing at ~17:30 Berlin (the Nikki/Corina clips encode at ~29s each, far
# slower than the ~6s of the other models), and `due_slots` only creates rows
# for slots that have already come round -- so starting on the standing grid
# mid-afternoon would create 09:00/11:00/13:00/15:00 all at once, and the
# posting planner's only gate is `Scheduled DateTime <= now`, with no
# per-account cool-down. These three forward-looking times clear the spoof run
# by ~30 min and keep the runs a real two hours apart.
#
# TO REVERT to the standing 09:00-21:00 grid (DEFAULT_SLOT_TIMES in
# queue_runner): drop the "--slots" pair below and re-run
# `sudo deploy/systemd/install_units.sh --apply`. Do that tomorrow morning --
# left in place, these three times are the only slots that will ever fill.
#
# `verification` is the one whose defaults are deliberately *tighter* on a timer
# than on the command line. Run by hand, a person is watching the balance and
# can afford `--limit 5`; run every 30 minutes with nobody watching, the numbers
# below are the whole safety argument, so they are written here rather than left
# to the module defaults:
#   --limit-profiles 2      two phones a tick. Each takes ~2-4 minutes and
#                           competes with posting for ADBBOT_MAX_LIVE_PROFILES,
#                           so this is about the fleet as much as the wallet.
#   --max-numbers 4         one tick cannot spend more than ~$0.80.
#   --max-numbers-per-day 12  the ceiling that actually bounds a timer: ~$2.40
#                           across every tick in a rolling 24 hours.
#   --min-balance 1.00      refuse to start rather than discover an empty
#                           wallet two launches in.
LOOP_EXTRA_ARGS = {
    "pipeline": ("--targets", "profiles"),
    "queue": ("--targets", "profiles", "--slots", "18:00,20:00,22:00"),
    "verification": ("--limit-profiles", "2", "--max-numbers", "4",
                     "--max-numbers-per-day", "12", "--min-balance", "1.00"),
}


# Commands with no dry-run/apply split. `doctor` only reads, so `--apply` would
# be noise on the command line and a lie in the journal.
READ_ONLY_COMMANDS = ("doctor",)


def loop_arguments(loop: str, apply: bool = True) -> str:
    """The argument string handed to the interpreter for one loop."""
    extra = "".join(f" {arg}" for arg in LOOP_EXTRA_ARGS.get(loop, ()))
    apply = apply and loop not in READ_ONLY_COMMANDS
    return (f"-m adb_bot.automation.run_loop {loop}"
            + (" --apply" if apply else "") + extra)


def description(loop: str) -> str:
    return DESCRIPTIONS.get(loop, f"ADB bot {loop} loop")
