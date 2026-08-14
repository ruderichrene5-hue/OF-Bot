# Stale-flag review and supervised posting, 2026-08-14

Re-checked every model profile whose Airtable `Issue Reason` is `No Recent
Success` or `Retries Exhausted` -- the two reasons that mean *posting stopped*,
as opposed to a reason naming something wrong with the account -- then posted on
the healthy ones with somebody watching.

    python -m adb_bot.automation.flag_review --posting-reasons --apply
    python -m adb_bot.automation.posting_probe --cleared --apply

## What was flagged

11 profiles carried the two reasons. Two (`Katja 3`, `nikki 4`) were already
tagged `logged out` by a person and were skipped for free. The other 9 were
launched and read once, with an `act=False` driver that cannot tap, type or buy
anything. **Cost: $0.00.**

| Profile | Verdict | What was on screen |
|---|---|---|
| Jil 6 | **cleared** | own story tray, `jill.acc19` |
| Jil 7 | **cleared** | own story tray, `jills.sav` |
| Jil 8 | **cleared** | own story tray, `jil.lena777` |
| Laila 4 | **cleared** | own story tray, `lail_a0703` |
| Jasmin 5 | left flagged | no UI dump; OCR shows a working feed but garbled |
| Jil 5 | left flagged | caught mid-splash ("from meta") |
| Kathi 7 | left flagged | real SMS code screen, `+31613813164` |
| Laila 5 | left flagged | **suspended** -- see below |
| Nikki 12 | left flagged | phone unreachable |

## The flag was a deadlock, not a diagnosis

`No Recent Success` means "no confirmed post in 24 h". The planner skips a
flagged profile. So the flag blocks the only event that could clear it, and the
profile parks forever.

Counting six days of the posting loop's log:

| Profile | Real post attempts | Times skipped |
|---|---|---|
| Jasmin 5 | 0 | 9,430 |
| Jil 5 | 0 | 976 |
| Jil 6 | 0 | 11,458 |
| Jil 8 | 0 | 11,837 |
| Nikki 12 | 0 | 12,038 |
| Kathi 7 | 5 | 632 |

Five of these were never tried once. They were not failing; they were only ever
refused. This is why `posting_probe` exists and why it can turn the
`Needs Human Check` gate off -- with a person watching, and never from a timer.

## Clearing a flag is not safe on its own

A profile parked for days accumulates queue rows, and **everything due on a
phone runs sequentially on one launch**. Unflagging the four above made 50 rows
due at once:

| Profile | Rows waiting |
|---|---|
| Jil 8 | 17 |
| Laila 4 | 16 |
| Jil 7 | 10 |
| Jil 6 | 7 |

The 14:03 tick planned exactly those 50 and would have posted 17 reels back to
back on `Jil 8`. It was stopped during ADB readiness, so **nothing went out**.
Yesterday's clearing of seven profiles had the same exposure and nobody noticed.

Two changes come from this:

* `plan_posting_queue(max_posts_per_profile=...)` caps posts per phone per run,
  oldest scheduled first. Uncapped by default, so the live loop is unchanged;
  the probe sets 1.
* The four are held on reason `Held For Supervised Run` so the unattended loop
  skips them while their backlog is decided. **The backlog is still there --
  46 rows, days old.** Nothing should un-hold them until that is dealt with.

## A screen read erased a real ban

`Laila 5` was reported `banned` by the posting loop on 2026-08-12. On 2026-08-13
its phone showed an ordinary feed with its own story tray -- a suspended account
keeps rendering a cached feed until the app next checks -- so the sweep cleared
it. Clearing the MLX tag reactivated the profile, `stale_profiles` re-flagged it
with its own reason, and the `Banned / Blocked` diagnosis was overwritten with
`No Recent Success`.

Today the phone shows Instagram's own words: *"we suspended your account, laila
-- 177 days left to appeal or we'll permanently disable your account."*

The reason has been restored by hand, and `flag_review` now refuses to clear any
profile whose recorded reason is `Banned / Blocked` or
`Human Verification Required`: those are conclusions drawn from an actual
posting attempt, and one screenshot may not overrule them. If Airtable cannot be
read at all, nothing is cleared.

## The ban detector was reading machine text

`account_flag_u2` passed the **raw XML hierarchy** to `classify_block_text`,
whose contract is visible screen text. The dump also carries `resource-id`,
`class`, `package` and `bounds` on every node, so anything matching a marker
anywhere in the tree classified a working screen as a block. This is the same
defect that produced 17 false `Human Verification Required` flags out of 29 on
2026-08-11 -- fixed there, left standing in the reel flow.

It now reads only `text` and `content-desc`, and logs the phrase it matched:

    block screen: human_verification matched 'we detected unusual activity' in: ...

`Laila 4`'s seven consecutive `human_verification` results on 2026-08-12 each
follow the identical pair of warnings:

    u2: REEL tab never became visible after 4 swipe(s)
    u2: could not confirm REEL mode; not selecting media to avoid posting a non-reel

Whether that is a real checkpoint or a misread screen was not decidable from the
logs -- which is precisely why the detector now says what it saw.

## The supervised run: 1 posted, 3 failed on one cause

    python -m adb_bot.automation.posting_probe --cleared --apply --posts-per-profile 1

| Profile | Row targeted | Result |
|---|---|---|
| Laila 4 | `lail_a0703` (its own handle) | **done** -- post count 70 → 71, strong |
| Jil 8 | `@helen_aiscooll` | `adb_connect_failed` (MLX 500, phone never came up) |
| Jil 7 | `@helenisyourebabe` | `failed` -- switcher does not list that handle |
| Jil 6 | `@helenaypurebabe` | `failed` -- switcher does not list that handle |

`Laila 4` proves the accounts are fine: it went through REEL tab, gallery, Next
and Share on the first attempt, including the exact step that produced seven
`human_verification` results on 2026-08-12. **Those were not a real checkpoint.**

The other three all failed on the same thing, and it is not Instagram.

## Airtable has the two handles the wrong way round

The posting flow refuses to post unless the account switcher proves the target
handle is signed in -- correctly, since the alternative is posting on the wrong
account. For four profiles, Airtable's `Primary IG Handle` is a handle the phone
does not have, and its `Second IG Handle` is the one the phone is actually
signed in as:

| Profile | On the phone | Airtable primary | Airtable second |
|---|---|---|---|
| Jil 6 | `jill.acc19` | `helenaypurebabe` | `jill.acc19` |
| Jil 7 | `jills.sav` | `helenisyourebabe` | `jills.sav` |
| Jil 8 | `jil.lena777` | `helen_aiscooll` | `jil.lena777` |
| Jasmin 5 | `naughty_jasminn` | `jasmindiecoolee` | `naughty_jasminn` |
| Jil 5 | `helenaiscutee` (matches) | `helenaiscutee` | `jiji.ll12` (absent) |

The handles on the phone were read two independent ways: each account's own
story tray during the flag sweep, and the flow's own
`is signed in as @X; switching to @Y` line.

**37 pending queue rows** name one of these absent handles -- 13 on `Jil 8`,
8 on `Jasmin 5`, 6 each on `Jil 6`/`Jil 7`, 4 on `Jil 5` -- and **33 of them are
marked `Account Slot = Primary`**. So this is not a second-account feature
problem: the row believes it is posting to the primary and names an account that
is not there. Every retry fails identically, which is exactly how these profiles
reached `Retries Exhausted` with nothing wrong at Instagram's end. Across seven
days of the posting log the refusal appears **324 times**.

Four other phones switch accounts successfully in both directions
(`nikkiisthierr` ↔ `nikki.kie20`, `nikkidiehotte` ↔ `nikkiasf_095`,
`nikkish97` ↔ `nikkiishier`, `jsmin3075` ↔ `janabahdim`), so the switcher itself
works. It is the recorded handles that are wrong.

### It is not a transposition -- correction

The pattern above reads as the two fields being swapped, and swapping them back
was nearly the recommendation. It is wrong. Asking whether the "missing" handles
had **ever** posted killed it: three of them had.

| handle | slot | last posted | first failed |
|---|---|---|---|
| `helenaypurebabe` | Primary | Aug 7 | Aug 10 |
| `helen_aiscooll` | Primary | Aug 8 | Aug 12 |
| `jiji.ll12` | Second | Aug 11 | Aug 14 |
| `helenisyourebabe` | Primary | **never** | Aug 13 |
| `jasmindiecoolee` | Primary | **never** | Aug 13 |

Two faults wearing one symptom:

1. **Signed out** (`helenaypurebabe`, `helen_aiscooll`, `jiji.ll12`) -- real
   accounts with posting history that have dropped out of the phone's account
   switcher. They need a re-login, so they need credentials.
2. **Never there** (`helenisyourebabe`, `jasmindiecoolee`) -- recorded in
   Airtable, never posted once, never in the switcher. A data error.

**It is progressive: one account roughly every 2-3 days.** Aug 7, Aug 8, Aug 11.
Five of the fourteen two-account phones are affected so far, and on that trend
more will follow. Swapping the fields would have hidden the first group and
"fixed" nothing -- the account still is not on the phone.

## Still open

* **The transposed handles.** One decision -- which account is the model's
  primary -- unblocks 37 rows across five profiles. Nothing else here is worth
  doing first.
* **46 backlogged queue rows** on the four held profiles. Posting them is not an
  option; they need to be dropped or re-dated.
* `Jasmin 5` and `Jil 5` were never decided -- both are cheap to re-check.
* `Nikki 12`'s phone would not connect.
* `Kathi 7` needs a real SMS code; SMSPool is at $0.02 and 5sim has no German
  stock.
