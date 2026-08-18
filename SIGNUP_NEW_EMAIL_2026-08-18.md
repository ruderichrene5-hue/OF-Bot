# How fast is a new account on a new email? — 2026-08-18

Continues [SIGNUP_RUN_2026-08-18.md](SIGNUP_RUN_2026-08-18.md), which made
`@ida.sommer43` on `Blank caio 2` and closed by saying that phone was **spent**:
one mailbox is one Instagram account, so a second account there needs a
different Google account signed in first, and *that* — not the signup chain — is
what limits accounts per day.

This run measured exactly that: same phone, Instagram already installed, a
**new** mailbox. The answer is not one number, so the sections below give the
stages, because which stage costs what is the part that decides how to run this.

## 1. The headline

A new email on a phone that already has Instagram costs **two launches**, not
one, and the second is the cheap one:

| stage | cost | needed again next time? |
|---|---|---|
| profile launch → ADB usable | 90–210s | every launch |
| Google sign-in, **cold** | 570–750s | no — persists on the profile |
| Google sign-in, warm | ~0s (`already_signed_in`) | — |
| Instagram + Gmail installs | seconds when already installed | no |
| Gmail sync switch | seconds | no |
| the Instagram signup itself | ~4–5 min | — |

So: **~13–16 minutes for the first account on a mailbox that has never been
signed in**, which does not fit the ~15 minutes a phone lives — and **~6–7
minutes once the mailbox is on the phone**. The chain was never the slow part.
The cold Google sign-in is, and it is a one-off per phone.

## 2. The mailbox decides the run, and most of the pool cannot be used

The first attempt used `hasan428483@gmail.com`, an unclaimed pool row carrying a
2FA key — on paper exactly what `signup_mailboxes --take` is for. Google
answered the *address*, before asking for a password:

    verify that it’s you ... confirm that you're not a robot

That is a captcha. There is no automating past it, the run had spent 5.5
minutes to find out, and it reported `unknown_screen`, which reads as "the flow
got confused" and invites a retry that lands in the same place.

`cicireynaamelia@gmail.com` — from the 2026-08-12 batch, the one the earlier
runs used — walked straight through. **The batch a mailbox comes from predicts
whether Google will accept it**, and the 45-row "usable" count in
[the pool note](../memory) is optimistic: usable means *Google will let it sign
in from these phones*, which is not a column in the base.

Ask this before spending a launch, not after.

## 3. Four false failures, all fixed

Every stall today reported something that pointed at the wrong thing. That is
the same lesson the verification work learned, and it is why the fixes are
worth more than the account:

1. **The robot check read as `unknown_screen`.** Now `google_robot_check`, whose
   message says the mailbox has to change.

2. **A password Google was still checking was retyped every pass.** The dump
   said `welcome loading indeterminate, loading` *with the password still in the
   field* — Google was thinking, and the four-repeat guard ran out before it
   finished. Run 2 died `mailbox-stuck` at 356s with nothing wrong. The code
   screen has had this wait since 2026-08-17; the password form now has it too,
   and run 3 cleared the same screen in ~190s.

3. **A finished sign-in read as `unknown_screen`.** Run 3 ended 750s in on
   Google's own confirmation — `signed in as cicireynaamelia@gmail.com` — and
   called it a failure. `dumpsys account` now gets the last word on any screen
   the classifier cannot name.

4. **Gmail sync off refused the mailbox outright.** Every freshly added account
   arrives with mail sync off, so this is the state a *new* mailbox always
   starts in — meaning a new email could never be used inside one launch.
   `PhoneMailbox.enable_sync()` now walks Gmail's own settings to the switch and
   confirms against the sync manager rather than the checkbox.

Also worth having: the Play Store's `Sign in` button only exists while the phone
carries **no** Google account. Once one is on there the store opens on its home
screen with nothing to press, and `sign_in` returned `stuck` — which made "this
phone already has a mailbox" look like a fleet fault. A second account now goes
on through Android's `ADD_ACCOUNT_SETTINGS` wizard. This did not fire today
(§4), but it is the case the *third* account on a phone will hit.

## 4. `Blank caio 2` had lost its Google account

Unexpected, and it changes the ceiling. Yesterday's note says a sign-in persists
on the profile, and it does — but this phone came up with its Play Store
**signed out** and `dumpsys account` empty, so `cicirahmaputrimu@gmail.com` was
gone. Nothing here removed it.

Two consequences. The good one: the phone was not "spent" after all, so a new
mailbox went on cleanly. The bad one: **`@ida.sommer43`'s mailbox is no longer
on any phone**, so the checkpoint it is held at cannot be answered by mail until
that account is signed in again — and whatever removed it can do so again.

Instagram survived it. So did the installs.

## 5. Launching this phone is the least reliable part of the day

Five launches, three different outcomes: 90s, 134s, 204s to a usable ADB, one
`not-ready` after 10 readiness attempts, and one that needed 11. MultiLogin
answers `mobile profile ... started` and its own API then says `profile is not
running; ADB toggle skipped`. This is the `Caio-tests` intermittency already on
record — retry in rounds, do not read it as a fault — but it means **a plan that
needs two consecutive launches should budget a third**.

## 6. What to do next

1. **Sort the mailbox pool by whether Google will accept it**, not by whether a
   2FA key is present. One column, filled in as runs discover it, would stop
   every future run rediscovering §2 at 5.5 minutes a go.
2. **Sign `cicirahmaputrimu@gmail.com` back onto a phone** if `@ida.sommer43` is
   wanted — see §4.
3. **The verification flow is still the last mile.** An account created this way
   lands on `confirm you're human`; that flow is merged on `integration` and this
   branch is not, so the two halves of the work are still in different places.
