# Multilogin X → Airtable: what we can sync, and the ID fix

> Findings from a live probe of the **correct** MLX workspace (2026-07-26). Companion to `AIRTABLE_INTEGRATION_REPORT.md`. Read-only; no data was written.

---

## 1. Workspace confirmed — and it's much bigger than Airtable

The correct token points at workspace `f03f25bc-eb13-4ec1-9cd6-80f14b3f9255`. `GET /mobile_profiles/phone/list` returns **91 mobile profiles across 9 models**:

| Model | Profiles in MLX | In Airtable now |
|---|---|---|
| Nikki | 19 (Nikki 1–19) | 15 (Nikki 1–15) |
| Jasmin | 11 (1–10 + Link) | 0 |
| Jil | 11 (1–10 + Link) | 0 |
| Katja | 5 (1–4 + Link) | 0 |
| Laila | 11 (1–11) | 0 |
| Luisa | 11 (1–10 + link) | 0 |
| Viktoria | 10 (1–11 partial + Link) | 0 |
| Rodrigo | 1 | 0 |
| Blank / Blank 1 | 12 (staging/unnamed) | 0 |
| **Total** | **91** | **~15** |

**So there is a large backfill available:** 8 models (~70 profiles) and Nikki 16–19 exist in MLX but are entirely absent from Airtable. Everything below can be pulled in one `list` call.

## 2. THE fix: Airtable's "MultiLogin Profile ID" is the `serial_no`, not the launch key

Cross-referenced all 15 Airtable Nikki IDs against this workspace:

- **15 / 15 match an MLX `serial_no`** (158697, 158698, … 211001). ✅
- **0 / 15 match the MLX API `id`.**

Confirmed truth of the two IDs MLX returns per profile:

| MLX field | Example | What it is | Airtable |
|---|---|---|---|
| `serial_no` | `158698` | 5–6 digit human serial | **= what Airtable stores in `MultiLogin Profile ID`** |
| `id` | `624354174112432228` | 18-digit API id | **what `launch` and `adb/info` actually require** — NOT in Airtable |

**Consequence:** the app cannot launch/connect using the value currently in Airtable. It must translate `serial_no → id`. Two options:
- **(A)** At run-time, call `list` once, build a `{serial_no: id}` map, and look up the id before launching. Simple, always current. *(Recommended.)*
- **(B)** Add an **`MLX API ID`** field to `Profiles (Cloning)` and store the 18-digit `id` there during the sync; launch off that field. Faster per-run, but must be kept fresh.

Either way: **keep `serial_no` as the human-facing key, add/resolve the `id` as the machine key.**

## 3. What MLX exposes per profile → where it maps in Airtable

Full field set returned by `GET /mobile_profiles/phone/list`, per profile:

```
id, serial_no, serial_name, status(2=active), created_at, updated_at, last_launched_at,
folder_id, group{id,name}, usecase_name, remark, is_*_favorite,
equipment_info{ device_brand, device_model, os_version, country_name, time_zone,
                phone_number, imei, bluetooth_mac, wifi_bssid, mac, enable_sim, net_type },
proxy{ server, port, type, username(geo embedded), password }
```

### ✅ Syncable NOW (static, one `list` call)

| MLX field | → Airtable target | Fills an empty field? |
|---|---|---|
| `serial_name` (e.g. "Nikki 1") | Profiles (Cloning) → Profile Name | matches existing |
| `serial_no` | Profiles (Cloning) → MultiLogin Profile ID | matches existing |
| `id` (18-digit) | Profiles (Cloning) → **new: MLX API ID** | **new (the launch key)** |
| `status` (2 → Active) | Profiles/Devices → Status | yes |
| `created_at` | **Accounts → Creation Date** | **yes — unblocks `lifecycle.py`** |
| `device_brand`+`device_model`+`os_version` | Devices → Phone Model / OS | yes (empty now) |
| `phone_number` | Devices → SIM Number | yes (empty now) |
| `time_zone` (e.g. Europe/Berlin) | **new field** (per-account local scheduling) | new |
| `country_name` | Proxies → Location / Geo (or Devices) | yes |
| `proxy.server:port` | Proxies → IP / Endpoint | yes (empty now) |
| proxy geo (`username`) | Proxies → Location / Geo | yes |
| (constant "Multilogin") | Proxies → Provider | yes |
| `group.name` / `folder_id` | grouping → could seed **Models** links | yes |
| `imei`, `bluetooth_mac`, `wifi_bssid`, `mac` | device fingerprint (new, optional) | if spoofing needs it |

### ⛔ NOT syncable statically (run-time only)

- **`ADB Serial / ID`, `ADB Connection`, `ADB IP:Port`** — `POST /adb/info` returns `status:"disabled"` unless the profile is launched with ADB enabled. The app captures these during a run; a pre-sync cannot.
- **`App Package Name`** — MLX does not expose it. These are MLX cloud phones (one isolated Android each), so the target app is native `com.instagram.android` — treat as a constant, not a per-profile value.

## 4. Auth note for the real sync

The token used here is a **regular token** (`isAutomation:false`, ~1 h lifetime). It worked for the probe, but an unattended sync/scheduler must use the **Workspace Automation Token** (longer-lived, higher rate limits) — see `reference-multilogin-x-api`. Don't hard-code the short token.

## 5. Recommended next steps (not yet built — per your call)

1. **Decide the ID strategy** — (A) runtime `serial_no→id` map, or (B) add an `MLX API ID` column. (A) is less to maintain.
2. **Add fields:** `Profiles (Cloning).MLX API ID` (if option B), and an `Accounts`/`Profiles` **Time Zone** field.
3. **Backfill sync (dry-run first):** for all 91 profiles → upsert `Profiles (Cloning)` (key on `serial_no`), create/link `Devices` (brand/model/OS, SIM), `Proxies` (endpoint, geo), and set `Accounts.Creation Date` from `created_at`. Then decide whether to onboard the 8 missing models or start with Nikki only.
4. ADB serial/IP stays a **run-time** write, folded into the existing launch workflow.
```
