# Reel posting reliability — audit

**Flow audited:** `InstagramReelUploadU2Flow` (`instagram_reel_upload_u2`) — the one
`posting_runner.POST_FLOW` and `lifecycle.FLOW_REEL` actually use. The retired
dump/OCR flow was not audited.

**Question:** why do some profiles fail to post, and what stops this running
unattended?

Findings are ordered by how much manual work each one causes.

---

## 1. Failed posts are never retried — "Needs Retry" is a label with no mechanism

**This is almost certainly why you are intervening by hand.**

When a post fails, `apply_post_result` ([posting_runner.py:79](adb_bot/automation/posting_runner.py:79)) writes:

- Post Status → **Failed**
- Issue Type → **Failed - Needs Retry**
- Retry Count → +1

But the queue is read with a hard filter on Pending, in two places:

- [`airtable.list_pending_posts`](adb_bot/clients/airtable.py) — `filter_formula = {Post Status}='Pending'`, so a Failed row is never even *fetched*
- [`posting_planner.py:107`](adb_bot/automation/posting_planner.py:107) — skips any row whose status is not `None`/`Pending`

So a row marked "Needs Retry" is never picked up by any later run. `Retry Count`
is incremented and then nothing reads it. The only way that post ever happens is
if a human flips the status back to Pending.

**Fix:** re-plan rows that are Failed with Issue = *Needs Retry*, under a retry
cap (say 3) and a cooldown (say 30 min), then mark them permanently failed. This
is a change to the fetch filter + planner, roughly 20 lines, and it is the single
highest-value item here — it converts most of today's manual work into a
self-healing loop.

**Decide explicitly:** a ban/verification/action-block failure must NOT be
retried (`apply_post_result` already routes those through `incidents` and does
not bump Retry Count). Only `ISSUE_NEEDS_RETRY` should be eligible.

---

## 2. The flow has no recovery for a dropped ADB tunnel

Verified: `instagram_reel.py` contains **zero** references to
`_adb_reconnect_device`, `_OFFLINE_MARKERS`, or any reconnect logic. The older
dump/OCR flow has that recovery; the u2 flow that replaced it does not.

The tunnel demonstrably drops on these MLX phones. From your own runs:

```
adb.exe: device offline
adb: error: failed to read copy response: EOF
```

Two consequences:

- **`u2.connect` failure is terminal.** [instagram_reel.py:816](adb_bot/automation/flows/instagram_reel.py:816) — one exception and the post is abandoned with `success: False`. No retry, no reconnect.
- **A mid-flow drop looks like a UI failure.** Every `u2` selector call starts
  failing, so the flow reports "composer not found" / "Share not detected" and
  gives up — blaming Instagram for what is a transport problem. The logs then
  send you looking in the wrong place.

**Fix:** an `_ensure_online` check (the probe flow already has a working one) at
the flow's entry, before `u2.connect`, and after any step that fails. On a
detected drop: reconnect via `_adb_reconnect_device`, re-establish `u2`, and
retry the step once rather than failing the post.

---

## 3. No step-level retry — one transient miss loses the post

Each major step is a single attempt with a hard exit:

| Step | Line | On failure |
|---|---|---|
| open reel composer | [868](adb_bot/automation/flows/instagram_reel.py:868) | return `success: False` |
| select REEL mode + media | [877](adb_bot/automation/flows/instagram_reel.py:877) | return `success: False` |
| tap Share | [921](adb_bot/automation/flows/instagram_reel.py:921) | return `success: False` |

There is no "back out to the feed and try again". A single mis-registered tap, a
pop-up that appeared a beat late, or a slow frame is enough to lose the post —
and because of finding #1 that post is then dead until someone touches it.

The individual tap helpers do have a fallback (`_u2_click` falls back to a ratio
tap when no selector matches), but there is no retry of the *step*.

**Fix:** wrap steps 1–3 in a retry of 2 attempts, where the recovery between
attempts is "press Back until the feed is visible, dismiss pop-ups, re-open".
Attempt 2 costs ~20s; a lost post costs a manual fix.

---

## 4. Verification silently drops to its weakest signals

`baseline_count` is best-effort ([instagram_reel.py:854](adb_bot/automation/flows/instagram_reel.py:854)) — if the profile
header can't be read, it stays `None`.

`verify_reel_posted` then **skips the post-count check entirely**
(`if callable(get_post_count) and baseline_count is not None`,
[reel_verify.py:239](adb_bot/automation/flows/reel_verify.py:239)) and falls back to banner text and notification
state, both of which the code itself documents as unreliable.

The failure mode is expensive: a post that **actually succeeded** gets reported
unconfirmed → `keep_media_for_retry` holds the clip → the queue row is marked
Failed. If you later retry that row manually, the same clip posts twice.

Nothing in the log distinguishes "confirmed by post count" from "confirmed by a
banner" or "gave up", other than `verdict.method` buried in the return value.

**Fix (two parts):**
- If the baseline can't be read, say so loudly and treat the run as
  *degraded* — that is a good reason to retry the whole post later rather than
  guessing from a banner.
- Surface `verify_method` into the Airtable Run Log note, so you can see at a
  glance which posts were strongly vs weakly confirmed.

---

## 5. A dead tunnel makes the push retry loop waste ~60s and still fail

`_adb_push_media_to_device` is retried up to 3 times ([instagram_reel.py:764](adb_bot/automation/flows/instagram_reel.py:764)),
with a content-hash verification after each. That verification is good — it
caught a push that reported `1 file pushed, 0 skipped` while the file was
actually absent.

But the retry is blind to *why* it failed. With a dropped tunnel it re-pushes a
12–23 MB file three times, each taking 10–20 s at the ~1 MB/s these phones run
at, and fails anyway. That is a minute of dead time per affected post.

Your new Drive variants are ~22 MB, which roughly doubles this.

**Fix:** check for an offline tunnel before each re-push; reconnect first. Same
helper as #2.

**Status (2026-08-02):** resolved by deletion — the verification and the 3x
re-push loop were removed from all three reel upload flows at the user's
request, since the on-device file matched the local one on every observed run.
The push and the media-scanner broadcast/index wait remain. A push that reports
success over a dead tunnel is no longer caught at push time; it now shows up as
the picker not finding the clip.

---

## 6. Minor

- `{"failed": True}` is set on one return path ([instagram_reel.py:870](adb_bot/automation/flows/instagram_reel.py:870)) and
  read by nothing. Either wire it up or drop it — as-is it implies a distinction
  the callers don't make.
- The media queue *is* thread-safe (`Lock` in `story_media.py`), so the new
  5-profile rolling window does not race on clip assignment. No action needed;
  noting it because it was worth checking.

---

## Suggested order

1. **Retry Failed→Pending rows** (#1) — removes most manual work on its own
2. **Tunnel-drop detection + reconnect** (#2, #5) — fixes the most common
   environmental failure and stops it being misdiagnosed as a UI problem
3. **Step-level retry** (#3) — catches the transient misses that remain
4. **Verification honesty** (#4) — stops good posts being recorded as failures

1 and 2 together should cover the large majority of what you are fixing by hand.
3 and 4 are what get it from "mostly unattended" to "actually unattended".

## What this audit could not check

Everything above is from reading the code and your run logs. None of it is
verified against a live device — in particular, how often the tunnel drops
mid-flow versus at connect time would change the priority of #2 vs #3. The
Run Log in Airtable already has the data to answer that, if the failure notes
are specific enough to tell them apart.
