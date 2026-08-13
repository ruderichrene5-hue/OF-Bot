# Creating an Instagram account by hand — first supervised run, 2026-08-13

Target: `Blank (12)` (`631357418634936369`), a staging phone tagged
`Created`, `Issue`, `logged out`, `Warmup ready, need Bio and Pic` and confirmed
signed-out by a probe earlier today. A staging phone was chosen deliberately
over a model's logged-out phone: a model's phone may only need its password
back, and creating a new account on it would spend something we already own.

Driven with `adb_bot/automation/signup_probe.py`, written for this run — the
phone **stays open between commands** so a person can read each screen and
decide the next move, and every screen is recorded to
`~/.adb_bot/signup/signup-Blank-12-<stamp>/` (dump, screenshot, text, `run.log`).

Everything below is from real screens. TODO_2026-08-13 §5 predicted that marker
lists written from general knowledge would be wrong; **the entry screen alone
proves it** — none of the labels that plan guessed at exist.

---

## 0. Before anything worked: the fleet could not launch a phone at all

The first two launch attempts failed at readiness with MultiLogin answering
`profile is not running; ADB toggle skipped` on every poll. That message is a
symptom, and the real error is only in MultiLogin's own log:

```
16:38:24  mobile profile '631357418634936369' started
16:38:24  error  mobileProcessMeta execution failed ... error: exit status 127
```

`exit status 127` is "cannot execute". At **15:45 the MLX agent self-updated
`phone_launcher` to 0.0.13**, and the binary it installed links against
**webkit2gtk-4.0**:

```
libwebkit2gtk-4.0.so.37   => not found
libsoup-2.4.so.1          => not found
libjavascriptcoregtk-4.0.so.18 => not found
```

This server has only **4.1**. The file is *named*
`phone_launcher_linux_amd64_webkit2_41`, so the name mismatch documented in
`mlx-phone-launcher-filename` was not the problem this time — the file with the
right name had the wrong contents. The update had fetched the plain
`phone_launcher_linux_amd64` artifact and written it under the `_webkit2_41`
name.

**This was fleet-wide, not specific to this run.** Last good launch 15:30;
first failure 16:08; 17 failed launches across at least six profiles by 16:41.
Every posting and warm-up run in that window was launching nothing.

**Fixed** by pulling the correct artifact, which does exist:

```
curl -o /root/mlx/deps/phone_launcher/phone_launcher_linux_amd64_webkit2_41 \
  https://cdn-mlx-prod.multiloginapp.com/phone_launcher/0.0.13/phone_launcher_linux_amd64_webkit2_41
chmod 777 ...
```

The broken one is kept at `...webkit2_41.bak-20260813-webkit40`. Verified by
`--help` returning 0 (the same probe the launcher runs, which was the first
thing to fail), then by a real launch at 16:42:59 with no error after it.

**Expect this on every phone_launcher bump**, the same way
`mlx-agent-launcher-update-fatal` expects it on every launcher bump. The check
is one line: `ldd <binary> | grep "not found"`.

---

## 1. The signup chain, screen by screen

Foreground activity is `com.instagram.modal.ModalActivity` for every screen
below (the launch lands on `BloksSignedOutFragmentActivity` first).

### 1.1 Entry — "Join Instagram"

```
join instagram
share what you're into with the people who get you
clickable:  'Get started'   'I already have a profile'
```

**The plan's guessed markers (`Create new account`, `Sign up`) are not on this
screen.** The real labels are `Get started` and `I already have a profile`.

### 1.2 "What's your mobile number?" — signup is phone-first

```
field:      hint='mobile number'
clickable:  'Mobile number' 'Learn more' 'Next'
            'Sign up with email address'   <- the escape hatch
            'I already have an account' 'Back'
```

Email is one tap away but is **not** the default. A flow that does nothing here
rents an SMS number it did not have to.

### 1.3 "What's your email address?" — pre-filled with somebody else's address

```
field:  hint='email address,'  value='i1aikjgs11a@gmail.com'   <- NOT ours
```

The field arrives **already populated** from the Google account signed in on
the device. An automated "type nothing, tap Next" would have created the
account on the phone's resident mailbox. The flow must clear the field and
verify what it reads back — never assume an empty form.

A `Clear Email address text` button appears once the field has content; that is
a cleaner reset than the driver's current 40-backspace loop.

### 1.4 The floating keyboard eats the button underneath it

Typing the address raised a **floating** keyboard that covers the blue `Next`
button. The UI dump still lists `Next` as clickable at (540, 1267), the tap is
delivered — and the keyboard receives it. Two taps did nothing at all, with
nothing in the dump to explain it; only the screenshot showed why.

Fix that worked: **`input keyevent 4` (Back) to dismiss the keyboard, then
tap.** Back closes the IME without leaving the screen — the typed address
survived. The very next tap submitted.

**This is the highest-value finding of the run for a flow.** A tap that the
dump says landed, on a control the dump says is visible, can be swallowed
silently. Any signup flow needs the keyboard dismissed before every submit.

### 1.5 Submission is visible

After a real submit the button's label becomes `Loading` — a positive signal to
wait on, rather than sleeping a fixed number of seconds.

### 1.6 "Enter the confirmation code"

```
to confirm your profile, enter the 6-digit code we sent to ciciaurainta@gmail.com
field:      hint='code input entry field'
clickable:  'Next'  "I didn't receive the code"  'Back'
```

`I didn't receive the code` is the resend, and it matters: the code outlives a
phone that dies mid-chain (§3), but not by much.

---

## 2. Reading the code is the real gap

`TODO_2026-08-13 §2.1` called email "the cheaper half". It is cheaper per code
and it is **not free of infrastructure** — this run is where that bill arrives.

**IMAP is refused outright:**

```
LOGIN FAILED: [ALERT] Application-specific password required
```

The mailbox has 2-Step Verification on, so Google will not accept the account
password over IMAP at all. For a flow that reads its own codes there are only
three options, and the choice has to be made per mailbox before it is used:

1. **An app-specific password per mailbox** — then `imaplib` works, ~20 lines,
   no phone involved. This is the only option that scales without a device.
2. **Sign the mailbox in on the phone** and read the code in the Gmail app.
   Works, costs the whole detour in §2.1 below, and couples every account to a
   device.
3. **A mailbox provider with an API** (a catch-all domain), which is what §2.1
   of the plan actually proposed, and which nobody has bought yet.

### 2.1 Signing the mailbox in on the phone — the full chain

Chosen for this run at the operator's direction. Recorded because it is the
same chain any "read the code on the phone" approach has to walk:

| Step | What worked |
|---|---|
| Start Gmail | `am start -n com.google.android.gm/.ConversationListActivityGmail`. **`monkey` started nothing again** — the third time this is confirmed on these phones |
| Account switcher | tap the avatar; it is a node whose desc begins `Signed in as` — at (980, 188) here |
| `Add another account` | **not clickable in the dump** (`clickable=false`); its parent takes the tap. Tapped by coordinates |
| Provider list | "Set up email": `Google` / Outlook / Yahoo / Exchange / Other — **none of them clickable in the dump either**, all tapped by bounds |
| Google sign-in | `com.google.android.gms/.auth.uiflows.minutemaid.MinuteMaidActivity`, field hint `email or phone identifierid`, button `NEXT` |
| Password | field hint `enter your password`, and the same keyboard-dismiss dance before `NEXT` |
| "Save password to Google Password Manager?" | answered `NOT NOW` — there is no reason to write our credential into the device's *other* account |
| 2-Step Verification | offered three ways; the only available one was **`Get a verification code from the Google Authenticator app`**. "Tap Yes on your phone" and the SMS option both said "not available at the moment" |
| Choosing it | the exact-label tap did **not** advance; the clickable node is the row `View` at `[0,1306][1080,1559]`, tapped by centre |
| TOTP field | hint `enter code totppin` |

The 2FA seed the operator supplied is a valid base32 TOTP secret; codes were
generated locally with `hmac` + `struct` in ~15 lines, no dependency.

**`input text` into the TOTP field is unreliable.** Typing `056293` produced
`2056293` — a stray leading digit, seven characters in a six-character field.
The driver's read-back check caught it. Whatever is done here needs the
read-back, and probably needs `_clear_field` first even on a field the dump
reports as empty.

### 2.1a Signing the mailbox in on the phone does not work — and it is Google refusing, not us

Tried four times across three phone launches. The result is the same every
time and the failure is **always in the same place**:

- the email is accepted (`Welcome ciciaurainta@gmail.com`);
- the password is accepted;
- **the TOTP is accepted** — proven, see below;
- Google shows its Terms of Service;
- `I agree` is tapped, and the very next screen is
  **"Sorry, something went wrong there. Please try again."**

After that, Android's own account list still holds only the device's original
account:

```
adb shell dumpsys account
    Account {name=i1aikjgs11a@gmail.com, type=com.google}
    Account {name=41416713852, type=www.instagram.com}
```

So the sign-in succeeds and the *account provisioning* fails. Nothing in the
chain we control is wrong; this reads as Google declining to add an account on
this device from this address, which is exactly the risk flagged before
starting. **Treat "read the code in the Gmail app" as closed** unless somebody
wants to fight Google's device checks.

Two things were learned on the way, and both are keepers:

**The TOTP has to be typed and submitted inside one 30-second window.** Driven
one CLI command at a time — each of which re-reads the screen and costs ~20
seconds — the code was always one or two windows stale by the time `NEXT` was
tapped, and Google answered with the same generic "something went wrong" it
uses for everything. It looked exactly like a rejected sign-in. Done in a
single process, typing at `28s left in window` and submitting immediately, the
code was accepted **every time**. A generic error message is not evidence about
which step failed.

**The second Google account is what the device already has.** The device's
resident mailbox `i1aikjgs11a@gmail.com` also holds an Instagram code (5 Aug),
so a regex for `(\d{6}) is your instagram code` over "whatever inbox is open"
happily returned **370185 — the wrong code, from the wrong account, eight days
old**. Any mailbox reader has to confirm whose inbox it is reading before it
reads anything off it.

### 2.2 The device's mailbox has been used for this before

The Gmail inbox on this phone (`i1aikjgs11a@gmail.com`) holds, from **5 Aug**:

```
Instagram — "370185 is your Instagram code"
  Someone tried to sign up for an Instagram account with i1aikjgs11a@gmail.com
```

plus Google security mail from 2 Aug: *2-Step Verification turned on*,
*Authenticator app added as sign-in step*, and *a new sign-in on Oppo Reno14
Pro*. So this staging phone already carries a half-finished manual signup
attempt against its own resident mailbox, and somebody hardened that mailbox by
hand a fortnight ago. Worth knowing before treating `Blank (NN)` phones as
blank.

---

## 3. The phone vanishes after about fifteen minutes, and nothing says so

**Twice, on two separate launches**, at 14 and ~15 minutes in. At 16:57,
fourteen minutes after launching, every command started returning
`adb: device offline`, then `Connection refused`. It was not a reap and not a
collision:

- no loop shut it down — the five shutdowns logged at 16:57:08 name five
  *other* profiles, and this one is not among them;
- the orphan reaper ran at 16:59 and reported `scanned=1 orphans=0`;
- MultiLogin's own launcher log has **no exit line at all** for the session,
  only its startup warnings;
- the MLX agent stayed up on the same pid throughout;
- `POST .../adb/set` then answered `profile is not running`.

So the phone stopped on its own, silently, and the only evidence is that it
stopped answering. All 24 entries in `adb devices` were `offline` at that
moment, which is the fleet's normal background noise rather than a signal.

The second death was identical: launched 17:01:43, unreachable by ~17:17, no
exit line, no loop involved. Two launches, two deaths, both at ~15 minutes,
which is regular enough to plan around rather than treat as bad luck.

**The practical consequence is speed.** One CLI command per screen costs ~20
seconds (dump + screenshot), so fifteen minutes buys perhaps forty screens with
no thinking time — and the Gmail detour alone is a dozen. Driving the same
steps from a single process with screenshots off costs ~3 seconds each, which
is what made the TOTP work at all (§2.1a). **A signup flow must run in one
process and must not screenshot every screen.**

**What this means for a signup flow:** the chain is long — a dozen screens
across two apps — and the device under it can disappear at any point with no
error to catch. The flow has to be **resumable**, and it has to treat
"the phone went away" as an expected outcome with its own result, not as a
failure of the account. A `verification`-style single-pass run that assumes the
phone lives to the end will lose accounts halfway through creation, and the
half-created account is the expensive kind: an email consumed, a code spent,
and nothing to show.

---

## 3a. A partial clear is worse than no clear

Driving the email screen from a script, the field was cleared with 14
backspaces — and it had held the phone's own 21-character address. Seven
characters survived, our address was appended to them, and Instagram accepted
the result without a murmur:

```
field currently holds 'i1aikjgs11a@gmail.com'
field now holds       'i1aikjgciciaurainta@gmail.com'
...
to confirm your profile, enter the 6-digit code we sent to i1aikjgciciaurainta@gmail.com
```

**A confirmation code was sent into a mailbox that does not exist.** Nothing
errored; the flow simply moved on to a code screen that could never be
satisfied. Recovered by tapping `Back` — which returns to the email screen with
the value still editable — clearing properly and resubmitting, after which the
code went to the right address.

Three rules out of one mistake:

- clear by the length of what is actually in the field, not by a guess
  (`max(len(existing) + 6, 45)`), or use the screen's own
  `Clear Email address text` button where it exists;
- **read the field back and compare it to what was intended before submitting**
  — one string comparison would have caught this before a code was spent;
- `Back` on the code screen is a safe way to correct a wrong address.

## 4. Small things worth keeping

- **The recording folder now contains the account password in clear text.** The
  password field renders its value into the UI dump, `read_screen` records the
  dump and its text, so `~/.adb_bot/signup/.../*.xml` and `*.txt` hold it. The
  `--secret` flag only kept it out of `run.log`. Before this is used in
  anger, the recorder needs to scrub known secrets out of what it writes.
- Exact-label tapping fails more often here than in the verification flow.
  Three separate controls on the Google side were `clickable=false` with a
  clickable ancestor. A signup driver needs "tap the clickable ancestor of the
  node whose text is X", not just "tap the node whose text is X".
- `Loading` as a button label is a better wait signal than a sleep.
- Costs so far: **$0.00.** No number rented, no captcha bought. The email path
  really is cheaper — the price is the mailbox plumbing in §2, not per-account
  spend.

---

## 4a. The account that got made — and the whole chain that makes one

**`@cici.aurainta` exists**, created 2026-08-13 ~18:30 UTC on `Blank (12)`,
verified by its own profile page (`0 posts, 0 followers, 0 following`) and by
Android registering a second Instagram account on the device
(`36066701969`, next to the `41416713852` left by somebody's earlier attempt).

| | |
|---|---|
| Handle | `cici.aurainta` |
| Display name | `Cici Aurainta` |
| Password | `akunbaru123@` |
| Date of birth | 12 August 1999 (27) |
| Verified with | rented DE number `+4915905609843`, code `653741` |
| Email on the account | **none** — see the warning below |
| Bio / picture / first post | not done, by design (the human hand-off) |

**It was made with SMS, not email.** After the email path had cost most of an
afternoon (§2, §2.1a), the mobile-number route — which is the screen Instagram
offers *first*, and which this fleet already had proven infrastructure for —
worked on the third number and then again on the first. That is the German
pool's ~1-in-2-or-3 rate, exactly as [[adbbot-sms-verification-providers]]
records it. Total spend: **4 numbers, ~$0.60 each, timeouts refunded.**

### The chain, in order, with what each screen actually needs

| # | Screen | What it wants |
|---|---|---|
| 1 | `Join Instagram` | tap `Get started` |
| 2 | "What's your mobile number?" | the **national part only** — the picker is already `DE +49`, so type `15905609843`, not the `+49` form. `lease.typed_number` is exactly this |
| 3 | "Enter the confirmation code" | the 6 digits. **The field auto-submits on the sixth character** — no `Next` tap is needed, and looking for one wastes a screen read |
| 4 | "Create a password" | ≥6 chars; the button becomes `Loading` while it works |
| 5 | "What's your date of birth?" | opens an **Android date-picker spinner defaulting to today** — accepting it claims the holder was born this year. The three spinners expose editable `numberpicker_input` fields, so type day/month/year and tap `SET`; swiping a year picker 27 times is not necessary |
| 6 | "What's your name?" | the display name |
| 7 | "Create a username" | **pre-filled with Instagram's own suggestion** (`auraintacici` here) — overwrite it or the account takes a name nobody chose |
| 8 | "Agree to Instagram's terms and policies" | `I agree` — this is the tap that creates the account |
| 9 | "Allow Instagram to access your device?" | `Skip` — do not sync contacts; that is what links these accounts to each other |
| 10 | "Add a profile photo" | `Skip` (hand-off) |
| 11 | "Follow 5 or more people" | `Skip` |
| 12 | **"Add an email address"** | **`Skip` — it is pre-filled with the phone's own Google account** |
| 13 | feed personalisation, then a "swipe to access reels" tip | `Skip`, then `Got it` |

Two screens in that list are pre-filled with something wrong (7 and 12) and one
defaults to a value that is actively harmful (5). None of them errors if
accepted.

### ⚠ This account cannot currently be recovered

It has **no email address** and its phone number was **rented and released**.
If it is ever logged out or challenged, there is nothing to prove ownership
with — which is precisely the state that ~16 profiles in the existing fleet are
stuck in. Attaching `ciciaurainta@gmail.com` needs a confirmation code sent to
that mailbox, i.e. it needs the same mailbox access §2 could not get. **Fix the
mailbox question before making more accounts**, or every one of them will be
one challenge away from being unrecoverable.

## 4b. The flow's own first runs — four wrong assumptions, all now fixed

`signup_runner --limit 1 --apply`, four times against live phones on
`Blank (1)`. No account came out of them; each failure was a different wrong
assumption, and every one is now a test.

**1. A submit that is still working is not a submit that failed.** Instagram
does not disable its button while it works — **it renames it to `Loading`**.
The password went through on the very first tap; the screen still said "create
a password"; and the flow spent four rounds hunting a `Next` that no longer
existed before giving up on a verified number:

```
none of ['Next','NEXT','Continue','Done'] is on screen; clickable labels were
['••••••••••••','Password,','Learn more','Loading','I already have an account','Back']
```

Galling, because *"`Loading` is a better wait signal than a sleep"* is written
in §4 of this very document and was not implemented. Screens whose button reads
`Loading` are now waited on, bounded, and the run carried straight on to the
date picker afterwards.

**2. A tap taken from a stale dump loses the dialog.** All three date spinners
were located in one dump — but the keyboard opening *moves* the dialog, so the
second and third taps landed outside it, and a tap outside a dialog dismisses
it. The run became a password ↔ date-picker loop, sixteen steps of it. Each
spinner is now found in its own fresh dump, the run stops rather than tapping
blind if the dialog goes away mid-way, and `SET` is tried **before** Back is
ever pressed, because Back on an open dialog throws away the date just typed.

**3. A phone that has died is not an unrecognised screen.** When the cloud
phone stopped answering, `read_screen` returned nothing and the run reported
`unknown_screen:` with an empty detail — which sends somebody hunting for a
marker list that does not exist. Three empty reads now say plainly that the
phone stopped answering.

**4. One provider's empty wallet is not the fleet's.** The router raised
`InsufficientBalance` straight out of `lease()`, so a signup died on SMSPool's
$0.02 while the 5sim account held **$6.89 and was never asked**. Each provider
has its own wallet; it now falls through, and only says "top one up" when every
provider is broke.

What the runs *did* prove, on real phones: the number is typed as the national
part and read back, `Back` swaps a timed-out number for a fresh one, the
confirmation code is accepted (22s and 52s on the two that delivered), the
password lands as 12 masked characters, and the date picker is reached. The
chain is right; these were four bugs in the driving of it.

**Where it stops now:** SMSPool is down to **$0.02** and 5sim answers
`no free phones` for Germany at this hour. The flow cannot be finished
end-to-end until one of those changes — the fallback works, there is simply no
number to rent.

## 5. Where this run got to

| | |
|---|---|
| Account created | **Yes — `@cici.aurainta`** (§4a), via SMS |
| Email signup | abandoned; reached the code screen twice and could not read the mailbox (§2) |
| Spent | **~$2.40** — 4 German numbers, 2 delivered, timeouts refunded |
| Phone | `Blank (12)`, shut down cleanly, lock released, nothing left running |
| Not done | bio, profile picture, first post (human hand-off); **no recoverable email on the account** (§4a) |
| Recordings | `~/.adb_bot/signup/signup-Blank-12-20260813-*` (six sessions) |

**Email is still the unsolved half**, and it now matters for account *recovery*
rather than for signup:

- IMAP with the account password — **refused**, needs an app-specific password (§2);
- the Gmail app on the phone — **refused by Google after its Terms screen** (§2.1a);
- Chrome on the phone — **cannot reach `mail.google.com` at all**,
  `ERR_SSL_PROTOCOL_ERROR`, so the profile's proxy breaks it;
- an app-specific password → `imaplib`, ~20 lines — **untried, and the only
  option that scales without a device**;
- a person reads the inbox — works now, does not scale.

## 6. What the flow needs, restated from evidence

1. **Settle the mailbox story** (§2, §5). Everything else is screens, and the
   screens are now known; code delivery is the only unbuilt part, and two of
   the three ways to build it are already ruled out.
2. **Run in one process, screenshots off** (§3). Fifteen minutes of phone is
   the budget, and a per-screen CLI spends it on dumps.
3. **Dismiss the keyboard before every submit** (§1.4).
4. **Clear by measured length, and read the field back before submitting**
   (§3a) — this run mailed a code to a mailbox that does not exist.
5. **Make it resumable** (§3), keyed on the MLX profile, so a phone that dies
   at screen nine does not cost the account.
6. Only then write `flows/signup.py` with the markers above — they are from
   real dumps, which is the one kind that has worked first time.

## 7. What is still unknown

- **The username-taken suggestion list.** `cici.aurainta` was free, so
  Instagram never argued. TODO_2026-08-13 §2.2 calls this the fiddly screen and
  it remains unseen.
- **Whether the account survives.** It is minutes old. Bans on new accounts
  often land in the first 24–72 hours, so the only honest measurement is to
  look again tomorrow and after warm-up.
- **The photo/video challenge**, which this signup never triggered.
- **Whether a second account made from the same phone behaves differently** —
  this device now holds two Instagram registrations, and contacts sync was
  declined precisely so they are not linked.
