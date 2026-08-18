# Signup run, 2026-08-18

Continues [SIGNUP_RUN_2026-08-17.md](SIGNUP_RUN_2026-08-17.md). Same phone
(`Blank caio 2`), same mailbox (`cicirahmaputrimu@gmail.com`).

**An Instagram account was created: `@ida.sommer43`.** The first one this
chain has ever produced end to end, and the first since `@hanna.sommer33` was
made by hand on 2026-08-13. It is **not usable yet** — Instagram put it behind
"confirm you're human" seconds after creating it.

## 1. Yesterday's blocker was real and is gone

Yesterday's note said Instagram was throttling the address after ~12
submissions in a day, and that the fix was to wait hours rather than retry.
That was right. Nineteen hours later the email screen answered in **~30
seconds** and the run reached the code screen in ~50, against a whole
30-screen budget spent spinning yesterday. Nothing was changed to achieve
that; the throttle simply decayed.

## 2. What was actually stopping the code read: Gmail sync was off

The first run today still failed — `no Instagram code reached
cicirahmaputrimu@gmail.com in 210s` — and it was **not** throttling, not the
shade, and not the read.

`dumpsys content` prints one row per sync authority, columns being authority,
syncable, enabled. On `Blank caio 2` it said:

    gmail-ls    -1    false    Total  0  0  0  0  0  0  0  0  0  0s

`enabled=false`, and every counter zero: **Gmail sync had never run once on
this profile**. No mail could arrive, so the notification shade was empty and
the inbox was empty, and both were empty for the same reason. The run polled
that inbox for its whole 210-second budget and then reported what looks like
Instagram's fault.

Turning the switch on made **six Instagram codes appear at once**, the oldest
six days old — including the one requested 20 minutes earlier.

### 2.1 Why the flow could not see it

Gmail *did* offer its "account sync is off" banner, once, on the first pass.
But it rendered on the same screen as the welcome tip, and `is_onboarding` was
checked **before** `sync_is_off`. So the tour branch won, tapped `OK`, and that
dismissed the tip **and the banner together**. From that pass on:

- `sync_is_off(text)` was false forever — nothing on screen said it any more;
- `inbox_shows_address` matched the "Signed in as … cicirahmaputrimu@gmail.com"
  header, so ownership was satisfied;
- the inbox read "Nothing in Primary", which was **not** in `_EMPTY_MARKERS`,
  so it did not even register as an empty inbox.

Three checks in a row each returned a plausible answer, and the run polled a
dead mailbox to timeout. This is the same shape as every other bug in this
work: not an error, a *confident wrong result*.

### 2.2 Where the switch actually is

Not where the banner suggests, and not in Android's account settings:

- Android's **master** "Automatically sync app data" was already **on**.
- The account's own page said "Account sync — **sync off for all items**", and
  its per-item list was **empty**: with `syncable=-1` Android has not
  enumerated the adapters, so there is nothing there to toggle. "Sync now" from
  that page's overflow does nothing.
- The switch that works is inside **Gmail**: Settings → the account → Data
  usage → **`Sync Gmail`**, which was unchecked. Checking it flipped
  `gmail-ls` to `enabled=true` immediately.

Gmail's own `Gmail2PreferenceActivity` is **not exported** (`am start` throws);
the way in is `com.android.mail.ui.settings.PublicPreferenceActivity`.

## 3. The account, and where it stopped

    identity  Ida Sommer / @ida.sommer43 / 16 September 1992
    password  8tcklcfzh!X4
    email     cicirahmaputrimu@gmail.com
    profile   Blank caio 2 (633141207085613320)

Credentials are in `~/.adb_bot/accounts/accounts.json` (mode 600), written
*before* the phone was touched, so they survived the run not finishing.

The whole chain, second run: entry → phone → **email hatch** → code screen
(30s) → **`code found in the mailbox`** (9s after switching to Gmail) → code
typed → save_password → password → date_picker → birthday → name → username →
`I agree` → **checkpoint**.

The final screen:

    confirm you're human to use your account, ida.sommer43

Instagram names the account, which it cannot do before creating it. The flow
reported `unknown_screen`, which reads like a failure and makes the recorded
credentials look worthless — when in fact the only work left is the
verification flow this repo already has.

## 4. Changes made

- **`gmail_code.py`: sync is handled before the welcome tour.** They arrive on
  the same screen and the tour's button takes the banner with it.
- **`gmail_code.py`: `sync_enabled_in_dump()` + `PhoneMailbox.sync_enabled()`**
  — ask the sync manager, not Gmail's screen, and refuse the mailbox up front
  with a message naming the switch instead of polling for 210 seconds.
  `None` (could not tell) is deliberately not `False`.
- **`gmail_code.py`: "nothing in primary" / "you've finished!"** added to
  `_EMPTY_MARKERS`.
- **`signup.py`: `SCREEN_CHECKPOINT` / `RESULT_CREATED_UNVERIFIED`.** A named
  outcome for "created, then held for verification", guarded by `progressed`
  exactly like `SCREEN_DONE` so it can never claim somebody else's live phone.

Eight tests added, written from the real dump text. Full suite green.

## 5. What to do next

1. **Verify `@ida.sommer43`.** It is sitting on a `verify_intro` screen; the
   verification flow is already merged on `integration` and has a timer. This
   branch is 31 ahead / 57 behind `integration`, so the two halves of this work
   are not in the same place yet — that merge is the next real task.
2. **One mailbox is one account.** Instagram takes one account per email, and
   the signup address must be the Google account *on the phone*, since that is
   where the code is read. So `Blank caio 2` is now spent: a second account
   there needs a different Google account signed in first. That, not the
   signup chain, is what limits how many accounts a day this can make.
3. **Check sync before spending a launch** on `Blank caio 1` / `Blank caio 3`
   (and on any newly prepared phone). It is one shell command and it is the
   difference between a 210-second dead poll and a working run:

       adb -s <target> shell dumpsys content | grep '^gmail-ls'

   `Blank caio 1` still has the Google-unreachable exit IP problem and
   `Blank caio 3` still would not render, both unchanged from yesterday.
