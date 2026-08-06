# Patches for the deployed checkout

`/root/adb_bot` is the checkout the systemd loops import from. It is **not** on
`main` — as of 2026-08-06 it sits on `server-heartbeat-spoof-layout` with
uncommitted work — so a fix that lands on a feature branch does not reach the
running loops. These patches are the minimal form of a fix, kept applyable
against that checkout until it is brought back onto a shared branch.

## 0001-scope-slot-label-guard-to-the-day.patch

**Fixes:** the queue loop permanently retiring a slot after one use.

`plan_slot_rows` guards against filling a slot twice. One guard keys on
`Scheduled DateTime` (`_slot_key`, date-scoped, correct); the other keys on the
slot label read back off the row Name (`_slot_label_from_name`), which returns a
bare `HH:MM`. That second guard was added 2026-08-04 so the retry pass — which
moves `Scheduled DateTime` to `now + backoff` — could not make a served slot
look unserved. Correct intent, but it was never scoped to a day, and the loop
reads the Posting Queue in full while nothing ever deletes old rows. So one
`"Jil 1 / 18:00"` row blocked Jil 1's 18:00 slot on **every future day**.

Each (target, slot) pair became single-use for all time. Measured on the live
base on 2026-08-06: of 52 real accounts, **0** could still be given an 18:00 row;
the whole grid had 20 postable cells left before it produced nothing at all.

The patch adds `_row_day()` and keys the label guard on `(target, day, label)`.
The day comes from Airtable's `createdTime` because the retry pass moves the
schedule and nothing moves the creation stamp; rows predating this (and rows in
tests) have no `createdTime`, so the scheduled day stands in.

Verified against the live base, same arguments as `adbbot-queue.service`
(`--targets profiles --slots 18:00,20:00,22:00`), dry-run:

    unpatched   queue result: [DRY-RUN] targets=104 slots_due=1 rows=0  skipped=52
    patched     queue result: [DRY-RUN] targets=104 slots_due=1 rows=46 skipped=52

Apply:

    cd /root/adb_bot
    git apply -p1 .claude/worktrees/daily-run-report/deploy/patches/0001-scope-slot-label-guard-to-the-day.patch

**Before applying, read this:** the first queue tick afterwards writes rows for
every slot already due today that the target has not been served, so it clears
the backlog in one go — 46 rows for the 18:00 slot at the time of measuring.
Those rows are legitimately owed, but they become real Instagram posts within a
few posting ticks, and `--targets profiles` carries no account health guards, so
profiles flagged Needs Human Check are included. Applying just after the last
slot of the day (23:00 Berlin) starts the next day clean with no catch-up burst.

The same fix already exists, with its regression test
(`test_yesterdays_row_does_not_block_todays_slot`), on branch
`worktree-daily-run-report` in commit 9503503. This patch is only for the
deployed checkout; it can be dropped once that checkout tracks a branch with the
fix in it.
