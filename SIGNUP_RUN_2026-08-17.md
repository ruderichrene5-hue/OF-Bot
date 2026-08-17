# Signup run, 2026-08-17

Continues [SIGNUP_RUN_2026-08-16.md](SIGNUP_RUN_2026-08-16.md). Same three
phones in the `Caio tests` folder, same three `cici*` mailboxes.

**Where it got to: no Instagram account was created.** Every stage of the chain
now works on a real phone except the last one, and the thing blocking that last
stage at the end of the day is Instagram rate-limiting the address, not a bug.

## 1. What now works that did not yesterday

| Stage | Yesterday | Today |
|---|---|---|
| Play Store Google sign-in | eight screens deep, never finished | **finishes** — `Blank caio 2` is signed in |
| Instagram install | never reached | **installed** |
| Gmail install | assumed preinstalled (it is not) | **installed** |
| Instagram signup to the code screen | never reached | **reached, repeatedly** |
| Reading the code | never reached | inbox opens on the right account; not yet read |

`Blank caio 2` is a fully prepared phone: Google account signed in, Instagram
and Gmail installed, Gmail opening on the right inbox.

## 2. The fixes, and what each cost to find

Every one of these was a real phone ending a real launch.

**Google's forms do not submit from a tap.** The email form, the password form
and the 2FA code screen all take the value, read it back out of the field,
accept a tap at the bounds the dump reports — and redraw. Pressing the
keyboard's own IME action is the one submit a tap cannot reproduce, and it is
what moves all three. This was yesterday's open question; it is answered.

**Do not type a fresh code over one Google is still checking.** The run where
2FA was finally accepted is the run this lost: after the submit, the code field
is still on screen with the code in it and a spinner beside it, so the flow read
"still on the code screen" and typed over a submission in flight.

**Two screens shared a heading and needed separating.** The 2FA *chooser* and
the *code entry* screen both say "get a verification code from the Google
Authenticator app"; only one has a field to type in. Classification now takes
the field hints.

**Three screens were unnamed and each ended a run:** Google unreachable
("problem communicating with google servers" — no buttons at all, so the app
must be restarted), Google's retry page ("something went wrong *there*" — one
`Next`), and the services consent list that follows the Terms (a long scrolling
page whose button is `More` until the bottom).

**An empty dump is a failed read, not an unknown screen** — and on these phones
`mCurrentFocus=null` with nothing to dump means a sleeping phone, which is now
woken rather than given up on.

**Three substring bugs, all the same shape.** `pm list packages
com.google.android.gm` matches **com.google.android.gm*s*** — Play Services, on
every phone — so Gmail reported installed on a phone that never had it, and
three launches went looking for a mail app that was not there. "Play" matched
inside "Google **Play** Pass", so a promo sheet read as a finished install. A
bare "%" in a page of ratings and reviews meant "downloading", so Gmail's
listing sat four minutes with an `Install` button on screen.

**Gmail was behind its own permission dialog.** Five launches ended on "Gmail
would not come to the front". Logging the focused window showed it was never
Gmail's problem: Gmail starts, immediately asks for a runtime permission, and
`com.android.permissioncontroller` holds the focus — so every later candidate
activity was started behind the same dialog. Notifications are now granted with
`pm grant` outright, which is also the permission the notification-shade read
depends on.

**Nothing that merely contains the address proves it is the inbox.** Instagram's
own confirmation page says "we sent to <address>", and Gmail's *compose* window
carries it in the `From` field. Both passed the "is this our mailbox?" check;
the compose window was read for 210 seconds. Gmail must be confirmed in front,
every pass, and a compose window is backed out of.

## 3. The one stage still unfinished

Reading Instagram's code out of the mailbox. It is now approached two ways:

1. **The notification shade** (`dumpsys notification`), which needs no app in
   front and cannot be a welcome tour or a compose window. This is the primary
   path and the right one.
2. The Gmail UI, kept as a fallback.

The last run to reach the inbox found it open on the right account with **13
unread messages** and Gmail reporting *"account sync is off — turn it on in
Account settings"*. The flow now follows that banner to the switch. That change
has not yet had a launch that got past Instagram to exercise it.

## 4. Why the last few launches failed, and it is not the code

`cicirahmaputrimu@gmail.com` was submitted to Instagram roughly a dozen times
today. Instagram's email screen went from answering in about thirty seconds to
spinning through an entire 30-screen budget without ever reaching the code
screen. That is throttling, and no amount of retrying inside one launch fixes
it.

`Blank caio 1` is a separate problem: Google was unreachable three times running
through that profile's exit IP, having worked earlier — six sign-in attempts
through one German mobile exit in an hour. `Blank caio 3`'s phone would not
render at all (`mCurrentFocus=null`, empty dumps, Play Store would not start),
the same way it failed yesterday.

## 5. What to do next

Wait for the throttle to decay — hours, not minutes — then:

    python -m adb_bot.automation.signup_phone --profile "Blank caio 2" --apply

Sign-in and both installs persist on the profile, so that run starts at the
signup and should reach the code screen in about ninety seconds. The untested
paths are the sync switch and the shade read.

If Instagram keeps refusing that address, the alternative is a different mailbox
— but note the signup address must be the Google account **on the phone**, since
that is where the code is read, so a fresh address means signing a different
Google account in first. That is now a reliable chain rather than a gamble.
