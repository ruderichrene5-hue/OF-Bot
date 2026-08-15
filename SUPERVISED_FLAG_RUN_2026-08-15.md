# Seven parked profiles, launched one at a time and watched

2026-08-15, 17:17–18:00 UTC. Six profiles carrying `No Recent Success` and one
carrying `Retries Exhausted` were each run through the **real posting flow**
with the `Needs Human Check` gate off (`posting_probe`), one phone at a time,
with the log read as it happened.

**Not one of the seven was parked for the reason its label gave.** As with the
2026-08-14 sweep ([`RETRIES_EXHAUSTED_2026-08-14.md`](RETRIES_EXHAUSTED_2026-08-14.md)),
both labels turned out to be counters, not diagnoses — and this time four of the
seven had never been attempted at all.

## The verdicts

| Profile | Was | Now | What was actually wrong |
|---|---|---|---|
| `nikki 8` | No Recent Success | **flag cleared** | nothing. Posted 98 → 99 first try |
| `Katja 3` | No Recent Success | Held For Supervised Run | nothing. Posted 21 → 22 first try; held only for its backlog |
| `Jil 6` | No Recent Success | **Account Not On Phone** | `@helenaypurebabe` is not in the phone's switcher |
| `Jil 8` | No Recent Success | **Account Not On Phone** | `@helen_aiscooll` is not in the phone's switcher |
| `Nikki 12` | Retries Exhausted | Device Unreachable | ADB fails on this phone; `@kikittie22` never reached Instagram |
| `Jasmin 5` | Retries Exhausted | Device Unreachable | the MLX profile is not in the workspace — cannot launch |
| `nikki 4` | No Recent Success | **Banned / Blocked** | Instagram has disabled the account |

Launch health across the run: **11 attempts, 10 ok, 1 MLX-500** — and that one
500 is `Jasmin 5`, which is a permanent condition, not cloud flakiness. The
phones are fine.

## Four of them had never been tried once

The grep pair from the flag-deadlock note (`Post result for <name>` vs
`Skipping post <name>:`), over six days of `adbbot-posting.service`:

| Profile | real attempts | skipped |
|---|---|---|
| `Jil 8` | **0** | 16,385 |
| `Nikki 12` | **0** | 15,415 |
| `Jil 6` | **0** | 13,456 |
| `Jasmin 5` | **0** | 12,348 |
| `nikki 4` | 26 | 11,931 |
| `Katja 3` | 21 | 11,583 |
| `nikki 8` | 22 | 738 |

`No Recent Success` means "no confirmed post in 24 h" and the planner skips any
flagged profile, so the flag blocks the only event that could clear it. Those
four were not failing. They were never asked.

## The switcher fault, seen on the phone

`Jil 6` and `Jil 8` are the same fault, and it is visible in one line each:

```
Jil 6  17:28:35  signed in as @jill.acc19; switching to @helenaypurebabe
       17:28:42  WARNING: the account switcher does not list @helenaypurebabe
                 -- Airtable says this phone has it, the phone disagrees
       17:35:53  CONFIRMED (strong) via banner  ->  @jill.acc19 posted

Jil 8  17:37:48  signed in as @jil.lena777; switching to @helen_aiscooll
       17:37:56  WARNING: the switcher does not list @helen_aiscooll
       17:40:16  CONFIRMED (strong) post count 12 -> 13  ->  @jil.lena777 posted
```

The phone is healthy and the sibling account posts perfectly. The primary handle
is simply not on the device — see [`adbbot-transposed-handles`]: accounts are
falling off these phones, roughly one every 2–3 days.

**This is why they were starving.** The dead handle's rows are the oldest,
because they have been failing longest, so earliest-scheduled-first puts them at
the front:

| Profile | due rows | on the dead handle |
|---|---|---|
| `Jil 8` | 22 | **15** |
| `Jasmin 5` | 15 | **10** (`@jasmindiecoolee`, which has never posted once) |
| `Nikki 12` | 19 | 12 (`@kikittie22`) |
| `Jil 6` | 9 | 8 |

Retrying cannot fix any of them. Correct the handle in Airtable, or put the
account back on the phone.

## Jasmin 5 cannot be launched at all

The one `Retries Exhausted` profile never reaches Instagram. MultiLogin refuses
the launch outright:

```
"the following profile IDs do not belong to the workspace
 f03f25bc-eb13-4ec1-9cd6-80f14b3f9255, IDs: 625615005450240115"
```

It is also absent from the 214-profile MLX inventory. So its retries were spent
on a profile that is no longer in the workspace — no handle, account or screen
involved. Note this is a **different** fault from the intermittent
`failed to get profiles starting urls` 500 that hits `Kathi 7`: this one names
the workspace and the ID, and it does not self-heal.

## nikki 4 is banned, and the label had overwritten that

Read off the phone during the run:

```
17:51:07  block screen: banned matched 'we disabled your account'
          "...you no longer have access to nikkiist..."
```

A person had already tagged it `Banned / Dead` in MultiLogin. `stale_profiles`
had overwritten that verdict with `No Recent Success` — the same way `Laila 5`'s
ban diagnosis was lost on 2026-08-13. **17 rows are due on a disabled account**
and must not go out.

## Two things that are not what they look like

**An UNCERTAIN post is very often a live one.** `Nikki 12`'s first run reported
`@nikki_lat` as `UNCERTAIN -- no positive signal` at baseline 59. The second
run's baseline was **60**, and it then confirmed 60 → 61. The first post had
landed; the verification window was simply too short. `nikki 8` was flagged on
exactly this signal — its last outcomes were `uncertain`, not `failed`.

**`@kikittie22` is untested, not diagnosed.** It failed twice
(`uiautomator2 could not connect -- device not online`, then
`adb_connect_failed`) *before the flow ever reached the account switcher*. It
may or may not also be missing from the switcher; nothing here says. Do not
record a verdict on it without a run that gets as far as Instagram.

## Why the healthy ones were not simply unflagged

Clearing a flag makes the profile's whole backlog due at once, and `run_profile`
works through everything due on one launch. `Katja 3` is proved healthy but has
**10 rows** waiting — that is the shape that ground this same phone for an hour
on 2026-08-14 for one confirmed post. It is therefore held with
`Held For Supervised Run` rather than released; drain it with
`posting_probe --posts-per-profile 1` and then clear.

`nikki 8` had **0** rows due after its post, so clearing it was free.

## A gap worth closing

MultiLogin tags `nikki 8` `2 accounts`, but Airtable has `Has Second Account`
empty and no handles for it. The second account is invisible to the bot and gets
no queue rows at all. (`Nikki 12` carries the same `2 accounts` spelling; the Jil
phones use `Second Account` — two spellings for one thing.)

## Reproducing this

`posting_probe` and the round-robin per-profile cap live on `flag-posting`,
which is seven commits behind `integration`. Running the probe from there would
diagnose with the very bugs `integration` fixed — the terminal
`Account Not On Phone` refusal, the stand-in post, and the `Kathi 7` markers
that stop an SMS checkpoint reading as "REEL tab never became visible". This
branch is `integration` plus those three tool commits cherry-picked.

Two-account phones were run with `--posts-per-profile 2` on purpose: the cap
round-robins across handles, so a cap of 2 gives **each account exactly one
attempt**. That is what separates "this phone is broken" from "one handle is
missing", and a cap of 1 would have spent Jil 8's whole visit on a certain
refusal.
