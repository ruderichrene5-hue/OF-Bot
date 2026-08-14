# The "Human Verification Required" list, worked — 2026-08-14

Ten phone launches, **$0.00**. Nine profiles carried the Airtable reason
`Human Verification Required`. **None of them needs the SMS or captcha
machinery.** Seven are signed out; two are identity challenges no bot can
answer.

Every verdict below is a screen this run read off the phone, not an inference
from a tag. Recordings (dump, screenshot, text) are under
`~/.adb_bot/verification/<name>-20260814-*`.

## The list

| Profile | MLX id | Flagged since | What the phone actually showed |
|---|---|---|---|
| Jil 2 | 624743063687856180 | 08-05 | signed out |
| Laila 9 | 628516863630180482 | 08-05 | signed out |
| **Luisa 7** | 628516863630442626 | 08-05 | **"Upload your ID"** — official ID, DOB visible |
| **Katja 2** | 625722787553673261 | 08-07 | **"Confirm you're a real person with a video selfie"** |
| Luisa 9 | 628659935063834662 | 08-07 | signed out |
| Luisa 2 | 625615115424694387 | 08-09 | signed out |
| Luisa 8 | 628659781652971558 | 08-09 | signed out |
| Luisa 3 | 625615115424759923 | 08-09 | signed out |
| Jil 10 | 626422241281769608 | 08-11 | feed, then "You've been logged out" |

Seven of the nine sit on Instagram's logged-out landing page — *"Join
Instagram / Get started / I already have a profile"*. That is not a challenge.
No number, no captcha solve and no amount of retrying reaches it; somebody has
to type a password.

## Three verdicts this run corrected

The reading half has been wrong in both directions before, so each of these was
settled on the screen text rather than on the previous answer.

- **Luisa 9** was `needs_human` on 08-12 with the detail "no verification
  challenge, but does not look like a working Instagram either". That run had
  read the **cloud-phone launcher**, not Instagram. It is signed out.
- **Luisa 3** was "could not reach it over ADB" on 08-12 — no verdict at all.
  It is signed out.
- **Jil 10** was `needs_human`, *terminal*, on 08-13: "the photo challenge could
  not be completed". Today it rendered `helenaistdaaa`'s own feed and story
  tray, and **then** Instagram threw *"You've been logged out. Please log back
  in."* A forced logout mid-session, not a photo challenge. It had been benched
  for seven days on the wrong diagnosis.

`Luisa 7` also failed its first launch with "could not reach it over ADB" and
succeeded on a straight retry — the same MultiLogin-says-ready-but-adb-cannot
-see-it flap. **One ADB failure is not a diagnosis.** Retry before recording
one; two of the nine would have been mis-filed otherwise.

## The two real challenges are both identity proof

Neither is a bot problem, and neither is a money problem:

- **Katja 2** — a video selfie of the person the account claims to be.
- **Luisa 7** — a photograph of an official ID with a date of birth on it.

Both need a decision from the client: supply the document, or retire the
account. Nothing in `flows/verification.py` should ever attempt either, and the
`--apply` path was deliberately not pointed at them.

## What was written

Only two things, both reversible, both additive:

- **MultiLogin tags.** `logged out` on Luisa 2, Luisa 3, Luisa 8, Luisa 9 and
  Jil 10; `unable to verify` on Luisa 7. (Jil 2, Laila 9 and Katja 2 already
  carried a correct tag.) `verification_runner` reads exactly these and now
  skips all nine **for free** — its "already diagnosed" bucket went 17 → 22 and
  its run list 33 → 30. Before this, every pass spent a ~2-minute launch per
  profile rediscovering the same thing.
- **Airtable `Issue Notes`.** Today's diagnosis prepended to each of the nine,
  prior history preserved.

Deliberately **not** written:

- **`Issue Reason` is still `Human Verification Required` on all nine, and it is
  wrong on all nine.** The field has no option that means "signed out" — the
  choices are Retries Exhausted, Human Verification Required, Banned / Blocked,
  Repeated Failures, Device Unreachable, No Recent Success, Held For Supervised
  Run. Adding one is the open question `VERIFICATION_RUN_2026-08-13.md` §"Open
  questions" already put to the client; answering it by creating a select option
  unasked seemed worse than leaving the field visibly wrong with the truth in
  the note beside it.
- **Nothing was unflagged.** Clearing a flag hands the profile straight back to
  the posting loop, and none of these nine can post.

## Four profiles kept out of this run on purpose

`verification_runner` routes `solved` → remove the `Issue` tag, and it has
**no** guard against doing that to a profile whose Airtable reason is
`Banned / Blocked` — only `flag_review` got that guard. A suspended account
keeps rendering a cached feed, which is how `Laila 5`'s ban was overwritten with
`No Recent Success` on 08-13. So these were excluded by name:

- `Laila 5` — suspension notice on the phone, reason restored by hand at 14:03.
- `Jil 6`, `Jil 7`, `Laila 4` — held for a supervised run at 14:05; unflagging
  them makes a 50-row backlog due at once.

**That missing guard is worth porting from `flag_review` into
`verification_runner`.** Right now the only thing standing between a cached feed
and an overwritten ban diagnosis is whoever picks the `--only` list.

## Money

**$0.00.** 5sim `6.84` and 2captcha `9.99` are unchanged from the readings taken
before the run. SMSPool read `0.02` at 17:20 and `9.45` at 17:47 — a top-up by a
person during the run, not spend.

The probe cannot spend: without `--apply` it never taps, types or rents. The
whole point of reading first was that a burned-pool run against nine profiles
would have cost real money to learn that none of them wanted a number.

## What actually unblocks these

**Seven of the nine are waiting on credentials nobody has.** Counting the six a
VA had already tagged and the ones found on 08-13, this is now the single
largest category of dead accounts in the fleet, and none of it is a bot defect.
`~/.adb_bot/accounts/accounts.json` only holds identities the *signup* flow
created, so there is no store to read these from.

The ask for the client, in order of how much it unblocks:

1. **Instagram passwords** for Jil 2, Laila 9, Luisa 2, Luisa 3, Luisa 8,
   Luisa 9, Jil 10 — seven accounts back in the posting loop.
2. **A decision on Katja 2 and Luisa 7**: supply a selfie / ID document, or
   retire them.
3. **A `Signed Out` option on `Issue Reason`** so the board stops saying
   "Human Verification Required" about accounts that need a password.
