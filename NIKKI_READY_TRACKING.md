# Nikki — Account-Ready Tracking

Started 2026-09-01. Tracks per-profile status while going through the "get every
account ready" pass (Setup tag = profile picture done, funnel ready to post).
Post status pulled from `/root/.adb_bot/posted_reels.jsonl` (live, not a guess).

| Profile | Setup tag | Ever posted? | Detail |
|---|---|---|---|
| Nikki 2 | yes | **never** | 0 ledger entries at all |
| Nikki 3 | yes | uncertain x2 | both still "shared" (unresolved), recheck pending |
| Nikki 4 | yes | uncertain x1 | still "shared" (unresolved), recheck pending |
| Nikki 5 | no | confirmed | 1 shared + 1 confirmed |
| Nikki 6 | no | confirmed | 1 shared + 1 confirmed |
| Nikki 7 | no | confirmed | 2x shared + 2x confirmed |
| Nikki 8 | no | confirmed | 1 shared + 1 confirmed |
| Nikki 9 | no | **never** | 0 ledger entries at all |
| Nikki 10 | no | **never** | 0 ledger entries; tagged **"in review"**, not Active_Posting -- that's why |
| Nikki 11 | no | confirmed | 2x shared + 2x confirmed |
| Nikki 15 | no | confirmed | 2x shared + 2x confirmed |
| Nikki 29 | no | confirmed | 1 shared + 1 confirmed |
| Nikki 24 | no | mixed | 2x shared, **2x disproved** (proven not posted, clip re-opened) |
| Nikki 26 | no | uncertain x1 | still "shared" (unresolved), recheck pending |

## Notes

- Nikki 2 and Nikki 9 have never posted anything at all — worth checking why
  (content availability, or something else) before assuming they're just "not
  reached yet."
- Nikki 3 and Nikki 4 have real Share attempts sitting unresolved -- the
  geelark_recheck pass should clear these on its own; check back after it runs.
- Manual ADB access to Nikki 6-9 failed today (2026-09-01) via the fast-path
  connect (skips proxy rotation) -- GeeLark's own status flickered
  running -> starting -> stopped when `/phone/start` was called on an
  already-running phone. The slower `_launch()` path (full proxy rotation) was
  not confirmed broken -- untested to completion.
