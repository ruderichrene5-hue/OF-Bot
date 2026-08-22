# Migrating the fleet onto Geelark — where it is stuck and what to do

Written 2026-08-22, after a day of testing nine accounts end to end.

The short version: **the migration is not blocked by Instagram, by the
passwords, or by Geelark.** All three work. It is blocked by one thing — we
cannot read the mailbox that Instagram sends its confirmation code to.

---

## 1. What actually happens when we try

Logging an existing account onto a Geelark phone gets this far, every time:

1. Geelark phone launches, Instagram opens — **works**
2. Handle and stored password accepted — **works**
3. Instagram asks to confirm it is really us — expected
4. It offers exactly two ways to confirm — **and this is the whole problem**

```
Email                          — send a code to <the account's address>
Notification on another device — approve from a device already logged in
```

**There is no SMS option.** That matters because SMS is the one channel we have
working: rented US numbers deliver about 73% of the time, and they are what
made 13 brand-new accounts today. None of that helps here, because Instagram
will not offer it for an existing account on a new device.

Nine accounts were driven to that screen. All nine reached it with the correct
address showing. Zero codes arrived.

### Why neither option works today

**The emailed code** goes to a Gmail account that lives on the account's old
MultiLogin phone (its "twin"). We cannot sign in to those mailboxes from
anywhere else — ten were tried through Google's own login, none opened, seven
hit a captcha on the address itself before a password was even asked for.

That left reading the mail *on the twin*, where it is already signed in. It
does not work either, and the reason is now measured rather than guessed:

```
dumpsys content   →   gmail-ls   Total 0 syncs ... [initialize=true]
                      gmail-ls   PERIODIC period=1d00h00m00s
```

**Gmail has never completed a single sync on these phones.** Not stalled —
never run, not once. Its content provider returns empty for every query, and
its UI never renders a readable screen.

> A trap worth recording: the twins show an Instagram notification with a
> six-digit code in it. It is **not** mail. It never changes, it predates any
> request, and Gmail has never synced — these cloud phones are cloned from an
> image that already contained it. It cost most of an afternoon before that
> was understood.

**The approval push** fails for the same underlying reason. Push and sync both
run on Google Play Services. Instagram names the right handset — it said
"OnePlus Ace 2V", which is exactly the twin — and the push still never lands,
even with notification permission granted directly over ADB.

**One broken component, both routes.** That is why every workaround failed.

### What is *not* the problem — all checked, all ruled out

| Suspected | Measured | Verdict |
|---|---|---|
| Geelark exits from the wrong country | phone's own IP `109.41.112.77`, German, Vodafone D2, Europe/Berlin, DE SIM | clean |
| Stored passwords are stale | Instagram accepts them and moves to confirmation | fine |
| Stored addresses are wrong | Instagram's masked address matches ours on 8 of 9 | fine |
| The login automation is broken | reaches the confirmation screen every time | fine |

The one exception: `islafaee`'s code goes to `d*******o@gmail.com`, which is
not the address we hold for it. That record is wrong and needs correcting.

---

## 2. What IMAP is, and why it keeps coming up

IMAP is the standard protocol an email *program* uses to read a mailbox from a
server — the thing Outlook, Thunderbird or a script uses to list messages and
pull them down. It needs only a hostname, an address and a password. No phone,
no app, no screen to read.

**If IMAP worked on these mailboxes, this whole problem would be a few lines of
code.** Instagram sends the code, a script connects to `imap.gmail.com`, reads
the newest message, extracts six digits, types them in. No twin, no Gmail app,
no sync adapter, nothing to break.

**Why it does not work here.** Google switched off plain password access to
IMAP. A normal account password is refused outright — tested on five of these
mailboxes, both kinds, all rejected:

```
2FA off  →  [AUTHENTICATIONFAILED] Invalid credentials
2FA on   →  [ALERT] Invalid credentials
```

To use IMAP on a Google account now you need an **app-specific password** — a
16-character password minted from inside Google account settings, for one
application. Which means signing in to Google. Which is the thing we cannot do
on this pool.

So IMAP is not a dead end because of IMAP. It is a dead end because it needs
one working Google login, and we do not have one.

**The important consequence:** IMAP works fine on mailboxes we control. Any
address on our own domain, or any provider that allows app passwords, is
readable by a script immediately. That is what makes Option A below the good
one — it swaps an unreadable mailbox for a readable one, and everything
downstream becomes easy.

---

## 3. The options

### Option A — point the accounts at a mailbox we control ★ recommended

On a twin that is still logged in, Instagram lets you change the account's
email address from inside the app. Point it at an address on **our own domain**,
then log in on Geelark and read the code over IMAP.

* Needs no Google fix at all — that is the point.
* We have already driven a twin's settings this far under automation
  (Profile → Options → Accounts Centre → Password and security), so the
  navigation is known to work.
* One domain with catch-all routing gives unlimited addresses, permanently.
  Roughly £10/year. This single purchase removes the dependency on a Gmail
  pool that has failed every test put to it.

**Risk, and it is real:** changing an account's email is a security-sensitive
action, so Instagram may challenge *that* on some accounts — putting us back at
the same wall for those. **Test on one account before committing to it.**

**Limit:** it only works on twins that are still logged in. See Option C.

### Option B — attach a phone number to the account, from the twin

Same access, different lever. With a number on the account, future
confirmations may offer SMS — the channel we already have working.

* Less certain than A: Instagram did not offer SMS in the login chooser for
  these accounts, and it may not start doing so just because a number exists.
* Costs a rented number per account, and the number is released afterwards.
* Worth trying on one account *while* testing A, since the access is identical.

### Option C — clear the twins' checkpoints first ★ prerequisite for A and B

This is the binding constraint on both routes above. Of 14 twins sampled:

```
1   on the feed          ← usable today
7   "confirm you're human"
2   logged out
3   unknown (captcha or phone-number prompt)
1   Instagram never opened
```

**Only one twin in fourteen is currently usable.** The seven behind "confirm
you're human" are the prize: 2captcha credit is untouched ($9.98), and each
twin cleared becomes an account that A or B can then reach.

Note this is *not* blocked by the Gmail problem — a captcha checkpoint is
solved on the screen, not by mail.

### Option D — rebuild instead of migrating

Proven today: **13 accounts created**, ~$0.53 each in SMS, about 7 minutes of
phone time apiece.

The cost is not money. It is the followers, history and age of the existing
accounts. Rebuilding is the right answer for accounts whose twin is logged out
or dead, and the wrong answer for anything with real reach.

### Option E — fix the Gmail pool at source

Get working Google passwords, sign in, mint app passwords, use IMAP.

Recorded for completeness, but the evidence is against it: ten mailboxes tried,
none opened, seven captchaed on the address itself. Google appears to have
flagged the pool. Not worth more effort unless someone holds newer passwords
outside Airtable.

---

## 4. Roadmap

Ordered so that each step either unblocks the next or kills the plan cheaply.

### Phase 0 — decisions needed before any work (owner: Caio/Rene)

- [ ] **Buy a domain with catch-all email.** Anything with IMAP or an API.
      This is the single highest-leverage purchase available. ~£10/year.
- [ ] **Top up SMSPool** (~$20 ≈ 35 more new accounts) if the rebuild path is
      to continue in parallel. Both wallets are now empty.
- [ ] **Decide about the non-German profiles.** 26 MultiLogin profiles are not
      pinned to Germany — 21 bulk `Default profile name` ones, plus
      `Blank (3)`, `Katja 9`, `Laila 1`, `Nikki 27` on `country=any`, and
      `Laila 6` on **Paraguay**. `any` means MultiLogin picks, and this is the
      likely source of the "login blocked from Indonesia" notice. Re-pinning
      changes their exit IP, which can itself trigger a challenge — so it needs
      a quiet window, not a live posting hour.

### Phase 1 — prove the route on one account (half a day)

- [ ] Pick the one twin known to be on its feed (`islafaee` / MLX `Jasmin 11`,
      id `632162578940952646`).
- [ ] Change its Instagram email to `<handle>@ourdomain`.
- [ ] Confirm the change lands — **watch for Instagram challenging the change
      itself**; that is the failure mode that would kill Option A.
- [ ] Log in on the Geelark phone, read the code over IMAP, complete it.
- [ ] **Gate:** if this works, the migration is a throughput problem. If
      Instagram blocks the email change, Option A is dead and Phase 2 becomes
      Option B or D.

### Phase 2 — widen the pool (one to two days)

- [ ] Run the twin checkpoint clear across all `human_check` twins using
      2captcha. Measure how many actually clear.
- [ ] Re-survey every twin's state — the 14 sampled are not the whole fleet.
- [ ] For each twin now reachable, repeat Phase 1's email change.

### Phase 3 — migrate in bulk

- [ ] Batch the login-and-confirm across all re-pointed accounts, 2–4 at a
      time (Geelark sells 4 parallel slots).
- [ ] Write each result back to Airtable and to the Geelark remark.
- [ ] Tag `IG connected` **only** on accounts that actually reach the feed.

### Phase 4 — deal with what is left

- [ ] Accounts whose twin is logged out or dead: no route exists. Put them on
      the rebuild list rather than spending more on them.
- [ ] Correct the records that are wrong regardless of migration:
      `islafaee`'s stored address, and the duplicate Geelark→MLX pointers.

---

## 5. Other things found that need fixing anyway

These are independent of the migration and will bite something else if left.

**33 duplicate MultiLogin profile names.** 216 profiles, 177 distinct names.
Two *different handsets* are both called `Blank (1)` (a OnePlus Ace 2V and a
Redmi Turbo 5); `Jasmin 11` names three. Every lookup by name — including the
repo's own `_find_profile` — takes whichever the API listed first, so code can
drive the wrong phone and report a confident wrong answer. **Address profiles
by id.** It fails roughly one time in six, which is exactly often enough to
look like a flaky phone rather than a wrong one.

**26 profiles not pinned to Germany.** Listed in Phase 0 above.

**Two Geelark phones point at the same MLX twin.** `timothycastrowrv196` and
`jasonjankem864` both claim `MLX:Blank (1)`. One of them is wrong.

**One Geelark phone is on MultiLogin's gateway.** `serialNo=9`, carrying sid
`uxvZIvuv` — left over from a shared-IP test today. Harmless, but it is a
stray in the proxy list and should be removed or used deliberately.

**SMS delivery is bimodal.** Codes arrive in 0–3 seconds, or at 68–77 seconds,
and never in between — the slow ones are a provider backlog flushing. A wait
shorter than ~90s silently discards a whole mode. Both timeouts are now 90s;
do not lower them without new evidence.

---

## 6. Costs, for planning

| Item | Cost | Notes |
|---|---|---|
| Domain + catch-all email | ~£10/year | unlocks Option A entirely |
| SMSPool top-up | $20 ≈ 35 accounts | ~$0.53 per new account |
| 2captcha | $9.98 already held | untouched; funds Phase 2 |
| Geelark minutes | $29.36 credit held | ~7 min per account |
| Geelark profile slots | 203 free of 400 | no purchase needed |

---

## 7. The honest summary

**Migrating an existing account needs a readable mailbox. Everything else
already works.** Buy the domain, prove the email change on one account, and the
migration turns from blocked into routine. If Instagram refuses the email
change, then the old accounts cannot be moved and the answer is to rebuild —
which is proven, cheap, and already produced 13 accounts.
