# Creating five accounts on five blank phones — what stopped it, 2026-08-16

Asked for: five blank MLX profiles, five Gmail addresses out of the
`VA - ML I IG` base, five new Instagram accounts, and the signup flow fixed
along the way.

**No account was created.** Two things block it, and neither is in the code
that was there to be fixed:

1. **There are not five blank phones.** There are two candidates, and only one
   of them is confirmed. The list the runner offered was wrong in a way that
   would have done damage.
2. **The email chain needs a mailbox on the phone**, and no blank phone has
   one. IMAP is refused on every mailbox in the base, re-checked today.

What *was* done: the flow no longer picks phones by name, no longer reports a
success it did not earn, and now has the email chain it never had — the one
that leaves an account recoverable. All of it is tested; the parts that need a
phone are marked as needing a supervised first run.

---

## 1. The runner offered eight empty phones. Four had accounts on them.

`signup_runner --list` picks any MLX profile whose name starts with `Blank` or
`Default profile name` and which carries none of seven "busy" tags. Today that
was eight phones:

```
Blank (10)                 tags=['Active / Posting', 'Task A', 'Task B']
Blank (21)                 tags=[]
Blank (5)                  tags=['Task A', 'Task B', 'Warmup Day 3 Done']
Blank (6)                  tags=['Task A', 'Task B', 'Warmup Day 3 Done']
Blank (7)                  tags=['Active / Posting', 'Task A', 'Task B']
Default profile name (4)   tags=[]
Default profile name (42)  tags=['Active / Posting', 'Task A', 'Task B']
Default profile name (43)  tags=['Active / Posting', 'Task A', 'Task B']
```

The tags were telling the truth and the names were lying.

**`Default profile name (42)` was launched and looked at. It is running a
logged-in Instagram account** — populated feed, story tray with its own
avatar, profile picture in the nav bar. The prod base has no account linked to
it and no posting queue rows, and the Run Log has nothing for it either, so
every cheap source said "empty" and the phone said otherwise.

**`Blank (5)`, `(6)`, `(7)` and `(10)` carry 15–42 Run Log rows each**, from
2026-08-06 to 2026-08-14, including `warm_up_process` runs that finished
`Done`. Warm-up runs against profiles tagged `Created` — i.e. ones that are
already somebody's account.

The Run Log's `Profile` column reads `Blank (5) [257879]`, name **plus a queue
id**, which is why an exact-name lookup finds nothing and a substring match
finds fifteen rows. Anything keying on that column has to split on `" ["`.

### Why this mattered more than a bad list

`run_signup`'s first action is to read the screen. A logged-in phone shows a
healthy feed, which classifies as `done`, which was the success case:

```
if screen == SCREEN_DONE:
    return SignupResult(status=RESULT_CREATED, ...)
```

So `--apply` on `Default profile name (42)` would have printed
`CREATED @<invented-username>`, written credentials to
`~/.adb_bot/accounts/accounts.json` for an account that does not exist, and
counted it toward the batch. A false success on somebody else's live phone.

**Fixed** (`RESULT_OCCUPIED`): a `done` screen only means "created" if this run
has actually typed something — a number, a code, a password, a name. Otherwise
it is the account that was already there, and the run says so and touches
nothing.

### What the fleet actually has

Every MLX profile, by tag:

```
107  Active / Posting        23  Banned / Dead        9  gmail
 92  Task B                  20  unable to verify     8  2 accounts
 65  Task A                  18  logged out           4  Warmup Day 3 Done
 63  Issue                   12  Created              3  tests
 46  Account creation done   11  Second Account       3  Warmup Day 2 Done
 32  review                   9  Warmup ready...      3  Link  / 1 Ready for Posting
```

**Four profiles out of 207 carry no tag at all**: `Blank (21)`,
`Default profile name (4)`, `Jil 11`, `coymeo switchrn`. The last two are named
for models and are not staging phones.

So the honest supply is **two**, of which:

* **`Default profile name (4)` — confirmed empty.** Launched, Instagram opens
  on its signed-out login form, `dumpsys account` lists no Google account at
  all. Gmail, Play Store and Instagram are all installed.
* **`Blank (21)` — unconfirmed.** Two launch attempts; MLX's own launcher log
  says `mobile profile '631357418635526193' started` while its API kept
  answering `profile is not running; ADB toggle skipped` for fourteen
  readiness attempts. The launcher and the API disagree about the same
  profile.

Making more blank profiles is a MultiLogin action nobody has automated —
`ONBOARDING_A_MODEL.md` is explicit that all of it is by hand — and it costs
money, so it was not done unasked.

## 2. Reading the mailbox: IMAP is dead, re-checked

`SIGNUP_RUN_2026-08-13.md` §2 found IMAP refused with
`Application-specific password required` on a mailbox with 2-Step Verification
on, and left open whether a mailbox *without* 2FA would work. Answered today,
against five addresses from the base's own rows:

```
REFUSED emanuelnewbyp601@gmail.com     (2FA No)     [AUTHENTICATIONFAILED] Invalid credentials
REFUSED frankmartaz518@gmail.com       (2FA No)     [AUTHENTICATIONFAILED] Invalid credentials
REFUSED juanstalldev681@gmail.com      (2FA No)     [AUTHENTICATIONFAILED] Invalid credentials
REFUSED hasan428483@gmail.com          (2FA Yes)    [ALERT] Invalid credentials
REFUSED 720qy9m7a6rek3sanc2hez@...     (2FA unset)  [ALERT] Invalid credentials
```

Account passwords do not open IMAP any more, 2FA or not. That leaves the two
options the earlier run named: **an app-specific password per mailbox** (still
the only one that scales without a device, and still nobody's decision), or
**the Gmail app on the phone**, which is what the new code drives.

Both need the mailbox signed in *somewhere*. On these phones that is the Play
Store's `Sign in`, not Gmail's own — proven 2026-08-13, and the account then
persists on the MLX profile. **No blank phone has a Google account on it
today**, so every one of the five would need that chain run first, and it is
still a by-hand chain.

## 3. Instagram's entry screen wedges, and force-stop is the way out

On `Default profile name (4)`, tapping `Create new account` on the login form
opened `com.instagram.modal.ModalActivity` that rendered **black** — a 22 KB
screenshot of nothing but the nav bar — while `uiautomator` kept dumping the
login form *underneath* it. `Get started` on the "Join Instagram" screen did
not advance either, twice, and neither did `I already have a profile`, so it
was not the signup path specifically.

Two red herrings ruled out along the way:

* **Not the network.** The phone pings `8.8.8.8` at 1.5 ms, resolves and
  reaches `instagram.com`, and leaves from `109.41.176.77` — a German Vodafone
  mobile IP, exactly what it should be.
* **Not the digitizer.** `getevent -p` reports the touch device as 0..720 ×
  0..1080 against a 1260 × 2800 display, which looked like the answer; it is
  not. Taps land normally after a restart, and `KEYCODE_HOME` worked
  throughout.

**The state survives a relaunch** — the profile came back up in the same wedged
modal on the next launch, and `uiautomator dump` then fails outright rather
than returning the wrong screen. `am force-stop com.instagram.android` followed
by `am start` clears it and lands on `Join Instagram`, confirmed twice.

Safe to do **only on a phone with no account and no signup in flight**: a
force-stop mid-chain returns to "Join Instagram" and throws away an
already-verified number.

## 4. What changed in the code

* **`signup_preflight.py` (new)** — decides blankness by asking the phone.
  Cheap sources (tags, Run Log, base links) only narrow what is worth
  launching; the verdict is Instagram's own first screen. `BUSY_TAGS` grew from
  7 tags to 17, including the four that were on live phones today.
* **`signup.py`** — `RESULT_OCCUPIED` (above); the **email chain**: the
  `Sign up with email address` escape hatch off the mobile-number screen, the
  email field cleared-and-typed rather than accepted, and the code read from
  the mailbox instead of an SMS lease. `RESULT_MAILBOX` says which of the four
  mailbox failures happened.
* **`flows/gmail_code.py` (new)** — reads Instagram's code out of the Gmail
  app. Refuses to read a mailbox it cannot prove is ours (these phones ship
  with a resident Google account, and the one on `gmail test` held an Instagram
  code of its own from 5 August); tells "not syncing" apart from "no mail";
  switches apps with `am start` and never force-stops.
* **`totp.py` (new)** — codes for the Play Store sign-in, verified against all
  four RFC 6238 SHA-1 vectors. `fresh_code` waits out the tail of a window
  rather than handing back a code that expires mid-typing, which is the failure
  that cost the 2026-08-13 run an afternoon and reads exactly like a wrong
  password.
* **`signup_mailboxes.py` (new)** — the pool, out of the VA base with its own
  token. 170 rows, **45 unclaimed, 15 of those with a 2FA key**. Also writes a
  created account back into `Profile Creation (VA)` and links its mailbox,
  which is what claims the address.
* **`verification_probe.instagram_installed`** — an empty `pm list packages`
  was being read as "Instagram is not installed". `Blank (21)` was written off
  that way seconds after `glogin success`: the shell was up, the package
  manager was not. Now retried three times, and the verdict is only given when
  `pm` actually answered.

85 tests pass across the signup, mailbox and TOTP suites — 11 of them for the
email chain, 12 for the mailbox reader, 8 for the codes. The repo's full suite
is 2459 passing; the one failure, `test_report.py::CpuProcessesTest`, samples
real CPU processes over a 50 ms window and passes on its own — nothing here
touches `report.py`.

## 5. What it would take to finish

1. **Three more blank profiles** (or a decision to reuse specific ones). Only
   `Default profile name (4)` is confirmed empty; `Blank (21)` needs a launch
   that succeeds.
2. **A mailbox on each phone.** Either the Play Store sign-in run by hand per
   phone — about eight screens, chain recorded in §2.1c of the 08-13 write-up,
   and `totp.py` now supplies the codes — or the app-password decision, which
   makes IMAP work for all 45 free mailboxes and needs no phone at all.
3. **A supervised first run of the email chain.** Every marker in it comes from
   a real dump of the 08-13 run, but no run has walked it end to end with the
   code being read by machine rather than by a person.
