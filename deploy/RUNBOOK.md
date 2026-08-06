# Server bring-up runbook

Getting the bot running on a server, in the order that surfaces problems
earliest. Nothing here writes to Instagram or Airtable until step 6 — every loop
runs **dry-run first**.

Rule of thumb: **never debug a loop by watching Instagram.** Run `doctor`, read
the logs, and check the Airtable Run Log. In that order.

Steps 1–3 and 7 differ by platform; the rest is identical. Linux is the primary
server target; the Windows column is kept because the desktop box still runs the
same code.

---

## 1. Install

### 1a. Get the code onto the server

The project is **not currently under version control**, so there is nothing to
`git clone` yet. Two ways:

**Direct copy** — quickest, no setup:

```bash
./deploy/sync_to_server.sh user@server            # ~0.2 MB, to /opt/adb_bot
DRY_RUN=1 ./deploy/sync_to_server.sh user@server  # preview first
```

**Or put it in git** — better once you are iterating on the server:

```bash
git init && git add . && git commit -m "Initial commit"
git remote add origin <your-remote> && git push -u origin main
# then on the server: git clone <your-remote> /opt/adb_bot
```

`.gitignore` already excludes the virtualenvs, build output, logs, and
`dev_settings.json` (which holds tokens in plain text — keep it out of git).

> **Never copy a virtualenv between machines.** It contains platform-specific
> binaries and absolute paths, so a Windows `.venv` on Linux produces an install
> that looks complete and cannot run. Always recreate it on the server.

### 1b. Install (Linux)

```bash
cd /opt/adb_bot
sudo apt install python3-venv android-tools-adb tesseract-ocr
./install_requirements.sh
```

Optional, per feature:

```bash
sudo apt install python3-tk    # only to run the desktop UI over X11/VNC
sudo apt install ffmpeg        # only for the separate video_spoofer project
```

### Windows

```bash
git clone <repo> C:\ADB_Bot
cd C:\ADB_Bot
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### What each dependency is for

| Dependency | Needed by | Install | Check |
|---|---|---|---|
| **adb** | every device loop | `android-tools-adb` | `adb version` |
| **Tesseract** (+ `eng` data) | OCR verification in the flows | `tesseract-ocr` | `tesseract --version` |
| **tkinter** | the desktop UI **only** — no loop imports it | `python3-tk` (pip *cannot* install this) | `python3 -c "import tkinter"` |
| **FFmpeg** | the `video_spoofer` project, not this repo | `ffmpeg` | `ffmpeg -version` |
| **MultiLogin X client** | launching every profile | MLX Linux client | see below |
| Python wheels | everything | `requirements.txt` | — |

`doctor` verifies all of these at once — prefer it over checking by hand.

> **Python 3.12 or 3.13.** Both resolve to prebuilt wheels with no compiler
> needed. Do **not** re-pin `numpy`/`opencv-python` to exact old versions in
> `requirements.txt`: `numpy==1.26.4` has no 3.13 wheel and forces a from-source
> build that fails without a C toolchain. The file's ranges are deliberate.

> **glibc 2.28 or newer** (Ubuntu 20.04+/Debian 10+). numpy and OpenCV now ship
> `manylinux_2_28` wheels.

> On Linux `requirements.txt` installs **opencv-python-headless**. Do not swap it
> for plain `opencv-python`: that wheel links `libGL.so.1`, which a server with no
> desktop doesn't have. The import is inside a `try/except`, so it fails
> *silently* — `cv2` becomes `None` and the blue-button classification and OCR
> confirmations quietly stop verifying anything while flows still report success.
> `doctor`'s **Imaging** check exists to catch exactly this. (The headless wheel
> is self-contained: it needs only libc, libstdc++ and libz.)

### The video_spoofer project (pipeline only)

The spoofing pipeline shells out to a **separate repo with its own virtualenv**,
pointed at by `SPOOFER_ROOT` and `SPOOFER_PYTHON`. It is not installed by
anything here and brings its own requirements plus FFmpeg. Skip it entirely if
you are not running the pipeline loop; `doctor` reports it as WARN when unset.

### The MultiLogin agent

Every loop that touches a phone POSTs to the MultiLogin agent on
`launcher.mlx.yt:45001` — **on this machine**. Install the MultiLogin X Linux
client on the server and keep it running; a working cloud token is not enough.
`doctor`'s **MultiLogin agent** check tells the two apart: `MultiLogin` PASS with
`MultiLogin agent` FAIL means the token is fine and the agent isn't running.

#### Keep it running: `adbbot-mlx-agent.service`

Do **not** hand-start the agent. It has died unsupervised twice — once to an OOM
— and each time every phone launch failed for about an hour before a human read
the logs, because a loose process reports to nobody and does not survive a
reboot. `deploy/systemd/adbbot-mlx-agent.service` supervises it: `Restart=always`,
ordered after the display, and `WantedBy=multi-user.target` so it comes back on
its own.

```bash
sudo deploy/systemd/install_mlx_agent.sh      # writes the unit; does not start it
```

It is deliberately not part of `install_units.sh` — that script installs the loop
*timers*, and its `--apply` flag means "start posting for real".

**Taking over from an already-running hand-started agent.** The loose process
owns `:45001`, so it has to go first or the unit crash-loops trying to bind:

```bash
sudo kill $(pgrep -f '^/opt/mlx/agent\.bin$')   # stop the loose one
sleep 5 && ss -lntp | grep 45001                # expect NO output
sudo systemctl enable --now adbbot-mlx-agent    # supervised from here on
sleep 10 && ss -lntp | grep 45001               # expect a LISTEN line
```

Killing the agent also drops every phone it has open, so do this between posting
slots. Afterwards:

```bash
systemctl status adbbot-mlx-agent
journalctl -u adbbot-mlx-agent -f
```

The listener on `:45001` is the agent's `launcher-linux_amd64.bin` **child**, not
`agent.bin` itself — so `ss -lntp | grep 45001` naming a different binary than the
unit's `ExecStart` is correct, not a mismatch.

## 2. Credentials

| Variable | Value |
|---|---|
| `MULTILOGIN_TOKEN` | MLX **workspace Automation Token** — not a regular token (those expire in ~1h) |
| `AIRTABLE_TOKEN` | Airtable Personal Access Token |
| `AIRTABLE_BASE_ID` | The base to run against (test base until you're confident) |

Optional, same file/mechanism:

| Variable | Value |
|---|---|
| `ADBBOT_MAX_LIVE_PROFILES` | How many phones may be open **across every loop at once** (default 12). This is the real ceiling: `--max-concurrent` is per loop, so posting (10) + warmup (10) + recheck (1) would otherwise be 21 phones. A loop that cannot get a place skips that profile and picks it up next tick — "skipped … global ceiling" in its log. Lower it if the box is tight on RAM; 12 assumes ~215 MB per live phone on 15 GB. |

### Linux

A systemd service inherits **nothing** from your shell. Put the tokens in an
environment file, or the timers will fail with "no token" even though running the
loop by hand works:

```bash
sudo install -d -m 750 /etc/adbbot
sudo tee /etc/adbbot/env >/dev/null <<'EOF'
MULTILOGIN_TOKEN=...
AIRTABLE_TOKEN=...
AIRTABLE_BASE_ID=...
EOF
sudo chmod 600 /etc/adbbot/env
```

### Windows

Set them as **machine-level** environment variables (System Properties →
Environment Variables → System). Machine-level matters: with "run when logged
off" the tasks run as SYSTEM and won't see *user* variables.

Either platform: entering them in the app under **Dev controls** works too, for
loops running as the logged-on user.

## 3. Pipeline paths

In the app: **Pipeline** button. Set what applies, then press **Check** — it runs
the pipeline preflight right there and tells you what's wrong. Headless, set the
same values as environment variables (`RAW_VIDEOS_DIR`, `SPOOFED_VIDEOS_DIR`,
`DRIVE_RAW_FOLDER_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `SPOOFER_PYTHON`,
`SPOOFER_ROOT`) and verify with `doctor`.

| Field | Notes |
|---|---|
| Raw videos folder | local raw source; skip if using Drive |
| Spoofed output folder | where variants are written (server disk) |
| Google Drive folder id | from the folder URL, `…/folders/<THIS>` |
| Drive service-account JSON | path to the key file |
| Spoofer interpreter | the `video_spoofer` venv python (`bin/python` on Linux) |
| Spoofer project folder | the `video_spoofer` repo root |

For Drive: create a Google Cloud **service account**, download its JSON key, and
**share the `01_Raw_Videos` folder with the service account's email** (viewer is
enough). Then `pip install google-api-python-client google-auth`.

## 4. Preflight

```bash
.venv/bin/python -m adb_bot.automation.run_loop doctor
```

Fix every `[FAIL]` before going further. `[WARN]` only limits the loop it belongs
to (e.g. no Drive configured → the pipeline can't run, everything else can).
Exit code is non-zero only when something is genuinely broken.

## 5. Dry-run each loop, in this order

Each command plans and prints, but launches nothing and writes nothing.

```bash
.venv/bin/python -m adb_bot.automation.run_loop mlx-sync
```
**Expect:** `MLX returned N profile(s)`, then a create/update/unchanged plan.
First run on a fresh base wants to create many profiles — that's correct.
Add `--skip-staging` to ignore MLX's unnamed "Default folder" profiles.

```bash
.venv/bin/python -m adb_bot.automation.run_loop pipeline
```
**Expect:** `would spoof <file> for N account(s) under <Model>`. If it says
*no active accounts under model X*, the accounts' **Model** link or Lifecycle
Stage is wrong in Airtable — not a bot problem.

```bash
.venv/bin/python -m adb_bot.automation.run_loop warmup
```
**Expect:** one line per account with its due flows. "nothing due" is normal if
every account is past Day 4 (they're in the posting phase).

```bash
.venv/bin/python -m adb_bot.automation.run_loop posting
```
**Expect:** one `POST <account> -> <launch id> | video=… | caption=…` per due
row. Empty means the `Posting Queue` has no Pending rows due yet — check that
Airtable's own 5×/day automations are actually creating them.

```bash
.venv/bin/python -m adb_bot.automation.run_loop recovery
```
**Expect:** usually nothing. It lists profiles whose `Needs Human Check` you have
cleared and whose dead posts it would hand back to the retry loop.

### Resuming a profile you have fixed

Clearing **`Profiles (Cloning).Needs Human Check`** is how you tell the bot you
looked at a profile and fixed it. The `recovery` loop (every 15 min) is what acts
on it — before it existed, clearing the box changed nothing at all.

What it does: finds the profile's `Failed` queue rows that the retry pass will
never touch again (`Retries Exhausted`, or the retry count at the limit, or an
Issue Type only a person can clear), sets them back to `Failed - Needs Retry`
with the count at 0, and closes out the profile's `Flagged At` / `Issue Reason`,
leaving a line in Issue Notes. The retry loop then picks them up on its next tick
and applies its own **ledger check**, so a reel that may already be live still
never goes out twice. Recovery makes a row eligible to be *considered*; it never
decides that a post may go out.

Freeing those rows matters for a second reason: a `Failed` row **owns its clip**
(`VARIANT_HELD_BY`). Leave the dead rows in place and the variants stay locked to
a row nobody will ever act on, so the profile only ever posts newly spoofed
media.

Two things it deliberately leaves to you:

* **Status.** A profile that is un-flagged but `Inactive` is reported with a
  warning and stays out of every loop. Status is your park switch — the bot will
  not un-park a profile because a different checkbox changed.
* **The checkbox itself.** You cleared it; the bot does not write it back.

### Warming up new profiles (the `Created` tag)

New accounts exist as MultiLogin profiles long before anyone writes an Accounts
row, so the account-driven warm-up above cannot see them. `--targets profiles`
warms up **the MLX profiles tagged `Created`** instead:

```bash
.venv/bin/python -m adb_bot.automation.run_loop warmup --targets profiles
.venv/bin/python -m adb_bot.automation.run_loop warmup --targets profiles --apply
```

The tag is the whole selection — set it in MultiLogin, and the profile joins the
warm-up on the next run; change it (to `Active / Posting`) and it drops out.
`--warmup-tag` picks a different one.

**Day 1 is the first run, not the MLX creation date.** The bot stamps
`Profiles (Cloning).Warm-up Started` the first time it warms a profile up and
counts from there, so a profile created a fortnight ago still starts at day 1.
Clear that date to run a profile through the warm-up again.

Two things it will tell you rather than guess about:

* *"has no Profiles (Cloning) row yet — run the mlx-sync loop"* — MLX has the
  profile, Airtable doesn't. `mlx-sync` runs nightly; run it by hand to pull a
  batch created today.
* *"Airtable Status is Inactive"* — the profile is parked. Set it to Active if
  it should be warming up.

Reels are never scheduled by the profile warm-up, even on a Warmup Plan day that
asks for one: a reel needs a spoofed variant, a variant needs a model, and a
`Created` profile has neither yet. Posting is the Posting Queue's job once the
profile has been named and assigned to a model.

## 6. First real run — one account

Don't start the scheduler yet. Pick a single test account and run it by hand:

1. In the app, select **one** profile.
2. Use **Run from Airtable** (or `run_loop warmup --apply`).
3. Watch the phone, then confirm Airtable got a **Run Log** row and
   `Last Run` / `Last Result` on the account.

Only once that round-trips should you schedule anything.

## 7. Schedule

Two different schedules, and they are easy to confuse:

* **When the loops run** — systemd timers / Task Scheduler, below. This is how
  often the bot *looks* for work.
* **When a model's reels go out** — Airtable, `Models` → **Reel Post Times**.
  This is the posting schedule itself, and it is edited in the base, not here.

### Reel post times, per model (Airtable)

Pick the wall-clock times (Europe/Berlin) a model posts at. Each picked time
becomes one Posting Queue row per account/profile of that model, filled with an
unused Ready spoof variant — so **the number of times picked is that model's
reels per day**.

**Leave it empty and the model posts whenever a video is ready.** That is the
flexible mode, not an off switch: the queue loop gives it a row as soon as it
has an unused variant, at most 7 a day and no closer than 2 hours apart.
`Models` → **Reels Per Day** overrides the 7 for one model;
`--anytime-gap` / `--anytime-max` override both defaults for one run.

The `--slots` grid is only the fallback for a base whose `Models` table has no
`Reel Post Times` field at all. Once the field exists, every model's own pick
wins, and `--no-model-times` is the escape hatch that puts everything back on
one grid for a single run.

The app's **Scheduler** window works on both platforms and drives whichever
backend is present. Enable the loops, set intervals, tick **dry-run**, press
**Apply**. Let it fire once, read the logs, then untick dry-run and Apply again.

| Loop | Suggested |
|---|---|
| Posting | every 10 min |
| Pipeline | every 20 min |
| Warmup | every 360 min (or 3×/day) |
| MLX sync | daily (1440) |
| Cleanup | daily (1440) |

### Linux — systemd timers

```bash
sudo ./deploy/systemd/install_units.sh          # dry-run timers
sudo ./deploy/systemd/install_units.sh --apply  # once the logs look right
```

Each loop becomes `adbbot-<loop>.service` (a `Type=oneshot` unit) plus
`adbbot-<loop>.timer`.

```bash
systemctl list-timers 'adbbot-*'            # what's installed, when it next fires
journalctl -u adbbot-posting.service -f     # follow one loop
sudo ./deploy/systemd/install_units.sh --remove
```

systemd won't start a service that's still active, so a timer tick during a long
run is dropped — the same protection as Task Scheduler's `IgnoreNew`. Nothing
there stops *different* loops colliding on one phone; the per-profile locks in
`core/locks.py` handle that on both platforms.

### Windows — Task Scheduler

**Run when logged off** runs the tasks as SYSTEM so they survive RDP
disconnects — requires starting the app **as administrator**, and credentials
must be machine-level env vars (step 2). On Linux there's no equivalent toggle:
system units always run with nobody logged in.

Equivalent from PowerShell: `.\deploy\scheduler\install_tasks.ps1 [-Apply]`.

---

## Troubleshooting

| Symptom | Likely cause | Do this |
|---|---|---|
| `401` from MultiLogin | regular token expired (~1h) | Use the workspace **Automation Token** |
| `401/403` from Airtable | PAT lacks base/table scopes | Re-issue the PAT with this base |
| Timers run but every loop says "no token" | systemd service has no environment | Populate `/etc/adbbot/env` (step 2), then `sudo systemctl daemon-reload` |
| Profiles never launch, token checks out | MultiLogin agent not running on the server | `doctor` → **MultiLogin agent**; start the MLX X client |
| Flows pass but never actually verify | `cv2` failed to import (libGL) | `doctor` → **Imaging**; install `opencv-python-headless` |
| `pip install` starts compiling numpy for minutes, or fails with a compiler error | someone re-pinned numpy/opencv to versions with no wheel for this Python | Restore the ranges in `requirements.txt`; never pin `numpy==1.26.4` on 3.13 |
| UI won't start: `ModuleNotFoundError: tkinter` | `python3-tk` missing (pip can't provide it) | `sudo apt install python3-tk` |
| Captions/bios arrive truncated or empty | *fixed* — was POSIX shell mangling; `tests/test_shell_argv.py` guards it | If it recurs, check nothing reintroduced `shell=True` |
| Loop logs "profiles busy in another loop" | posting + warmup overlapped | Normal — it retries next cycle. Persistent? See stuck locks below |
| A profile is *always* busy | a crashed run left a lock | Locks self-expire after 45 min; `doctor` lists held locks, or delete files in the locks dir |
| Posting plan is empty | no Pending+due `Posting Queue` rows | Check Airtable's slot-creating automations |
| Pipeline: "no active accounts under model X" | Account's Model link / Lifecycle Stage | Fix in Airtable — accounts must be **Active** and linked to that Model |
| Pipeline plans but makes no files | spoofer not configured | Set Spoofer python/root (step 3); `doctor` verifies it |
| CPU pinned at ~100%, nothing looks wrong | almost always a spoof encode holding every core | Dashboard → **Top CPU use** names the process and the clip, **Spoofing** says how much is left. An encode is not a fault |
| Raw clips sit in Drive and never get spoofed | no *active* profile under that model | Dashboard → **Spoofing** flags these separately from the queue; check the folder name against the model, and Profiles → Status |
| Flow fails right after launch | ADB didn't connect | Confirm the profile launched in MLX; `adb devices` |
| Account stopped being picked up | flagged by ban/verification detection | Check `Ban & Flag History` + `Needs Human Verification`; unticking it resumes the account |
| Timer never fires | unit not enabled, or service still active | `systemctl list-timers 'adbbot-*'`, `systemctl status adbbot-<loop>.service` |
| Scheduled task never runs (Windows) | wrong user context / bad path | Task Scheduler → History; check "run when logged off" + machine-level env vars |

**Where things are**

| | Linux | Windows |
|---|---|---|
| Loop logs | `logs/loop_<name>.log` (rotating, 5 MB × 5) + `journalctl -u adbbot-<loop>` | `logs/loop_<name>.log` |
| Settings + locks | `~/.adb_bot/` | `%APPDATA%\ADB Bot\` |
| Scheduled jobs | `/etc/systemd/system/adbbot-*` | `taskschd.msc`, named `ADBBot-*` |
| Tokens | `/etc/adbbot/env` | machine env vars |

**Running the UI on the server** — it needs a desktop session. Over SSH use
`ssh -X`, or attach via VNC, then `./run_ui.sh`. Everything except the UI runs
headlessly.

**The dashboard, two ways** — `adbbot-report.service` renders the report on
`127.0.0.1:8080`, per request, no password: reach it with
`ssh -N -L 8080:localhost:8080 <box>`. `adbbot-site.service` is the same page on
`0.0.0.0:8088` behind a password, rebuilt every five minutes and served from
memory so a public port cannot spend the Airtable quota. Both carry a **Top CPU
use** table (which process is burning the box, and what it is doing) and a
**Spoofing** section (what is on the encoder right now, and how many variants
are queued behind it, per model) — the two questions a pinned box provokes.
A **Schedules** tab holds every time on the page: when each loop next runs
(with the hour for the daily ones) and when each model posts, beside the stock
each has to post *with*. Mind the two clocks — loop times are the server's
(UTC here), posting slots are `Europe/Berlin`; the page says so when they
differ.
Note that the queue half costs a Drive listing plus two Airtable reads per
rebuild; on the loopback dashboard, which renders per request, that is the
slowest thing on the page. Its password hash and
cookie secret live in `/etc/adbbot/env`
(`ADBBOT_SITE_PASSWORD_HASH`, `ADBBOT_SITE_SECRET`); make new ones with
`python -m adb_bot.automation.site --hash-password`, then
`systemctl restart adbbot-site`. It serves plain HTTP — put Caddy in front of it
if the box ever gets a domain name. The site runs from a pinned copy at
`/opt/adbbot-site` (see `DEPLOYED_FROM`), so redeploy it after changing the
report code:

```
git -C /root/adb_bot archive HEAD | tar -x -C /opt/adbbot-site && systemctl restart adbbot-site
```

### The recovery loop runs from a pinned copy (2026-08-06)

`adbbot-recovery` is the one loop whose unit does *not* point at `/root/adb_bot`.
It runs from `/opt/adbbot-recovery`, pinned the same way the site is. The reason
is drift, not design: the loop landed on `worktree-daily-run-report`, the live
checkout was still behind that branch, and it had uncommitted work in flight, so
it could not be fast-forwarded without overwriting someone mid-edit. A timer
pointing at a checkout that has no `recovery_runner.py` would just fail every
15 minutes, so the code went where the timer could reach it.

This is a bridge, not a second home. Redeploy it the same way as the site:

```
git -C <checkout> archive HEAD | tar -x -C /opt/adbbot-recovery
```

and once `/root/adb_bot` carries the recovery loop, `install_units.sh --apply`
rewrites the unit to point back at the checkout with no extra step. Nothing has
to be undone first — the installer overwrites units in place, which is the end
state to aim for. `/etc/systemd/system/adbbot-recovery.service` carries the same
note at the top.

Worth knowing why this mattered: between the loop being written and the timer
existing, four profiles had their Needs Human Check cleared by a person and
nothing picked them up. Clearing the box is only half a handoff; the loop is the
other half, and until it is scheduled the box is a checkbox that does nothing.

**Safety net:** any loop can be reverted to dry-run at any time — Scheduler
window, tick dry-run, Apply (or re-run the installer without `--apply`). That
stops all device and Airtable writes without uninstalling anything.
