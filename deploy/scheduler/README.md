# Scheduled loops (server deployment)

The bot runs as independent loops, each fired by the OS scheduler: **systemd
timers** on Linux, **Task Scheduler** on Windows. Every loop is one command that
runs once and exits:

```
python -m adb_bot.automation.run_loop <posting|warmup|pipeline|mlx-sync> [--apply]
```

**Dry-run is the default** — it plans and logs but launches nothing and writes
nothing device-side. Add `--apply` for real work. This is what makes the loops
safe to test before the phones/media are wired.

## Loops & cadence

| Loop | Command | Cadence | What it does |
|---|---|---|---|
| Posting | `run_loop posting` | every 10 min | Post due `Posting Queue` rows (Spoof Variant + Caption) → write Post Status / Issue Type / Run Log |
| Warmup | `run_loop warmup` | 3×/day | Run lifecycle-due flows for Warmup accounts |
| Pipeline | `run_loop pipeline` | every 20 min | Scan raw videos → spoof one variant per active account → `Spoof Variants` rows |
| MLX sync | `run_loop mlx-sync` | every 3 h | Diff MultiLogin against Airtable → create Devices/Proxies/Profiles for new phones, and reconcile an existing row's Profile Name, MLX Folder and MLX Tags (never Status) |
| Cleanup | `run_loop cleanup` | daily (04:00) | Delete finished media older than 2 days (`--max-age-days`) |

## Resource limits

The server is one machine driving many phones, so the loops are capped:

| Limit | Default | Override |
|---|---|---|
| Phones running at once | **10** | `--max-concurrent` |
| Variants produced per pipeline run | **20** | `--max-variants` |
| ffmpeg encodes at once | **1** (serial by design) | — |
| Retention grace period | **2 days** | `--max-age-days` |

Posting/warmup launch and run in batches, so a backlog of 80 accounts never
boots 80 profiles at once. The pipeline stops at the variant cap *between
videos* (never mid-video, so no account is left without its variant) and picks
up the rest next run.

Cleanup only removes media that is provably finished with: clips in a `used/`
folder, and spoofed variants whose Airtable row says **Used**. A variant that is
still `Ready`/`Pending` is never deleted — a scheduled post needs it.

## Install

Register in dry-run first (safe — plans only), inspect the logs, then re-run
with the apply flag.

### Linux — systemd timers

```bash
sudo ./deploy/systemd/install_units.sh
sudo ./deploy/systemd/install_units.sh --apply
sudo ./deploy/systemd/install_units.sh --remove   # to uninstall
sudo ./deploy/systemd/install_units.sh --status   # what's installed, next fire
```

Units are named `adbbot-<loop>.service` / `.timer` in `/etc/systemd/system`.
Override the interpreter with `PYTHON=/path/to/bin/python`, and run the loops as
a non-root account with `SERVICE_USER=adbbot`.

### Windows — Task Scheduler

```powershell
.\deploy\scheduler\install_tasks.ps1
.\deploy\scheduler\install_tasks.ps1 -Apply
.\deploy\scheduler\install_tasks.ps1 -Remove   # to uninstall
```

The script auto-detects the repo root and `.venv\Scripts\python.exe`. Pass
`-Python <path>` if your interpreter is elsewhere. Tasks are named `ADBBot-*` in
Task Scheduler (`taskschd.msc`).

Either platform: the app's **Scheduler** window does the same thing through
`adb_bot.automation.scheduling`, which picks the backend for you.

## Credentials & config

The loops read the same settings the app uses. On Linux put these in
`/etc/adbbot/env` (a systemd service inherits nothing from your shell); on
Windows set them as **machine environment variables**. Saving them in the app's
dev settings also works for loops running as the logged-on user.

| Var | Purpose |
|---|---|
| `MULTILOGIN_TOKEN` | MLX token — use the **workspace Automation Token** (long-lived) for unattended runs |
| `AIRTABLE_TOKEN` | Airtable Personal Access Token |
| `AIRTABLE_BASE_ID` | Airtable base id (defaults to the test base) |
| `RAW_VIDEOS_DIR` | pipeline: root of raw videos (`01_Raw_Videos`) — used when Drive isn't configured |
| `SPOOFED_VIDEOS_DIR` | pipeline: output root on local disk (`02_Spoofed_Videos`) |
| `DRIVE_RAW_FOLDER_ID` | pipeline: Google Drive folder id of `01_Raw_Videos` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | pipeline: path to the Drive service-account key file |
| `SPOOFER_PYTHON` | pipeline: interpreter for the `video_spoofer` project (its venv) |
| `SPOOFER_ROOT` | pipeline: root folder of the `video_spoofer` project |

## Raw videos from Google Drive

The pipeline reads raw videos from **Google Drive** when both
`DRIVE_RAW_FOLDER_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` are set; otherwise it
falls back to the local `RAW_VIDEOS_DIR`. Spoofed outputs always stay on the
server's local disk (`SPOOFED_VIDEOS_DIR`).

Setup:

1. In Google Cloud, create a **service account** and download its JSON key to the
   server. Put the path in `GOOGLE_SERVICE_ACCOUNT_JSON`.
2. **Share the `01_Raw_Videos` Drive folder** with the service account's email
   (viewer is enough — the bot only reads).
3. Copy the folder id from its Drive URL
   (`https://drive.google.com/drive/folders/<THIS_PART>`) into `DRIVE_RAW_FOLDER_ID`.
4. Install the optional deps:

```bash
pip install google-api-python-client google-auth
```

Drive layout mirrors the local one — one subfolder per model, videos inside:
`01_Raw_Videos/{Model}/*.mp4`. Files are downloaded to a temp path only while
being spoofed and deleted right after, so the server never mirrors the whole
raw library.

## Logs

Each loop writes to `logs/loop_<name>.log` under the repo (rotating, 5 MB × 5).
On Linux the same output also goes to the journal:

```bash
journalctl -u adbbot-posting.service -f
```

Check these after the first scheduled fire to confirm the plans look right
before switching to apply mode.
