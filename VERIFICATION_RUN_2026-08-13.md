# Verification run, 2026-08-13

Two passes, 14 profile-launches, **$1.22**. Five fixes, all found by watching
the run rather than by reading the code.

## Pass 1 — ten flagged profiles, as they came

`verification_runner --limit 10 --apply`, 14:08-14:29. **$0.002** (two captcha
solves; no numbers rented).

| Profile | Result | What it was |
|---|---|---|
| Jasmin 5 | solved | no challenge at all — see §5 |
| Blank (13), (14), (15) | needs_human | code screen for a number we do not own — §1 |
| Blank (12) | signed_out | genuinely needs credentials |
| Blank (9), Default (40) | needs_human | no Instagram installed — **and logged nothing** (§2) |
| Default (38) | needs_human | Instagram's own "an unexpected error occurred" |
| Default (44) | failed | captcha typed, then a screen with no UI dump — §3 |
| Jasmin 6 | error | Instagram would not open |

16 further profiles were skipped for free on tags a VA had already written.
That mechanism paid for itself again: at ~2 minutes a launch it saved half an
hour of doing nothing.

## Pass 2 — the same profiles, against the fixes

`--only <four launch ids> --apply`, 14:33-14:54. **$1.22**, 3 numbers.

| Profile | Result |
|---|---|
| **Blank (13)** | **solved** — change number → our number → code in 40s |
| **Blank (15)** | **solved** — first number timed out and was refunded, second worked |
| Blank (14) | error — MultiLogin said ready, adb never saw the device |
| Default (44) | needs_human — honestly, and for $0.001 instead of $0.002 (§3) |

Both solved profiles had been tagged `unable to verify` and benched for seven
days by pass 1, three hours earlier.

---

## 1. A code screen waiting on somebody else's number

Three of the first four profiles opened on *"enter the 6-digit confirmation
code we sent via sms to +49…"*. The bot cannot read that phone, so each was
settled as terminal.

Every one of those screens carried an **`Update mobile number`** link, and the
driver already knew how to press it — `request_new_number` uses that exact
label list whenever one of *our* numbers times out. The path just never tried
it when the number was not ours. Blank (15) had burned three of our numbers the
day before, so the number it was waiting on was most likely one of ours,
already refunded: nothing about the account was wrong.

Guarded three ways, because pressing something on an unexpected screen is how a
chain gets abandoned: only when the link is really on screen
(`request_new_number` falls back to Back, which from here *leaves* the flow),
only once per run, and still inside the three-number cap.

**Two profiles recovered from terminal.**

## 2. Profiles that settled before the loop said nothing at all

Blank (9) and Default (40) logged `launching` and then silence — while
collecting a status, a detail and a seven-day bench. Both phones have no
Instagram installed, which is detected *before* the screen loop, and the result
line lived *inside* that loop. The line now belongs to the pass and fires for
every outcome, plus a branch for "no outcome recorded" so silence cannot come
back quietly.

## 3. Never buy an answer for a screen nothing can be typed into

Default (44): the answer `312129` went into the field, `Next` was pressed, and
43 seconds later the phone — still working; its own intro screen had said this
"takes about 30 seconds" — produced **no UI dump**. OCR read the picture, which
still showed the captcha screen with `312129` in its field. So the run called a
possibly-correct answer wrong, reported it back to 2captcha, bought a second
solve off the whole screenshot, and failed with `no input field on screen`.

OCR text is a picture of words: it can be read, and nothing on it can be tapped
or typed into. The handler now asks how the screen was read before it spends,
waits 20s and looks again (twice), and only then hands back — and the check
sits above the report-incorrect call, so an answer is only called wrong on a
screen we could read.

**Still open:** this phone never produced a dump across three looks and a
minute of waiting, so whether `060049` was accepted is unknown. Two candidate
next steps, neither yet tried: dismiss the IME before dumping (a keyboard that
never idles is the likeliest cause), or type using the field bounds from the
dump taken *before* submitting.

## 4. `--only` could not name one profile of three

The ambiguity warning has always ended "— use the id", and there was no way to
do that. A re-run aimed at four profiles planned three called `Blank (13)`, and
pushed the two actually wanted off the end of the limit. `--only` now matches
launch ids as well as names.

## 5. A solve that cleared nothing read like a solve

**Jasmin 5 was tagged `Issue` at 04:40 today for `Retries Exhausted`** — a
posting failure, not a challenge:

    04:40:50  tag Jasmin 5 -- Retries Exhausted
    14:26:41  verification: solved
    14:33:21  unflag Jasmin 5 -- Issue tag removed

The pass launched a healthy phone, found nothing to do, and reported `solved`
in the same words it uses for a captcha it actually answered — as it had for
the same profile the day before. A solve now names what it cleared
(`cleared phone, code`), or says there was no challenge to clear.

**This is worth a decision.** The `Issue` tag is the whole interface, and it is
applied for reasons verification cannot fix. Skipping "Retries Exhausted"
outright would be wrong — posts can fail *because* of a challenge — but the
tally cannot keep counting those launches as verification successes.

## Open questions for a person

1. **Phones with no Instagram** (Blank (9), Default (40)) need provisioning.
   No tag in the workspace means that; the closest are `logged out`,
   `unable to verify`, `Banned / Dead`. Add one, or fold it into an existing
   tag? Until then these stay flagged with no visible explanation.
2. **Top up SMSPool** — $3.07 left, about 5 numbers. 5sim holds $6.89.
3. `SMS_VERIFICATION.md` still says the device driver is unwritten and that
   "nothing calls `run_verification` yet". Both have been false since
   2026-08-12.
