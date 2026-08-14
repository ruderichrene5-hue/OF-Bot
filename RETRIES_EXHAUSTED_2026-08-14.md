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
| Kathi 7 | `failed` | composer opened, but the REEL tab never became reachable |

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

## Fault 3 — Kathi 7 is not an account problem at all

Kathi 7 has no `Primary IG Handle` recorded, is a much newer profile (MLX serial
280059 against 173486–184067 for the others) and has **never posted once**. It
failed two different ways:

- **On 2026-08-12/13** — MLX's launcher returned `500 "failed to get profiles
  starting urls"`, the phone never booted, ADB never went ready. Still happening
  today: 7 occurrences in `phone_launcher_20260814.log`, intermittent bursts.
  MLX-side, not the local webkit fault from 2026-08-13.
- **On this run** — it booted, reached the composer, then:

```
u2: REEL tab never became visible after 4 swipe(s)
u2: could not confirm REEL mode; not selecting media to avoid posting a non-reel
Unable to select reel media
```

That REEL-tab failure is fleet-wide and declining, not specific to this phone:
13 occurrences on 2026-08-11, 8 on 08-12, 3 on 08-13. Worth its own look; a
profile that has never posted is the wrong place to conclude anything about it.

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
