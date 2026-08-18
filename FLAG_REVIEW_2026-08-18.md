# Flag review, 2026-08-18

Worked the `No Recent Success`, `Retries Exhausted` and `Human Verification
Required` lists. 24 profiles were in scope at the start; 68 profiles carried
`Needs Human Check` overall.

**The headline is not in the flag list.** The whole fleet had been unable to
post for seven hours before this review started, and that outage is what
produced most of the recent flags. Fixing it is the critical path; the flags
are downstream.

---

## 1. The fleet was down, and the flags are a symptom

At **06:07:55 UTC** every posting tick began failing:

```
Failed to build the posting-queue plan: 403 Client Error: Forbidden
  for .../appL9q0XcNFtP4fYV/Accounts?...
*** POSTING STILL STALLED: nothing produced in 404 min while 711 item(s) are due
```

**The `Accounts` table no longer exists in the base.** The token is fine — it
reads base metadata with a 200 and lists eleven tables, and `Accounts` is not
among them. Airtable answers an unknown *table name* with **403, not 404**, so a
schema change arrived wearing the label of an auth failure. `doctor` reported
`Airtable: auth rejected` for seven hours, which points every reader at the PAT
instead of the base.

Blast radius: `posting`, `queue`, `retry` and `doctor` all dead. `recheck`,
`pipeline` and `second-accounts` unaffected.

Whether that table *should* have been deleted is a question for whoever changed
the base this morning — **that is the one open question in this report.** But
posting must not depend on it either way, because the fleet has been
profile-driven since the cut-over:

| Posting Queue rows | count |
|---|---|
| link a **Target Profile** | 2825 |
| link an **Account** | 1 |
| due + Pending (all profile-driven) | 656 |

### The fix

Branch `worktree-accounts-table-optional`, commit `12a2d88`, pushed to origin.
**Not deployed** — deploying needs the shared checkout, which this session was
isolated from. Commands are in §5.

* `_list_accounts_or_empty()` is the single place that decides the table may be
  gone: 403/404 reads as absent and returns `[]`, explained once per process.
  **401 still raises**, so a dead token is never mistaken for a schema change.
* Both reads of the table go through it. `list_accounts()` covers posting,
  retry, recovery and the report. `active_accounts_by_model()` is a *separate*
  read and is what the **queue** loop uses to create rows — patching only the
  first left the queue loop still dying, which would have drained the backlog
  and stopped the fleet again a day later for a different-looking reason.
* `doctor` no longer requires `Accounts`, and no longer says "auth rejected"
  when a *subset* of tables fails — only when every one does, which is what a
  dead token actually looks like. A subset now names the tables.

Verified against the live base:

| loop | before | after |
|---|---|---|
| `posting` | 403, no plan | `posting plan: due=52 skipped=605` |
| `queue` | 403, `errors=1` | `targets=197 slots_due=2 flexible=180 rows=38 errors=0` |

2814 tests pass, plus four new regression tests covering both reads, the 401
case, and the once-per-process explanation.

---

## 2. Flags cleared (7, and they hold)

Rather than guess, each candidate was tested by re-running
`stale_profiles.find_stale_profiles` against the **live ledger and queue** with
that profile treated as unflagged — if it came back stale, clearing it would be
undone within the tick. Seven passed, and all seven are still clear:

| Profile | was | evidence it was a stale flag |
|---|---|---|
| Emely 4 | No Recent Success | recheck confirmed **3 → 5** at 08-17 23:25, *after* the 19:52 flag |
| Luisa 5 | No Recent Success | confirmed **109 → 110** at 22:44, after the 21:22 flag |
| Luisa 6 | No Recent Success | confirmed **72 → 73** at 23:08, after the 20:52 flag |
| Jasmin 1 | Retries Exhausted | confirmed **79 → 84** at 23:31, after the 22:21 flag |
| Nikki 12 | Retries Exhausted | confirmed **61 → 69** at 18:18, after the 17:21 flag |
| Jil 14 | Retries Exhausted | no stale condition remains on live data |
| nikki 7 | No Recent Success | 08-16 supervised run proved it healthy; not stale on live data |

All cleared with `clear_human_flag` and **`Flagged At` left stamped**, so
`recovery_runner` picks them up. Each carries a note recording the evidence.

### Two of them would have dumped their whole backlog

`Nikki 12` had **40 due rows** and `nikki 7` **23** — and the timer loop passes
no per-profile cap, so `run_profile` would have worked through every one of them
back-to-back on a single launch. Nikki 12's 40 included **31 all scheduled on
08-17 alone**, which is fixed-slot back-fill, not real demand. Posting 40 reels
in a row is also an account-safety problem, not just a grind.

So before un-flagging, **59 rows were spread to ~5/day** (Nikki 12: 40 due → 2,
nikki 7: 23 due → 2). Every row's original scheduled time is recorded in its
Notes, and a full backup is in the job's tmp dir as `schedule_backup.json`.

### These clears expire tonight unless posting is restored

`stale_profiles` counts only a **confirmed** post in the last 24h. The posts
that justify these clears are ageing:

| Profile | flag returns at |
|---|---|
| Nikki 12 | **2026-08-18 18:18 UTC** |
| Luisa 5 | 22:44 UTC |
| Luisa 6 | 23:08 UTC |
| Emely 4 | 23:25 UTC |
| Jasmin 1 | 23:31 UTC |
| Jil 14 | 2026-08-19 03:56 UTC |

None of them can earn a fresh confirmation while posting is down. **Deploy
before this evening or this work undoes itself.**

---

## 3. Human Verification Required — what the ten actually are

The label remains wrong on most of the list, as in previous passes.

**Nikki 24 — a real image captcha, and solvable.** Probed read-only: *"Confirm
you're human … enter the code from the image"*, one input field, on three
consecutive looks. This is the chain proven end to end on 2026-08-12 (2captcha
read six distorted digits for $0.001; the captcha gates a phone step behind it,
~$0.60 a number). It is the single most worthwhile thing left on this list and
it needs a person to run it — see §5.

**Katja 6 — healthy, and it demonstrates the deadlock.** Three consecutive
read-only looks showed its own story tray (`coyemoxxcute`) and the full bottom
nav, with zero input fields: the `screen_is_healthy` whitelist. Its HVR label
never came from a screen — it came from a queue row's Issue Type via
`retry_runner`, the known raw-XML false-positive path (`account_flag_u2` still
feeds the whole `dump_hierarchy()` XML to the classifier).

It was cleared at 13:19 and **`stale_profiles` re-flagged it at 13:26** as
`No Recent Success`. That is correct behaviour, not a failure: the phone is
fine, but it has no confirmed post and cannot get one while the fleet is down.
It is now the clearest single illustration of why the deploy comes first.

**Still signed out — a credentials ask, not a verification problem.** Re-read
today, unchanged since 08-14: **Luisa 2, Luisa 3, Luisa 8, Luisa 9** all sit on
Instagram's logged-out landing page (`BloksSignedOutFragmentActivity`,
*"Join Instagram / Get started / I already have a profile"*). Nobody can fix
these without passwords. Expect some logins to fail into a ban notice, as
`Laila 9` did on 08-14.

**Jil 2 and Jil 10 do not exist any more, and that is a new finding.** Both
failed to probe, and the reason is not a flaky launch: their MLX ids
(`624743063687856180`, `626422241281769608`) are **absent from the MultiLogin
workspace**. 205 mobile profiles were read; 16 are named `Jil*` and neither of
these is among them, while `Luisa 2` and `Luisa 8` were used as positive
controls and both matched. `verification_probe` refuses them outright with
*"no MultiLogin profile named or id'd"*.

So their standing "signed out / needs credentials" note was obsolete: **there is
no phone to sign in to.** No credential and no amount of verification spend can
recover them. Their Issue Notes have been corrected in Airtable and both were
left flagged so the diagnosis is not overwritten.

`Jil 10` is still `Active` and carries **44 Posting Queue rows aimed at a device
that does not exist**. Someone needs to decide whether to re-create the phone or
retire the profile — that is a client call, so nothing was deleted here.

**Not solvable at any price** — leave them or retire them, but do not spend on
them: `Katja 2` wants a **video selfie**, `Luisa 7` wants a **photo of an
official ID**.

**Kathi 7** is on an SMS checkpoint asking for a code sent to `+31613813164` —
a number the fleet does not control. Unchanged from 08-14.

---

## 4. The six that will not clear yet

`Emely 1`, `Jil 6`, `Jil 7`, `Kathi 9`, `Nikki 28`, `nikki 5` were all tested
and all come back stale on live data, so clearing them now would be undone
within the tick. They are genuinely without a confirmed post — the ledger shows
each was confirming normally and then stopped:

| Profile | last confirmed | signal |
|---|---|---|
| nikki 5 | 08-17 01:51 | 1 failed since |
| Kathi 9 | 08-16 18:38 | 2 failed, then `heartbeat_lost` |
| Jil 6 / Jil 7 | 08-15 ~18:00 | 1 unconfirmed each |
| Nikki 28 | — | recheck **disproved** it: count still 2 after 258 min |
| Emely 1 | never | recheck abandoned after 24.2h |

None of these can be resolved by clearing a checkbox. They need the fleet
posting again, then one confirmed post each. Re-run this review after the
deploy: on past form a good share of them will resolve themselves, and whatever
is left will be a real fault worth a supervised run.

---

## 5. What needs a person

**1. Deploy the fix — this is the one that matters, and it is time-boxed.**

```bash
git -C /root/adb_bot merge --ff-only 12a2d88
cd /root/adb_bot && python -u deploy/adbbot_deploy.py apply --apply --drain-after 120
```

`adbbot_deploy.py` hardcodes `BRANCH = "integration"`, so the branch pointer
moves first. Use `python -u` (it buffers stdout) and `--drain-after 120` (it
waits 900s for a running loop and then aborts changing nothing).

**2. Confirm the `Accounts` table deletion was intentional.** If it was not, it
needs restoring — the fix makes the fleet survive its absence, it does not put
the data back. If it was, nothing further is needed.

**3. Solve Nikki 24's captcha.** Money-spending commands are blocked by the
permission classifier, so run it yourself with a `!` prefix:

```
!cd /root/adb_bot && .venv/bin/python -m adb_bot.automation.verification_probe --profile "Nikki 24" --apply
```

Check the wallet first (`.venv/bin/python -m adb_bot.clients.sms.cli balance`).
Budget ~$0.60 if it goes to a phone step behind the captcha.

**4. Credentials for the signed-out accounts** — `Luisa 2`, `Luisa 3`,
`Luisa 8`, `Luisa 9`, all confirmed on screen today. This is the largest single
blocker on the flag list and no amount of automation touches it.

**5. Decide what happens to `Jil 2` and `Jil 10`** — their MultiLogin phones are
gone. Re-create them or retire the Airtable profiles. `Jil 10` is still `Active`
with 44 queue rows pointed at nothing, so it also quietly consumes content
planning until someone decides.

**6. Decide on `Katja 2` and `Luisa 7`** — video selfie and ID document. Supply
them or retire the accounts.

---

## Footnote: the verification timer is installed but disabled

`adbbot-verification.timer` exists and is `disabled`, with a real `--apply`
ExecStart bounded at 2 profiles/tick, 4 numbers/tick, 12 numbers/day (~$2.40)
and a $1.00 minimum balance. It has never run on a timer; its log holds one
dry run from 08-17. That is the loop that would work `Nikki 24` and its
successors unattended. Arming it is a spend decision and was deliberately left
to a person, so it was left alone — noted here only because the HVR list is
exactly what it exists to drain.
