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
   - Rules: first word = the model; **prefer a one-word model name** — the
     posting join key is literally `name.split()[0].lower()`
     (`airtable.profile_targets_by_model`), so `Anna Maria 1` posts under the
     model `anna`, and a two-word name only works if the Drive folder is named
     after the first word or an alias points at it; **unique** across the whole workspace (two
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

## Doing it now: Kathi and Katherine

State as of 2026-08-10 (from the `doctor` run recorded in commit `22dc652` and
the review rerun; **not** re-verified live while writing this): both models exist
in MultiLogin and in Drive, and neither is finished in Airtable. `Lou` also shows
up — profiles, no raw folder. Nothing below is done by any code path in this
repo; all of it is by hand, in MultiLogin and Airtable.

For **Kathi** and again for **Katherine**:

1. **MultiLogin → the model's folder.** Rename each profile's `serial_name`
   `Blank (N)` → `Kathi 1`, `Kathi 2`, … (`Katherine 1` …). Contiguous, no gaps,
   no duplicates anywhere in the workspace.
2. **Airtable → `Profiles (Cloning)`.** Find the same rows by
   **`MultiLogin Profile ID`** (that is the MLX serial `mlx-sync` wrote on
   create, and it does not change when you rename), and set **`Profile Name`** to
   the identical string. This is the step that is actually load-bearing: the
   posting join key is the first word of *this* field, and `mlx-sync` never
   rewrites it (`adb_bot/automation/mlx_sync.py:275`). Renaming only in MLX
   changes nothing.
   - **Edit the rows. Do not delete and re-sync** — they carry `Warm-up Started`,
     and new rows restart the six-day clock.
   - On each row check `MLX API ID` is filled and `Status` is empty or `Active`.
3. **Drive → `01_Raw_Videos/Kathi/`** (and `…/Katherine/`). The folder is what
   `raw_folder_no_targets` is naming, so it exists; it needs `.mp4` clips at its
   top level. No alias is needed — the folder name already equals the
   profile-name prefix.
4. **Optional:** a `Models` row named exactly `Kathi` / `Katherine`. Posting
   works without one; the dashboard calls the model "stray" until it exists.
5. **Re-run `doctor`.** `mlx_folder_unnamed` for that folder must be gone once
   step 2 is done, and `raw_folder_no_targets` once steps 2 and 3 are both done.
6. **`Lou`** is the mirror image: profiles exist, no raw folder. Either create
   `01_Raw_Videos/Lou/` and upload clips, or — if Lou's clips live in a
   differently-named folder — add that pair to `RAW_FOLDER_MODEL_ALIASES`
   (step 5 above). If Lou is parked on purpose, set its profiles' `Status` to
   `Inactive`: with no active target and no folder it drops out of the
   comparison entirely.

## How to check it worked

```bash
# names the gaps across Drive / MultiLogin / Airtable, read-only
python -m adb_bot.automation.run_loop doctor        # look for "Model inventory"

# should report variants = clips x profiles for the new model, not "skipped"
python -m adb_bot.automation.run_loop pipeline --targets profiles
```

`doctor`'s **Model inventory** check WARNs on three conditions, each of which is
one of the steps above left undone, and notes two more as INFO (they never turn
the check WARN):

| Finding | Means |
|---|---|
| `mlx_folder_unnamed` | step 3: an MLX folder whose profiles are all still `Blank (N)` |
| `raw_folder_no_targets` | steps 3/4/5: clips can be dropped where nothing will collect them |
| `targets_no_raw_folder` | step 4: phones exist, no folder to feed them |
| `no_models_row` (INFO) | step 6: cosmetic, posting still works |
| `raw_folder_model_parked` (INFO) | not a gap: every profile under that model is Inactive on purpose |

Two things to know about reading it:

* **A detail line starting `[partial: ...]` is a half-comparison.** When the raw
  source cannot be read — no `DRIVE_RAW_FOLDER_ID` / no service-account key, a
  Drive 403, or a listing that comes back empty — the two folder rules are
  **skipped**, not run against an empty list, and the line says so before it says
  anything else. Without that, an outage reported every model as missing its
  Drive folder (`model_inventory.raw_source_known`, `doctor.check_models`). The
  MLX and `Models`-row rules keep running.
* **Parking a model is not a gap.** Setting Status to Inactive on every
  `<Model> N` row is how a model is taken out of the run; its clips normally stay
  in Drive. That is reported once, as INFO, and cannot turn the check WARN.

The dashboard's Spoofing panel names any raw folder that is empty and has no
profiles behind it — **after the dashboard is redeployed.** `adbbot-site.service`
runs with `WorkingDirectory=/opt/adbbot-site`, a pinned copy; merging this branch
changes nothing on :8088 until `/opt/adbbot-site` is refreshed (see
`deploy/RUNBOOK.md`). The doctor check has no such copy — it runs from the
checkout — so it is the thing to trust right after a merge.

`run_loop pipeline` logs one
`pipeline: skipped <clip>: no MLX profiles under model '<Model>'` line per
dropped clip, and those clips now count as *due* work for the pipeline watchdog.
That does **not** mean a folder of clips going nowhere reads STALLED: the
watchdog short-circuits to OK as soon as the run encodes any variant at all
(`loop_watchdog.py`, `LoopWatchdog.observe`), so on a fleet that is spoofing for
anybody the effect is an extra clause in the pipeline's detail line. It reads
STALLED only when the whole run produced zero variants.
