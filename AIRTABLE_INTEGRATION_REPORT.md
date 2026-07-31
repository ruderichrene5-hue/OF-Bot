# ADB_Bot ↔ Airtable Integration Spec

> **Audience:** an LLM/engineer wiring the ADB_Bot app to the "OFM Agency OS" Airtable base.
> **Scope:** every Airtable Web API endpoint needed to (a) READ what to do / which profile & device to target, and (b) WRITE results, metrics, and events back into the sheets.
> **Generated:** 2026-07-26 from the live base schema.

---

## 0. TL;DR — the data flow

```
Airtable (control + dashboard)  ──READ──►  ADB_Bot app (executor)  ──WRITE──►  Airtable (results)
                                                │
                                                └── Multilogin X (device/profile layer)
```

The app is the executor. Airtable is the brain/dashboard. Multilogin X is the phone/profile layer.

**The core "run a post" join chain** (read top-to-bottom, all in Airtable):

```
Posting Queue  (Post Status = Pending, Scheduled DateTime ≤ now)
  ├─► Target Account      → Accounts        (lifecycle, automation mode, status)
  │        └─► Profile    → Profiles (Cloning)  → MultiLogin Profile ID  + App Package Name
  │                 └─► Device → Devices    → ADB Serial/ID + ADB Connection + ADB IP:Port
  ├─► Spoof Variant       → Spoof Variants  → Spoofed File Path (the video to upload)
  └─► Caption             → Caption Pool    → Caption Text
```

After execution the app writes back: `Posting Queue.Post Status`, `Retry Count`, `Issue Type`; creates a `Performance Log` row; and on a ban/flag, creates a `Ban & Flag History` row + updates `Accounts.Status`/`Lifecycle Stage`.

---

## 1. Connection & auth

| Item | Value |
|---|---|
| **Base ID** | `appL9q0XcNFtP4fYV` |
| **Base name** | OFM Agency OS |
| **API root** | `https://api.airtable.com/v0/{baseId}/{tableIdOrName}` |
| **Metadata root** | `https://api.airtable.com/v0/meta/bases/{baseId}/...` |
| **Auth header** | `Authorization: Bearer <PERSONAL_ACCESS_TOKEN>` |
| **Body header** | `Content-Type: application/json` (writes only) |

**PAT scopes required:** `data.records:read`, `data.records:write`, and `schema.bases:read` (needed to read select-choice IDs). The token must be granted access to this specific base.

**Rate limit:** 5 requests/second **per base**. Exceeding it returns `429` and locks the base for 30 s. Serialize writes and add a small delay when batching. Prefer batch endpoints (up to 10 records/call).

**Prefer table IDs over names** in the URL — names change, IDs don't. `tableIdOrName` may be either; IDs are given per table below. Use `returnFieldsByFieldId=true` on reads and field-IDs in write bodies to be immune to column renames (see §6).

> **Config today:** the app stores `airtable_token` / `airtable_base_id` / `airtable_table_name` in `adb_bot/config/settings.py` (or env `AIRTABLE_TOKEN` / `AIRTABLE_BASE_ID` / `AIRTABLE_TABLE_NAME`). `airtable_table_name` currently defaults to `"Profiles"`, which **does not exist** in this base — see §7 Gap 1.

---

## 2. Airtable Web API — the 6 endpoints you'll use

All paths are relative to `https://api.airtable.com/v0`.

| # | Purpose | Method & path |
|---|---|---|
| 1 | **List records** (query a table) | `GET /{baseId}/{tableId}` |
| 2 | **Get one record** | `GET /{baseId}/{tableId}/{recordId}` |
| 3 | **Create records** (≤10) | `POST /{baseId}/{tableId}` |
| 4 | **Update records** (≤10, partial) | `PATCH /{baseId}/{tableId}` |
| 5 | **Delete records** (≤10) | `DELETE /{baseId}/{tableId}?records[]=rec...` |
| 6 | **Read schema / select choices** | `GET /meta/bases/{baseId}/tables` |

### 2.1 List records — query parameters

| Param | Meaning |
|---|---|
| `filterByFormula` | Airtable formula string, e.g. `{Post Status}='Pending'`. URL-encode it. |
| `fields[]` | Restrict returned fields (repeat param per field). Speeds up + shrinks payload. |
| `sort[0][field]` / `sort[0][direction]` | e.g. field=`Scheduled DateTime`, direction=`asc`. |
| `maxRecords`, `pageSize` (≤100) | Limits. |
| `offset` | Pagination cursor — pass the `offset` returned by the previous page until absent. |
| `view` | Query within a saved view (applies the view's filters/sort). |
| `cellFormat` | `json` (default) or `string`. |
| `returnFieldsByFieldId=true` | Return keys as field IDs instead of names (rename-proof). |

**Pagination pattern (the app already does this in `list_ready_records`):** loop, carrying `offset` from the response into the next request until the response has no `offset`.

### 2.2 Write bodies

Create / update share the same shape (`PATCH` = partial update, leaves other fields untouched):

```json
{
  "records": [
    { "id": "recXXXXXXXXXXXXXX", "fields": { "Post Status": "Posted" } }
  ],
  "typecast": true
}
```

- **Create** (`POST`): omit `id` on each record.
- **Update** (`PATCH`): include `id`. Use `PUT` only if you want to clear unspecified fields (destructive — avoid).
- `typecast: true` lets Airtable coerce/auto-create values by name (e.g. create a new select option, or match a linked record by primary-field text). Handy but it can silently create junk options — see §6.

### 2.3 curl templates

```bash
# READ: pending posts, soonest first
curl -G "https://api.airtable.com/v0/appL9q0XcNFtP4fYV/tblGk1drpW496oJeR" \
  -H "Authorization: Bearer $AIRTABLE_TOKEN" \
  --data-urlencode "filterByFormula={Post Status}='Pending'" \
  --data-urlencode "sort[0][field]=Scheduled DateTime" \
  --data-urlencode "sort[0][direction]=asc"

# WRITE (update): mark a post Posted
curl -X PATCH "https://api.airtable.com/v0/appL9q0XcNFtP4fYV/tblGk1drpW496oJeR" \
  -H "Authorization: Bearer $AIRTABLE_TOKEN" -H "Content-Type: application/json" \
  -d '{"records":[{"id":"recXXXX","fields":{"Post Status":"Posted"}}]}'

# WRITE (create): a Performance Log row
curl -X POST "https://api.airtable.com/v0/appL9q0XcNFtP4fYV/tbl8J7MsnM4M7ELiA" \
  -H "Authorization: Bearer $AIRTABLE_TOKEN" -H "Content-Type: application/json" \
  -d '{"records":[{"fields":{
        "Account":["recACCOUNTID"],
        "Log Date":"2026-07-26",
        "Followers Total":12450,"Views 24h":83000,"Bio Link Clicks":210}}]}'
```

---

## 3. Table catalog (IDs)

| Table | Table ID | Role in integration |
|---|---|---|
| **Accounts** | `tblLjqdiHagP4n3V6` | HUB. R: lifecycle/automation/target. W: status, ban notes, needs-verify. |
| **Profiles (Cloning)** | `tblnKMvdWNwZgz494` | R: **MLX profile id + package name** + device link. W: status. |
| **Devices** | `tblP0Bm7bRNTn2AhA` | R: **ADB serial + connection + ip:port**. |
| **Posting Queue** | `tblGk1drpW496oJeR` | R: what/when to post. W: post status, retry, issue type. |
| **Spoof Variants** | `tblE5lJ6D5KdODr9J` | R: **spoofed file path** (media). W: status. |
| **Caption Pool** | `tblTp2FIytPYaLK8C` | R: caption text/category/language. |
| **Performance Log** | `tbl8J7MsnM4M7ELiA` | W: create daily metrics rows. |
| **Ban & Flag History** | `tbltFTmsf966q4XMl` | W: create on ban/flag detection. |
| **Content Pipeline** | `tblNyNxlvDafKhQYX` | R (optional): raw video + spoof status. |
| **Models** | `tblPJfTdjIH8NfPMB` | Reference (human-owned). |
| **Proxies** | `tblhEXsoAHKarNbsL` | Reference / MLX-sync target. |

---

## 4. Field maps (the important tables)

Legend — **Dir**: `R` app reads, `W` app writes, `—` human/reference. Types are Airtable field types. Select fields list their **valid write values**.

### 4.1 Accounts — `tblLjqdiHagP4n3V6`  (the hub: 1 row = 1 IG account)

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Name | `fldfFCNbcfWBEUb0Y` | singleLineText | R | primary; account handle/label |
| Model | `fldVTOXzT0stdR4yK` | link→Models | R | |
| Profile | `fldkDfoDdJgoFSPt8` | link→Profiles (Cloning) | R | **→ MLX profile id / device** |
| Lifecycle Stage | `fldhzIXjJCwMUUaxN` | singleSelect | R/W | `New`, `Warmup`, `Active`, `Paused`, `Banned` |
| Automation Mode | `fldVNJzpu3V0wqsFR` | singleSelect | R | `Posting`, `Warmup`, `Paused` |
| Daily Target (Posts) | `fldc4nLXamwndCIoy` | number | R | |
| Creation Date | `fldFTajUR7HGrL8b5` | date | R | campaign day-0 for lifecycle math |
| Days Since Creation | `fldCLBCLJg9AhZlZf` | formula | R | derived |
| Account Email | `fldYxloIi6sWNS5Yg` | email | R | |
| Status | `fldPGQL8vyNTnqsbw` | singleSelect | R/W | clean values: `Todo`, `In progress`, `Done` (⚠ polluted, see §6) |
| Ban / Flag Notes | `fld2x1LgVbSWdiwzS` | multilineText | W | free text on incident |
| Needs Human Verification | `fldSBru11XzlVmYaW` | checkbox | W | set `true` when IG demands manual verify |
| Posting Queue | `fldP4hvhQpPFICqtR` | link→Posting Queue | R | |
| Performance Log | `fldS4EfeCoZ3YR224` | link→Performance Log | R | |
| Ban & Flag History | `fldFW8hICjag9rWK5` | link→Ban & Flag History | R | |
| Spoof Variants | `fldQ0BaqUPLS38JKa` | link→Spoof Variants | R | |

### 4.2 Profiles (Cloning) — `tblnKMvdWNwZgz494`  (the Multilogin join key lives here)

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Profile Name | `fldc6tEg271ZrHhYQ` | singleLineText | R | primary |
| **MultiLogin Profile ID** | `fldlQwpnyWUWtFSqK` | singleLineText | R | **the ID passed to Multilogin launch/ADB-info** |
| **App Package Name** | `fldduweWyPvlLM95R` | singleLineText | R | Android pkg of the cloned IG instance (e.g. `com.parallel.space.instagram123`) — ADB targets this exact instance |
| Device | `fldmaY6yG1ELrfr2W` | link→Devices | R | **→ ADB serial/ip** |
| App Instance Type | `fldhdAxuXPfAT2tbH` | singleSelect | R | `Original App`, `Cloned App` |
| Clone Slot | `fldPYPxZletQD8Z4a` | number | R | |
| Cloner Tool | `fldh6iw2dGY9syLaI` | singleSelect | R | `Native (Dual Messenger etc.)`, `App Cloner`, `Clonely Cloner`, `Super Clone`, `Parallel Space`, `MultiLogin`, `None` |
| Spoofed Device ID | `fldso5XZU30p1JKqz` | singleLineText | R | |
| Status | `fldbU1AD46RGevrTQ` | singleSelect | R/W | `Active`, `Inactive` |
| Accounts | `fld7DRjRal3ncZ1Mk` | link→Accounts | R | |

### 4.3 Devices — `tblP0Bm7bRNTn2AhA`  (how ADB connects)

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Device ID | `fld0qXIQNq3OsCaxI` | singleLineText | R | primary |
| **ADB Serial / ID** | `fldJAo9X7WKL0YUdr` | singleLineText | R | what `adb devices` shows — target this device |
| **ADB Connection** | `fldYGcgxSrsO6v6SO` | singleSelect | R | `USB` or `WiFi` |
| **ADB IP:Port** | `fld4HhnOr2VTxWvNb` | singleLineText | R | only for WiFi-ADB, e.g. `192.168.1.50:5555` |
| Phone Model / OS | `fldGcOUALPlsZHAbT` | singleLineText | R | |
| SIM Number | `fldPnJDTaoascfVsm` | phoneNumber | R | |
| Status | `fld4ETCM3DYWaboco` | singleSelect | R/W | `Active`, `Inactive`, `Burned` |
| Model | `fldN5U1c22hUj5keZ` | link→Models | R | |
| Proxies | `fldmtO5QpZWlCHzpP` | link→Proxies | R | |
| Profiles (Cloning) | `fldzzbGhtfywZmbkH` | link→Profiles (Cloning) | R | |

### 4.4 Posting Queue — `tblGk1drpW496oJeR`  (the work queue)

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Name | `fldueX5ZyI6NFhHzU` | singleLineText | R | primary |
| Target Account | `fld9BDSmhFEqvGVFa` | link→Accounts | R | **→ profile/device** |
| Scheduled DateTime | `fldgsmk9rMeA0rRAX` | dateTime | R | when to fire (ISO 8601, UTC) |
| Spoof Variant | `fldkLrfBOmIU4mFdI` | link→Spoof Variants | R | **→ media path** |
| Caption | `fldEWGBiH4TYVGuCr` | link→Caption Pool | R | **→ caption text** |
| Post Status | `fld2eFcjR5rdqT3ll` | singleSelect | R/W | `Pending`, `Posted`, `Failed` |
| Retry Count | `fldVgeJJHGuvSQiAc` | number | R/W | increment on failure |
| Issue Type | `fldbPkRSKuUkQErp8` | singleSelect | W | `None`, `Failed - Needs Retry`, `Banned / Blocked`, `Human Verification Required`, `Other` |
| Status | `fldjPBClZNOW5CiE7` | singleSelect | — | Kanban col: `Todo`/`In progress`/`Done` (⚠ polluted) |

### 4.5 Spoof Variants — `tblE5lJ6D5KdODr9J`  (the media to upload)

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Variant ID | `fldEOPRbU4dpVZGx3` | singleLineText | R | primary |
| **Spoofed File Path** | `fld8tYW6DM3PIfuva` | singleLineText | R | the video file the app pushes+posts |
| Source Content | `fldj0FsxmitiyzFW1` | link→Content Pipeline | R | |
| Target Account | `fldrk7kl4jABEuQt7` | link→Accounts | R | |
| Spoof Method / Script Version | `fldmWYQQS3SCiiig9` | singleLineText | R | |
| Status | `fld4kLkxqVSQr5aJk` | singleSelect | R/W | `Pending`, `Ready`, `Used`, `Failed` — set `Used`/`Failed` after posting |
| Created Date | `flddd1kHWDYHNRN1E` | date | R | |
| Posting Queue | `fldUbe4JifW58PypX` | link→Posting Queue | R | |

### 4.6 Caption Pool — `tblTp2FIytPYaLK8C`

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Caption ID | `fldmbWFK1Owr0LGdz` | singleLineText | R | primary |
| **Caption Text** | `fldOrEiYNPZvTpAKj` | multilineText | R | the text to type into IG |
| Category | `fldCxSoWKa5ZSzQbt` | singleSelect | R | `Rate Me`, `Would You Date Me`, `Get To Know Me`, `This Or That`, `Engagement CTA`, `Fun / Compatibility`, `Confession`, `Compliment Fishing`, `Hot Take`, `Caption Game` |
| Language | `flddTSoH0J5DYgylU` | singleSelect | R | `German`, `English` |
| Active | `fldomiYey5htXkkE2` | checkbox | R | only pull `Active=true` |
| Posting Queue | `fld90CO926BrnswqU` | link→Posting Queue | R | |

### 4.7 Performance Log — `tbl8J7MsnM4M7ELiA`  (create one row per account per day)

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Name | `fldXopHOc6PCs6QLH` | singleLineText | W | primary; e.g. `"{account} 2026-07-26"` |
| Account | `fldtjxMB7j0OqBhAT` | link→Accounts | W | `["recAccountId"]` |
| Log Date | `fldkyjDULTBa8JS15` | date | W | `YYYY-MM-DD` |
| Followers Total | `fldEBqGaKXev9znDT` | number | W | scraped from profile |
| Views 24h | `fldcZmUVkA45sFk7o` | number | W | scraped |
| Bio Link Clicks | `fldGBsYYjnepsvxmW` | number | W | scraped (from insights) |

### 4.8 Ban & Flag History — `tbltFTmsf966q4XMl`  (create on detection)

| Field | Field ID | Type | Dir | Notes |
|---|---|---|---|---|
| Date | `fldvjpMs2UFJ7R3Bd` | date | W | primary |
| Account | `fld4TbfvyaXW4kc5E` | link→Accounts | W | `["recAccountId"]` |
| Event Type | `fldvZchMmqEEe5w4A` | singleSelect | W | `Shadowban`, `Full Ban`, `Action Block`, `Warning`, `Appeal Filed`, `Resolved` |
| Notes | `fldPVohmMGFOOQOS0` | multilineText | W | what was observed |
| Resolved | `fldp8ugUT0a2kXxbj` | checkbox | W | |

### 4.9 Content Pipeline — `tblNyNxlvDafKhQYX` (optional read)

Primary `Name` `fld3cD6JskIcevZWl`; `Model` `fldGVomJ2xuYtwJRU` (link); `Raw Drive Link` `fldaNrbfa1GR9wAlz` (url); `Spoof Status` `fldTRrPRdV1veNRId` singleSelect `Needs Spoofing`/`Spoofed`/`Failed`; `Spoof Variants` `fldF1tvAnxAv9nuIz` (link).

### 4.10 Proxies — `tblhEXsoAHKarNbsL` (reference / MLX-sync target)

Primary `Proxy ID` `fldYZsK0KDzTpmYJ2`; `Provider` `fldwrluTo0TdU0fZI`; `IP / Endpoint` `fld2it60kEIsExm4B`; `Location / Geo` `fld7Gg90CTwijA6fB`; `Assigned Device` `fldTo3mGPWhfIs4e5` (link); `Status` `fldmIQXiUhvnv5QAo` singleSelect `Active`/`Rotated`/`Inactive`.

---

## 5. Task recipes — exact call per integration action

Each recipe = the endpoint + method + params/body. `{PAT}` = bearer token.

### READ recipes (get the work / profile info)

**R1 — Get due posts** (what to run now)
`GET /appL9q0XcNFtP4fYV/tblGk1drpW496oJeR`
- `filterByFormula` = `AND({Post Status}='Pending', IS_BEFORE({Scheduled DateTime}, NOW()))`
- `sort[0][field]=Scheduled DateTime&sort[0][direction]=asc`
- Returns each row's `Target Account`, `Spoof Variant`, `Caption` as **arrays of record IDs** → resolve with R2–R5.

**R2 — Resolve an Account** (lifecycle + profile link)
`GET /appL9q0XcNFtP4fYV/tblLjqdiHagP4n3V6/{recAccountId}`
- Read `Lifecycle Stage`, `Automation Mode`, `Daily Target (Posts)`, `Creation Date`, and `Profile` (record-id array).

**R3 — Resolve the Profile** (Multilogin key + device)
`GET /appL9q0XcNFtP4fYV/tblnKMvdWNwZgz494/{recProfileId}`
- Read `MultiLogin Profile ID`, `App Package Name`, `Device` (record-id array). Feed `MultiLogin Profile ID` to Multilogin launch/ADB-info (§8).

**R4 — Resolve the Device** (how ADB connects)
`GET /appL9q0XcNFtP4fYV/tblP0Bm7bRNTn2AhA/{recDeviceId}`
- Read `ADB Serial / ID`, `ADB Connection`, `ADB IP:Port`.

**R5 — Resolve media + caption**
`GET /appL9q0XcNFtP4fYV/tblE5lJ6D5KdODr9J/{recSpoofVariantId}` → `Spoofed File Path`
`GET /appL9q0XcNFtP4fYV/tblTp2FIytPYaLK8C/{recCaptionId}` → `Caption Text`

**R6 — List all runnable accounts for the daily scheduler** (lifecycle-driven, no Posting Queue)
`GET /appL9q0XcNFtP4fYV/tblLjqdiHagP4n3V6`
- `filterByFormula` = `OR({Automation Mode}='Posting', {Automation Mode}='Warmup')`
- Combine with the app's `lifecycle.plan_actions_for_day(Creation Date, today)` to decide flows.

> **Optimization:** instead of R2–R5 one-by-one, batch-read a table with `filterByFormula=OR(RECORD_ID()='recA', RECORD_ID()='recB', ...)`, or query the child table by the parent link text with `filterByFormula={Target Account}='<account name>'`. One list call beats N get calls against the 5 req/s limit.

### WRITE recipes (report results back)

**W1 — Mark a post done/failed** — `PATCH /appL9q0XcNFtP4fYV/tblGk1drpW496oJeR`
```json
{"records":[{"id":"recPostId","fields":{"Post Status":"Posted"}}]}
```
On failure:
```json
{"records":[{"id":"recPostId","fields":{
  "Post Status":"Failed","Issue Type":"Failed - Needs Retry","Retry Count":1}}]}
```

**W2 — Log daily metrics** — `POST /appL9q0XcNFtP4fYV/tbl8J7MsnM4M7ELiA`
```json
{"records":[{"fields":{
  "Name":"account_x 2026-07-26","Account":["recAccountId"],
  "Log Date":"2026-07-26","Followers Total":12450,
  "Views 24h":83000,"Bio Link Clicks":210}}]}
```

**W3 — Record a ban/flag** — `POST /appL9q0XcNFtP4fYV/tbltFTmsf966q4XMl`
```json
{"records":[{"fields":{
  "Date":"2026-07-26","Account":["recAccountId"],
  "Event Type":"Action Block","Notes":"Blocked after 2nd reel","Resolved":false}}]}
```
…then **W4 — flag the account** — `PATCH /appL9q0XcNFtP4fYV/tblLjqdiHagP4n3V6`
```json
{"records":[{"id":"recAccountId","fields":{
  "Lifecycle Stage":"Paused","Needs Human Verification":true,
  "Ban / Flag Notes":"Action block 2026-07-26"}}]}
```

**W5 — Consume a spoof variant** — `PATCH /appL9q0XcNFtP4fYV/tblE5lJ6D5KdODr9J`
```json
{"records":[{"id":"recSpoofVariantId","fields":{"Status":"Used"}}]}
```

**W6 — Advance lifecycle after warm-up** — `PATCH /appL9q0XcNFtP4fYV/tblLjqdiHagP4n3V6`
```json
{"records":[{"id":"recAccountId","fields":{"Lifecycle Stage":"Active"}}]}
```

---

## 6. Gotchas (read before writing code)

1. **Linked-record fields take arrays of record IDs, not names.** `Account`, `Target Account`, `Profile`, `Device`, `Spoof Variant`, `Caption`, etc. must be written as `["recXXXX"]`. To link by human name instead, set `typecast: true` and pass the primary-field text — Airtable matches or **creates** a new linked row (can create junk; prefer record IDs).
2. **Several single-select fields are polluted** by a CSV import that turned data rows into "choices." Affected: `Accounts.Status`, `Posting Queue.Status`, `Performance Log.Status`, `Content Pipeline.Status` — they contain stray options like `App Type`, `Views 24h`, `85000`, `01_Raw_Videos/video1.mp4`. **Only write the canonical values** listed in §4 (`Todo`/`In progress`/`Done` for the generic `Status`). Better: use the purpose-built selects (`Post Status`, `Lifecycle Stage`, `Spoof Status`) and ignore the generic `Status` columns. Do **not** write these with `typecast:true` or you'll mint more junk options.
3. **Select values are case- and space-sensitive** — send the exact `name` string from §4 (not the choice ID) in a write body. Choice IDs are only for `filterByFormula` on selects via the schema.
4. **Batch limit = 10 records** per create/update/delete call. Chunk larger sets.
5. **Rate limit 5 req/s per base** → serialize, prefer list-with-filter over N gets, prefer batch writes.
6. **Rename-proofing:** pass `returnFieldsByFieldId=true` on reads and use `{ "fields": { "fldXXXX": value } }` (field IDs) on writes so column renames don't break the integration. Field IDs are in §4.
7. **Dates:** `date` fields want `YYYY-MM-DD`; `dateTime` fields want ISO 8601 (`2026-07-26T14:00:00.000Z`). Send UTC.
8. **Writes should never abort a run.** The app's existing `AirtableClient.update_record` swallows exceptions on purpose — keep that pattern (a failed write must not kill an in-progress device automation).

---

## 7. Gaps / mismatches to resolve

**Gap 1 — The current app client targets a table that doesn't exist.**
`adb_bot/clients/airtable.py` assumes a **single flat `Profiles` table** with columns `Multilogin Profile ID`, `Account`, `Flow`, `Bio`, `Caption`, `Status`, `Last Result`, `Last Run`, `Notes`, filtered by `{Status}='Ready'`. **None of that exists** in this base — the base is normalized across the 12 tables above, there is no `Flow`, `Bio`, `Last Result`, `Last Run`, or `Ready` status anywhere. Two ways forward:
  - **(A) Adapt the app** to the normalized schema (recommended): drive work from `Posting Queue` + `Accounts` lifecycle per §5; derive the "flow" from `Automation Mode`/`Lifecycle Stage` + the `lifecycle.py` planner rather than a per-row `Flow` column.
  - **(B) Add a thin control table** (e.g. a `Run Queue` table or new fields on `Accounts`) mirroring the flat model the code expects, if you'd rather not refactor the runner.

**Gap 2 — No "Flow" / "Last Run" / "Last Result" anywhere.** If you keep the app's flow-per-row model, add: `Flow` (singleSelect: `update_bio`, `warm_up_process`, `instagram_reel_upload`, `instagram_story_upload`, …), `Last Run` (dateTime), `Last Result` (singleLineText) — most naturally on `Accounts` or a new `Run Queue`.

**Gap 3 — Bio / profile-picture source.** Warm-up day-3 updates bio + picture (`lifecycle.py`), but there's no `Bio` field or picture attachment in the base. Add `Bio` (multilineText) and a picture `Attachment` field on `Accounts` (or `Models`) if these should be Airtable-driven.

**Gap 4 — MLX join key is resolved.** `Profiles (Cloning).MultiLogin Profile ID` + `App Package Name` now exist, so keying the app ↔ Multilogin by profile ID works (this was previously a blocker). Devices carry `ADB Serial/ID` + `ADB IP:Port`, so ADB targeting works too.

---

## 8. Source-data side — Multilogin X (where profile info originates)

The app already talks to Multilogin (`adb_bot/clients/multilogin/` + `clients/api.py`). These feed the Airtable `Profiles (Cloning)` / `Devices` / `Proxies` tables and the live launch/connect step:

| Purpose | Endpoint |
|---|---|
| List mobile profiles (id, name, ip, port, pwd, status, folder) | `GET https://api.multilogin.com/mobile_profiles/phone/list?page=1&page_size=100&sort=desc&order_by=created_at` |
| Get ADB credentials for profiles | `POST https://api.multilogin.com/mobile_profiles/phone/adb/info` — body `{"ids":[...]}` |
| Launch a mobile profile | `POST https://launcher.mlx.yt:45001/api/v1/mobile_phone/launch` — body `{"ids":[...]}` |
| Enable ADB / shutdown profile | `MultiloginAdbEnableClient` / `MultiloginShutdownClient` |

Auth: `Authorization: Bearer <MLX token>`. Regular token = 30 min; use the **Workspace Automation Token** for unattended sync (longer-lived, higher limits). For a full MLX→Airtable sync also pull **Workspace Folders** (grouping) and **Proxy** details. See the `reference-multilogin-x-api` note / Postman docs: https://documenter.getpostman.com/view/28533318/2s946h9Cv9

**Sync direction (MLX → Airtable):** for each MLX mobile profile, upsert a `Profiles (Cloning)` row keyed on `MultiLogin Profile ID`; link/create its `Device` (ADB serial from the device), and its `Proxy`. Use list-with-`filterByFormula={MultiLogin Profile ID}='<id>'` to find-or-create (idempotent upsert), respecting the 10-record batch + 5 req/s limits.

---

## 9. Quick reference — all IDs

```
BASE  appL9q0XcNFtP4fYV  (OFM Agency OS)

Accounts             tblLjqdiHagP4n3V6
Profiles (Cloning)   tblnKMvdWNwZgz494
Devices              tblP0Bm7bRNTn2AhA
Posting Queue        tblGk1drpW496oJeR
Spoof Variants       tblE5lJ6D5KdODr9J
Caption Pool         tblTp2FIytPYaLK8C
Performance Log      tbl8J7MsnM4M7ELiA
Ban & Flag History   tbltFTmsf966q4XMl
Content Pipeline     tblNyNxlvDafKhQYX
Models               tblPJfTdjIH8NfPMB
Proxies              tblhEXsoAHKarNbsL
```
