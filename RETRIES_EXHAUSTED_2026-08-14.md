# The four `Retries Exhausted` profiles, posted on and watched — 2026-08-14

Every profile carrying `Issue Reason = Retries Exhausted` was run through the
**real posting flow** (`posting_probe --apply`, one profile at a time, one post
each, `Needs Human Check` gate off) and watched to a verdict.

None of the four was parked for the reason the label gives. `Retries Exhausted`
is a **counter that reached 5**, and nothing more — the same shape of mistake as
the `No Recent Success` deadlock. Three distinct faults were wearing it.

## What the run found

| Profile | result | why |
|---|---|---|
| Jil 8 | `wrong_account` | `@helen_aiscooll` is not in the phone's account switcher |
| Jasmin 5 | `wrong_account` | `@jasmindiecoolee` is not in the phone's account switcher |
| Jil 5 | `wrong_account` | `@jiji.ll12` is not in the phone's account switcher |
| Kathi 7 | `failed` | never in a composer at all — an Instagram SMS checkpoint |

Launch health for the run: 4 attempts, 4 ok, 0 MLX-500. The phones are fine.
In all four cases the flow **refused to post rather than post the wrong thing**,
which is the behaviour we want; the bug is in what happens to the row afterwards.

## Fault 1 — a dead handle burns five retries and parks the whole phone

On `integration` (the live branch) the account-switcher guard exists and works:

```
199.190.44.226:39805 is signed in as @jil.lena777; switching to @helen_aiscooll
The account switcher does not list @helen_aiscooll -- Airtable says this phone has it, the phone disagrees
Not posting: could not prove it is signed in as @helen_aiscooll. The clip stays queued
```

But `instagram_reel.py` returns a bare `{"success": False, "failed": True}` for
that refusal — an **ordinary retryable failure**. So:

    handle not in switcher -> generic failure -> `Failed - Needs Retry`
      -> retry pass re-queues -> 5 retries burn -> row `Retries Exhausted`
      -> profile flagged `Needs Human Check` -> BOTH accounts on the phone stop

A row naming a handle the phone does not have cannot succeed, ever. Retrying it
is spending a launch and a ~2 min boot to reach the same answer five times.

### It starves the healthy account

`_cap_per_profile` selects **earliest scheduled first**. On a parked profile the
oldest rows are the dead-handle ones, because they have been failing longest, so
the dead account is systematically picked first. Due-row order on 2026-08-14:

| Profile | dead rows ahead of the first healthy row |
|---|---|
| Jil 8 | 10 (`@helen_aiscooll` × 10, then `@jil.lena777`) |
| Jasmin 5 | 5 (`@jasmindiecoolee` × 5, then `@naughty_jasminn`) |
| Jil 5 | 3 (`@jiji.ll12` × 3, then `@helenaiscutee`) |

At five retries each, the healthy account on Jil 8 sits behind **50 doomed
attempts**. That is the mechanism by which one signed-out handle parks a phone
whose other account is posting perfectly well.

### The fix

Cherry-picked `6396344` onto `integration` — it was written on `flag-posting`
on 2026-08-14 and **never merged**, along with three sibling commits. The
refusal becomes its own terminal status mapped to `Account Not On Phone`, which
`retry_runner.py:95` does not re-queue. The counter is not bumped (a refusal is
not an attempt) and the variant is left unused, so the clip goes out as soon as
the handle is corrected.

Verified live during the run — both refused rows wrote back correctly:

```
Jil 8 / 19:06       status=Failed  issue=Account Not On Phone
Jasmin 5 / 19:52    status=Failed  issue=Account Not On Phone
```

Full suite: 2319 passed, 1 skipped.

### Proved on the phone: the healthy account posts

Jil 5 was re-run with a cap of 4, which is exactly its three dead rows plus the
first healthy one. The three `@jiji.ll12` rows refused, and then:

```
Baseline post count for 199.190.44.226:22864: 94
u2: REEL mode confirmed selected
Verifying reel post: baseline=94, min 20s / max 45s
CONFIRMED (strong) via post_count after 46s (post count 94 -> 95)
Post result for helenaiscutee: done
```

**A real reel went out**, verified by the post count incrementing rather than by
trusting a banner. `@helenaiscutee` is now 17 Posted, up from 16. Nothing was
wrong with the phone or the account — the healthy row was simply queued behind
dead ones.

The cost of each doomed row is visible in the timestamps: the three refusals
landed at **18:45:53, 18:50:19 and 18:54:49** — a consistent **~4.4 minutes**
each, every one a fresh launch, boot, ADB connect and media push before the
switcher says no. On live code that is multiplied by five retries per row.

Six rows are now terminal `Account Not On Phone` and permanently out of the
retry loop: four `@jiji.ll12`, one `@helen_aiscooll`, one `@jasmindiecoolee`.

## Fault 2 — the accounts themselves, which code cannot fix

Two different problems, and the remedies differ. Split by whether the handle
ever posted:

| handle | profile | posted | last posted | verdict |
|---|---|---|---|---|
| `helen_aiscooll` | Jil 8 | 2 | 2026-08-08 | **signed out** — was on the phone |
| `jiji.ll12` | Jil 5 | 10 | 2026-08-11 | **signed out** — was on the phone |
| `jasmindiecoolee` | Jasmin 5 | 0 | never | **never on the phone** — Airtable data |

The two signed-out accounts need a re-login, so they need credentials nobody
has. `jasmindiecoolee` was never on that phone at all: the row is wrong, not the
device. Their healthy counterparts (`jil.lena777` 12 posts, `naughty_jasminn`
14, `helenaiscutee` 16) are all still reachable.

This is the progression already recorded — roughly one account falling out of a
switcher every 2–3 days. Expect more; do not read the next one as a new fault.

### Follow-up: the clip now goes out on the account the phone does have

Asked for on 2026-08-14. Parking a clip forever waits on credentials nobody
has, while the phone's sibling account posts fine — and it is the same model's
content either way. So when the switcher is demonstrably open and the row's
handle is demonstrably not in it, the reel goes out on the account the phone
*is* signed in as, and the row's note records which:

```
posted on @jil.lena777 -- the row's handle is not on this phone
```

Two limits, both deliberate:

- **Only off an `ACCOUNT_ABSENT` verdict.** That verdict already requires the
  switcher to have been proven open. If the profile header will not read, there
  is no stand-in and nothing is posted — posting on an account nobody
  identified is the exact outcome the account check exists to prevent.
- **One stand-in post per profile per run.** A phone parked on a missing handle
  has a *backlog* of orphaned rows (Jasmin 5 has nine) and they all resolve to
  the same stand-in account, while the timer loop sets no per-profile cap.
  Without this, `@naughty_jasminn` would post its own four rows plus nine
  orphans back to back. Thirteen reels in a row from one account is the
  behaviour Instagram acts on. The rest stay queued and drain one per tick.

## Fault 3 — Kathi 7 is not an account problem at all

Kathi 7 has no `Primary IG Handle` recorded, is a much newer profile (MLX serial
280059 against 173486–184067 for the others) and has **never posted once**. It
failed two different ways:

- **On 2026-08-12/13** — MLX's launcher returned `500 "failed to get profiles
  starting urls"`, the phone never booted, ADB never went ready. Still happening
  today: 7 occurrences in `phone_launcher_20260814.log`, intermittent bursts.
  MLX-side, not the local webkit fault from 2026-08-13.
- **On this run** — it booted, and then:

```
u2: REEL tab never became visible after 4 swipe(s)
u2: could not confirm REEL mode; not selecting media to avoid posting a non-reel
Unable to select reel media
```

### Why the REEL tab was never going to appear

**Kathi 7 was never in a composer.** Read directly off the phone, read-only, on
2026-08-14 — Instagram is sitting on
`com.instagram.challenge.activity.ChallengeActivity`:

```
Get support
Enter confirmation code
Enter the 6-digit confirmation code we sent via SMS to +31613813164.
It may take up to a minute for you to receive this code.
6-digit code | Request new code | Next | Update mobile number
```

It is an SMS checkpoint. Instagram is installed and healthy (v442.0.0.46.79);
the account is simply waiting for somebody to type in a code. The run log said
so an hour before anyone looked — the composer step listed its tap candidates as
`request new code`, `next` and `update mobile number` — and three separate
signals up-thread agreed: no profile tab, not on the home feed, and the Home tab
not found by *any* selector.

**Two bugs let a checkpoint look like a composer failure.**

1. **`^next$` counted as proof the gallery was open.** `_GALLERY_SELECTORS`
   accepted a bare "Next", which is on nearly every Instagram onboarding, login
   and checkpoint screen. It matched the checkpoint's Next button, the flow
   announced `reel composer / gallery appeared`, and then swiped four times
   hunting a REEL tab that could not exist. Removed — every remaining entry
   names something only the composer has, and the successful Jil 5 post matched
   on the real one (`id=com.instagram.android:id/cam_dest_clips`), so the happy
   path is untouched.

2. **The checkpoint was not classified as one.** `ban_detection` had
   `"enter the code we sent"`, which does **not** match `"Enter the 6-digit
   confirmation code we sent via SMS"` — the words in the middle break it. So
   `account_flag_u2` returned nothing, the flow reported a plain failure, the
   retry pass re-queued it five times, and the profile landed on
   `Retries Exhausted`. Added the markers this screen actually shows, with the
   verbatim text as a test, plus a test that the composer's own screens do not
   trip them.

With both fixed the screen classifies as `human_verification`, which maps to
`Failed` + `Human Verification Required` + an incident — terminal, no retry
counter bumped, not re-queued. Exactly the same disease as fault 1: a
non-retryable condition reported as a retryable failure.

Kathi 7's Airtable reason has been corrected from `Retries Exhausted` to
`Human Verification Required`, with the evidence in its Issue Notes.

The bare `REEL tab never became visible` warning is fleet-wide and declining
(13 on 08-11, 8 on 08-12, 3 on 08-13); with `^next$` gone, the cases that were
really checkpoints will now say so instead of counting swipes.

## Two things worth not misreading

**A single refusal is not a dead account.** `@mini_jas12` (Jasmin 3) shows one
`does not list` in the recent logs, which looks like a seventh casualty in the
"one account every 2–3 days" progression. It is not: that handle has 17 posts
and **last posted the same day, 17:27**. The fix guards exactly this by refusing
to conclude "absent" unless the switcher is demonstrably open — a sheet that
never opened used to read as absence.

**Zero refusals can mean nobody tried.** `@helen_aiscooll` and
`@jasmindiecoolee` show no refusals at all in the two most recent posting logs,
because their profiles were parked and never attempted — not because they
recovered. Any check that counts failures per window has this hole; confirm the
thing it watches was actually attempted before believing its all-clear.

## What is still open

- **Deploy.** The fix is committed on `worktree-retries-exhausted-fix`, not
  merged to `integration` and not deployed. Nothing changes on the fleet until
  it is.
- **The current flags stay.** The fix stops *future* false `Retries Exhausted`;
  it does not clear the four already set. Clearing one re-flags within ~30 min
  unless a post confirms, so release them just after a retry tick.
- **Three sibling commits on `flag-posting` are also unmerged** (`af56096`,
  `3ae8c28`, `1237e90`), including the supervised probe itself.
- **Credentials** for `helen_aiscooll` and `jiji.ll12`.
- **`jasmindiecoolee`** should be corrected or cleared in Airtable — it has
  never been on that phone.
