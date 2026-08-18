# Working a flagged profile through Instagram's verification chain

Status: **the logic is built and tested; the device half is not written yet.**
Everything below runs today except the part that actually taps the phone — see
[What is left](#what-is-left).

## The problem

A profile flagged `Human Verification Required` is parked until a person clears
it. Instagram will let a bot clear most of them, but it asks for two or three
things in **no fixed order**:

- an image captcha — "type the characters you see" (usually first);
- a phone number, then the SMS code sent to it;
- a photo, to prove there is a person.

Anything written as a fixed sequence of steps works for one account and breaks
on the next.

## The shape of the solution

`adb_bot/automation/flows/verification.py` is a loop, not a script:

```
read the screen -> name what is on it -> do the one thing that screen needs -> repeat
```

Each pass stands alone, so the order stops mattering, and a screen that comes
back twice (Instagram re-asking after a resend) is handled by the same code that
handled it the first time. `classify_challenge()` is a pure text-in / label-out
function, so the whole ordering question is unit-tested without a phone.

One subtlety worth knowing about, because it is the expensive one to get wrong:
the phone screen and the code screen each contain the other's words — the phone
screen says *"enter your mobile number to get a confirmation code"* and the code
screen says *"enter the code we sent to your phone number +1…"*. Matching on
topic words alone reads one as the other, which either throws away a number that
is seconds from receiving or waits 45 seconds for an SMS nobody requested. Each
screen therefore has *strong* markers (the action being asked for) that decide,
and *weak* ones (the topic) that only break a tie.

Screen text is matched against **visible text only** — element `text` and
`content-desc`, or OCR. Never the raw XML of a UI dump: Instagram ships resource
ids containing words like `confirm` and `verification` on ordinary screens, which
is how 17 of 29 profiles were once flagged for verification they did not need.

## Providers, failures, and the switch

`adb_bot/clients/sms/` rents the numbers. SMSPool is primary, 5sim is the
fallback, and the switch is automatic:

| rule | value |
|---|---|
| wait for a code | 45 s |
| consecutive failures before switching | 10 |
| cooldown before the benched provider is retried | 30 min |

- A code arrives → the failure count resets to 0.
- No code in 45 s → the number is **cancelled for a refund**, and the count goes
  up by one.
- The count hits 10 → log `Primary provider unreachable/failing. Switching to
  Secondary Provider.`, bench that provider for 30 minutes, switch, reset to 0.

Three decisions the specification did not cover:

**The counter lives on disk** (`~/.adb_bot/sms_breaker.json`), not in a global.
Each loop here is a separate systemd invocation that starts, drives a batch and
exits, so an in-memory counter would reset every run and a pool failing twice per
run would never reach 10 — the breaker would never once fire.

**Which provider is active is derived from the cooldowns, not stored.** "The
first provider that is not benched" gives the specified behaviour in both
directions — trip SMSPool, get 5sim; 30 minutes later, get SMSPool back — with
one source of truth and no timer to fire.

**A provider that cannot sell a number counts as a failure too.** A burned pool
shows up as "no numbers available" at least as often as it shows up as a number
that never receives.

Two things deliberately do *not* count as pool failures: an empty wallet, which
switching providers cannot fix and which needs a person; and a 5sim cancel it
refuses inside its own minimum window, since such an order self-refunds when it
times out anyway.

## The captcha

`adb_bot/clients/captcha.py` sends the image to 2captcha (`ImageToTextTask`) and
types the answer back. A rejected answer — which shows up as the captcha screen
simply coming round again — is reported back to the service, which refunds that
solve and feeds its worker scoring.

The solver never guesses: anything it cannot read comes back as `None` and the
profile stays with a human, which is the state it was already in. With no
2captcha key configured the same thing happens, so the feature is absent rather
than broken.

## Configuration

All three credentials are read from `/etc/adbbot/env` (or the app's dev settings,
same as every other token here). None of them belong in the repo — the
`test_no_hardcoded_secrets` suite fails the build if one lands there.

```
SMSPOOL_API_KEY=…
FIVESIM_TOKEN=…
TWOCAPTCHA_API_KEY=…
```

Each is optional. With only one number provider configured there is no fallback
to switch to, and the router says so at startup.

## Command line

```
python -m adb_bot.clients.sms.cli balance   # credit at all three services
python -m adb_bot.clients.sms.cli state     # active provider, failure count, cooldowns
python -m adb_bot.clients.sms.cli reset     # clear the count and un-bench everything
python -m adb_bot.clients.sms.cli rent      # rent one real number end to end (~$0.42)
```

`state` answers "why is verification suddenly using 5sim?". `reset` is the
override for when a provider has been fixed and there is no reason to sit out the
rest of its 30 minutes.

## What is left

**The device driver.** The loop drives a `ChallengeDriver` — read the screen,
type in a field, tap the button, upload a photo, screenshot the captcha. The
orchestration is pure logic and is tested against a fake driver; the real
implementation has to be written against a live flagged phone, where the actual
selectors can be seen rather than guessed. Its shape is the `ChallengeDriver`
protocol in `flows/verification.py`.

**A live rent.** `cli.py rent` exercises purchase → poll → refund against the
real SMSPool API. It has not been run yet, so SMSPool's `/purchase/sms` response
shape and its `/sms/check` status numbers are handled defensively rather than
confirmed — the code decides on the presence of SMS text first and treats an
unknown status as "still pending", so an unseen status number cannot make it
give up early. `_DEAD_STATUSES` in `smspool.py` is the single place to correct
if SMSPool ever publishes the full table.

**Wiring into the loops.** Nothing calls `run_verification` yet. It returns a
`VerificationResult` with a status (`solved` / `needs_human` / `banned` /
`stuck` / `failed`) and a human-readable detail, which is what the Airtable
write-back and the `Issue` tag clearing would key off.
