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

## Re-check at 18:00 — and the thing "signed out" was hiding

Profiles had been looked at by hand, so all seven signed-out phones were read
again. **MultiLogin tags had not moved at all** — the `issue-tags` mirror
reported `tagged=0 untagged=0 resolved=0 raised=0` against 219 unchanged, so
nobody had cleared an `Issue` tag. The change was on the phones, not in the
tags, which is why re-reading the screens was the only way to find it.

Six were unchanged. **`Laila 9` was not.**

At 17:45 it showed the ordinary logged-out landing page. At 18:00, after
somebody signed back in, the same phone read:

> We disabled your account — you no longer have access to **laila_linksss**.
> Account disabled on **August 12, 2026**. Your account, or activity on it,
> doesn't follow our community standards… all your information will be
> permanently deleted.

**So `signed out` is not a terminal diagnosis — it can be a ban with the login
screen in front of it.** A logged-out phone shows the same landing page whether
the account behind it is healthy, challenged or destroyed; the state only
becomes visible once someone signs in. Six of the seven are still unread in that
sense, and any of them could be a disabled account too. That is worth saying
plainly to whoever supplies the credentials: **expect some of these logins to
fail into a ban notice rather than into a working account.**

Corrected for `Laila 9`, and nothing else touched:

- MLX `logged out` → `Banned / Dead`; `Issue` **kept**, so the posting loop can
  never pick it up.
- Airtable `Issue Reason` → `Banned / Blocked`, with the disable date in the
  note.
- The other six got a note recording the re-check; their tags were already
  correct and were left alone.

Nothing was unflagged and no backlog was released. For the record, what a clear
*would* have released: Luisa 2 **21** pending rows, Luisa 8 **19**, Luisa 3
**17**, Jil 10 **7** — 64 across the seven, with Jil 2, Laila 9 and Luisa 9 at
zero.

Dashboard: `adbbot-site` and `adbbot-report` restarted to drop the 300s cache;
:8088 and :8080 both serve `Laila 9 — Banned / Blocked`. No redeploy was needed,
because the dashboard's *code* is pinned but its *data* is read live. Note both
units warn that their unit files changed on disk and want a `daemon-reload`;
that predates this work and was left alone rather than enact somebody's pending
unit edits.

## What actually unblocks these

**Seven of the nine are waiting on credentials nobody has.** Counting the six a
VA had already tagged and the ones found on 08-13, this is now the single
largest category of dead accounts in the fleet, and none of it is a bot defect.
`~/.adb_bot/accounts/accounts.json` only holds identities the *signup* flow
created, so there is no store to read these from.

The ask for the client, in order of how much it unblocks:

1. **Instagram passwords** for Jil 2, Luisa 2, Luisa 3, Luisa 8, Luisa 9 and
   Jil 10 — six accounts, not seven: `Laila 9` turned out to be disabled and is
   now `Banned / Blocked`. Expect some of the remaining six to sign in to a ban
   notice rather than a working account, for the reason in the 18:00 section.
2. **A decision on Katja 2 and Luisa 7**: supply a selfie / ID document, or
   retire them.
3. **A `Signed Out` option on `Issue Reason`** so the board stops saying
   "Human Verification Required" about accounts that need a password.
