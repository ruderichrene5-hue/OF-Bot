# Which flagged model profiles are actually broken — 2026-08-13, 22:57–23:34 UTC

Every model profile carrying the MultiLogin `Issue` tag was launched, its
screen read once, and the flag removed only where Instagram was positively
working. **Nothing was tapped, typed or bought**: the driver runs `act=False`,
so a profile mid-challenge is left exactly as it was for `verification_runner`,
which is the tool that rents numbers. Cost: **$0.00**.

Tool: `python -m adb_bot.automation.flag_review [--apply]` (dry-run by default).
Raw result: `~/.adb_bot/flag_review/review-20260813-233355.json`.

## The population

**37 flagged model profiles.** Staging phones (`Blank`, `Default profile name`)
and link-in-bio profiles were excluded — neither was ever posting, so neither
has a posting flag.

* **12 skipped for free** — a person had already written the diagnosis on them
  (`logged out` ×6, `Banned / Dead` ×2, `unable to verify` ×4). Launching a
  phone to rediscover what a VA already knows costs two minutes and teaches
  nothing.
* **25 checked.**

## Flagged but healthy — the list to post on

Seven were working normally. Each was confirmed by its **own account's story
tray**, which is positive evidence of a signed-in account rather than the
absence of an error:

| Profile | Handle seen on the phone | id |
|---|---|---|
| Jasmin 6 | `coyemoo__` | 625727194475659309 |
| Jil 3 | `jil.lai67` | 626422241281704072 |
| Jil 8 | `jil.lena777` | 626547576228806881 |
| Laila 4 | `lail_a0703` | 625727194475266093 |
| Laila 5 | `laila_a0407` | 625727194475331629 |
| Nikki 12 | `nikki_lat` | 625727267523461165 |
| Viktoria 3 | `viktoriaistda` | 625727267523264557 |

All seven had their flag removed. Six now carry `Active / Posting` and nothing
else; **`Jil 8` was flagged again within minutes** — see below.

**Also probably healthy: `Jasmin 5`.** Its screen would not produce a UI dump,
so it was read by OCR, which showed an ordinary working feed (`for you`,
`your story`, suggested accounts) in text too garbled to prove it. It was left
flagged on purpose — a flag is only cleared by positive evidence — but it is
the eighth candidate, and it is the profile that was tagged for
`Retries Exhausted` (a posting counter) and found healthy earlier the same day.

## The flags come back until a post confirms

`Jil 8` was unflagged at ~23:10 and was carrying `Issue` again by 23:40. This is
not the review misfiring; it is `stale_profiles` doing its job. It runs inside
the retry loop every 30 minutes and re-flags any profile whose condition is
unchanged, and the condition for **`No Recent Success`** is *no confirmed post
in 24 hours*. Clearing the flag does not post anything, so the flag returns —
three profiles untagged at 19:30:14 on 2026-08-11 were flagged again at
19:30:33.

**Only a confirmed post clears it.** Which means the supervised posting run is
not just the next step, it is the *only* thing that will make these clearances
stick. Until then, expect the six to drift back to flagged, one retry tick at a
time.

## The 18 that really are broken

| What the screen showed | Count | Profiles |
|---|---|---|
| **Signed out** — needs credentials, not verification | 10 | Jil 23, Katja 7, Luisa 2, Luisa 3, Luisa 8, Luisa 9, Luisa 10, Nikki 23, Nikki 25, Nikki 27 |
| Image captcha | 2 | Laila 11, Nikki 19 |
| Photo / video-selfie challenge | 1 | Luisa 7 |
| SMS code screen | 1 | Kathi 7 |
| "Confirm you're human" intro | 1 | Katherine 8 |
| Unreadable screen (no dump, OCR gave nothing) | 2 | Jasmin 5, Kathi 2 |
| Phone never reachable over ADB | 1 | Katja 8 |

**Signed out is over half of them**, and it is the same hole that leaves the
newly created accounts unrecoverable: verification cannot fix it, only a stored
password can. Ten profiles here plus the six already tagged `logged out` by a
VA is sixteen accounts waiting on credentials nobody has.

The captcha, code and human-intro screens are solvable by
`verification_runner` — but not tonight: SMSPool is down to **$0.02** and 5sim
answers `no free phones` for Germany.

## What to do next

1. **Post on the six**, supervised, and watch what actually fails. Their flags
   came from posting, so the posting run is where the answer is — and a
   confirmed post is also the only thing that stops them re-flagging.
2. **Top up SMS** before pointing `verification_runner` at the five with real
   challenges.
3. **Decide the credential story** for the ten signed-out ones. Nothing else
   recovers them.
