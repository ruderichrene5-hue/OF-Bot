# Onboarding a new model

Written 2026-08-10, after two models (10 + 5 real phones, warmed up for six days)
turned out to be invisible to every posting code path with no error anywhere.

**The one thing to know:** the join key between a raw video and the phones that
post it is the **first word of `Profiles (Cloning).Profile Name`** in Airtable —
not the MultiLogin folder, not the MLX profile name, not the `Models` table.
`mlx-sync` writes `Profile Name` **only when it creates a row**
(`adb_bot/automation/mlx_sync.py:275`); it never updates it. So renaming a
profile in MultiLogin after its Airtable row exists changes nothing. That rename
has to be done in **both** systems, by hand.

## The checklist

1. **MultiLogin folder** named exactly as the model, e.g. `Kathi`.
2. **MultiLogin profiles** in that folder, `status=2` (Active), tagged `Created`
   so the warm-up loop picks them up.
3. **Profile names — in BOTH systems. This is the step that gets missed.**
   - MultiLogin: `serial_name` → `Kathi 1` … `Kathi 10`.
   - Airtable `Profiles (Cloning).Profile Name`: the same names, edited by hand
     on the same rows.
   - Rules: first word = the model; **unique** across the whole workspace (two
     targets with the same handle overwrite each other's variant file —
     `spoof_pipeline.finalize_variant`); must not contain the word `Link`
     (`airtable.py:1317` treats those as the link-in-bio account, not a target).
   - **Rename the rows; do not delete and re-sync.** The rows carry
     `Warm-up Started`, and re-creating them restarts the warm-up clock.
   - Check each row has `MLX API ID` filled and `Status` empty or `Active`.
     Status is the only on/off switch a profile-driven target has.
4. **Google Drive folder** `01_Raw_Videos/<Model>/`, spelled exactly as the
   profile-name prefix, one level deep, `.mp4` files at its top level. Upload
   clips.
5. **Alias** — only if the Drive folder name differs from the profile prefix
   (today: `Corina` holds Nikki's clips, `Mandy` holds Luisa's). This is
   configuration now, not code:

   ```
   # /etc/adbbot/env
   RAW_FOLDER_MODEL_ALIASES="corina=Nikki,mandy=Luisa,<folder>=<Model>"
   ```

   Keys are lower-cased folder names, values are the model as Airtable spells
   it. **An override replaces the whole map** — include the existing pairs, or
   Nikki's and Luisa's content stops routing. Unset means the built-in default
   (`corina=Nikki,mandy=Luisa`). No redeploy of `/opt/adbbot-warmup` needed.
6. **Airtable `Models` row** — optional for posting, and nothing in the codebase
   creates one. Without it the model still posts (a missing schedule means
   flexible mode, ≤7/day, ≥2h apart), but the dashboard calls it "stray" and
   `mlx-sync` cannot link its Devices to a Model.
7. **`Reel Post Times`** — optional; blank is flexible mode. Every model in the
   base is flexible today.
8. **Captions, schedule entries, systemd units** — nothing to do. None of them
   are per-model.

## How to check it worked

```bash
# names the gaps across Drive / MultiLogin / Airtable, read-only
python -m adb_bot.automation.run_loop doctor        # look for "Model inventory"

# should report variants = clips x profiles for the new model, not "skipped"
python -m adb_bot.automation.run_loop pipeline --targets profiles
```

`doctor`'s **Model inventory** check WARNs on three conditions, each of which is
one of the steps above left undone:

| Finding | Means |
|---|---|
| `mlx_folder_unnamed` | step 3: an MLX folder whose profiles are all still `Blank (N)` |
| `raw_folder_no_targets` | steps 3/4/5: clips can be dropped where nothing will collect them |
| `targets_no_raw_folder` | step 4: phones exist, no folder to feed them |
| `no_models_row` (INFO) | step 6: cosmetic, posting still works |

The dashboard's Spoofing panel names any raw folder that is empty and has no
profiles behind it, and `run_loop pipeline` now logs one
`pipeline: skipped <clip>: no MLX profiles under model '<Model>'` line per
dropped clip. A folder of clips with nowhere to go makes the pipeline watchdog
read STALLED instead of IDLE.
