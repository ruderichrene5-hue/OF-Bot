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

## 5. Where this run got to

| | |
|---|---|
| Account created | **No** |
| Instagram signup | reached the confirmation-code screen, twice, with the code sent to `ciciaurainta@gmail.com` |
| Blocked on | reading that code — see §2 |
| Spent | **$0.00** (no number, no captcha) |
| Phone | `Blank (12)`, shut down cleanly, lock released, nothing left running |
| Instagram state | none — the signup resets to "Join Instagram" on every relaunch, so no half-made account is sitting anywhere |
| Recordings | `~/.adb_bot/signup/signup-Blank-12-2026081[3]-*` (three sessions) |

**The one thing standing between this and a created account is the six-digit
code**, and every way of reading it is now either tried or costed:

- IMAP with the account password — **refused**, needs an app-specific password (§2);
- the Gmail app on the phone — **refused by Google after the Terms screen** (§2.1a);
- an app-specific password → `imaplib`, ~20 lines — **untried, and the only
  option that scales without a device**;
- a person reads the inbox and hands the code over — works now, does not scale.

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

Everything past the confirmation code. Nobody here has yet seen Instagram's
**name**, **password**, **birthday** or **username** screens, nor the
username-taken suggestion list that TODO_2026-08-13 §2.2 calls the fiddly one.
Those markers cannot be written until a code is entered — which is another
reason the mailbox decision is the whole critical path.
