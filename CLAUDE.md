# adb_bot repo: Northfeed portal + manager + chatter backend, and the posting-bot fleet

Gesamtbild Northfeed (gilt immer):
@/root/northfeed/NORTHFEED.md

Spezialwissen für diesen Bereich:

The **Northfeed portal chat** is opened here (portal.northfeed-management.com): model portal, manager UI, chatter portal, **courses incl. producing
the course videos** (source in /root/chatterkurs – read /root/chatterkurs/CLAUDE.md when working on lessons), test, mass messages, contracts,
billing, mails. The bot fleet (GeeLark/MLX phones, posting) lives in the same repo but is not an active topic right now.
Talk to Rene in German.

## Portal / Manager
- Service `adbbot-platform` (`adb_bot/platform_server.py`), env `/etc/adbbot/env`; after code changes `systemctl restart adbbot-platform`.
- Code in `adb_bot/automation/`: manager.html/.py (manager UI + API `manager.api`), onboarding*.py, contract_data.py, docuseal.py (model template 10,
  chatter 13), billing.py, course.py + course.html, chatter_portal.py, chatter_admin.py (`manager_api`), chatter_test.py, mass_messages.py
  (Google Sheet MASS_SHEET_ID), shifts.py (Discord webhooks), portal_auth.py (mails, sessions).
- Data in Airtable (base via portal.T_* constants). **Change, don't delete** – never delete records/fields/views unasked.
- Mails: team voice ("Dein Northfeed-Team"), no time estimates, no "schreib mir auf WhatsApp" everywhere; WhatsApp follow-ups are one-click in the manager.
- Test once on real accounts; test rows get renamed "TEST …" and set to Ended, not deleted.

## Repo / deploy
- Live branch is `integration` in this checkout; most systemd loops execute this checkout – **a merge/commit here is a deploy**.
- GitHub remote OF-Bot (token currently embedded in the remote URL – to be rotated by Rene).
- History/decisions: /root/.claude/projects/-root/memory/ (northfeed-portal-*.md, northfeed-manager-ui-rules.md, adbbot-*.md).
