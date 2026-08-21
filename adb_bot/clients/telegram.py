"""Send a message to a Telegram group, or do nothing at all.

The first outbound notification channel this deployment has had. `loop_watchdog`
records why there was none: alerts went to a log file, a state file and a Run Log
row, all of which require somebody to go and look. A VA clearing Instagram
checkpoints does not watch `logs/alerts.log`.

Two rules shape everything here:

* **Not configured is a normal state, not an error.** Without a token and a chat
  id this is a no-op that says so once. The loops it hooks into must run exactly
  as before on a box that has never heard of Telegram -- which is every box
  until somebody pastes a token into `/etc/adbbot/env`.
* **A notification must never cost a post.** Telegram being down, slow, or
  rate-limiting is not a reason for a posting or tagging loop to fail, so every
  call is wrapped and returns False rather than raising.

Config, both from the environment (normally `/etc/adbbot/env`):

    TELEGRAM_BOT_TOKEN   the token @BotFather gives you, "123456:ABC-DEF..."
    TELEGRAM_CHAT_ID     the group's id. Negative for groups, e.g. -1001234567890
    TELEGRAM_TOPIC_ID    optional. In a forum group (one with topics), the
                         thread to post in; without it every message lands in
                         "General", which is not where anybody is looking.
    ADBBOT_TELEGRAM_ALERTS
                         optional comma-separated allowlist of alert categories
                         (see CATEGORIES). Unset means every category is sent,
                         which is what a box that has never heard of this does.

**Why an allowlist and not four separate switches.** One token feeds every
alerting loop, so switching the token on switches all of them on together --
which is how re-enabling it on 2026-08-21, after ten days off, silently re-armed
the daily digest and the profile-flag alerts as well as the thing somebody
actually wanted. Narrowing the channel has to be one decision in one place, or
it is not a decision anybody can check.

Finding the topic id: open the topic in Telegram and copy its link. For a
private supergroup that is `t.me/c/<chat>/<topic>` -- the last number is the
thread id. Note the chat id for the API is `-100` prefixed to the middle
number: `t.me/c/4448753764/43` means chat `-1004448753764`, topic `43`.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.telegram.org"

# Telegram rejects anything longer. Messages are truncated with a marker rather
# than split: a half-sent alert read out of order is worse than a short one.
MAX_MESSAGE_CHARS = 4096

# Short on purpose. This runs inside a loop that has phones open and locks held;
# waiting on a chat server is not worth a second of that.
TIMEOUT_SECONDS = 10

# Every kind of message this deployment can send, and which loop sends it.
# Named here rather than at the call sites so the set can be read off in one
# place -- an allowlist you have to grep four modules to understand is one
# nobody will maintain.
CATEGORIES = {
    "proxy": "MultiLogin proxy traffic: out, running low, and back (mlx-guard)",
    "minutes": "MultiLogin minutes: out (mlx-guard) and running low (mlx-minutes)",
    "fleet": "nothing is launching, cause unattributed (mlx-minutes)",
    "flags": "profiles tagged as needing a person (issue-tags)",
    "digest": "the daily summary (digest)",
}


class TelegramNotifier:
    """Posts to one chat. Never raises."""

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 topic_id: str | None = None) -> None:
        self.token = (token if token is not None
                      else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None
                        else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        # Optional: only forum groups have topics, and a chat that has them
        # rejects nothing without it -- the message just lands in "General".
        self.topic_id = (topic_id if topic_id is not None
                         else os.environ.get("TELEGRAM_TOPIC_ID", "")).strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def allowed(self) -> set:
        """The categories this box is willing to send, or an empty set for all.

        Read per call rather than cached at construction: these objects are
        built once per loop run, and a change to `/etc/adbbot/env` should take
        effect on the next tick rather than the next deploy.
        """
        raw = (os.environ.get("ADBBOT_TELEGRAM_ALERTS") or "").strip()
        return {part.strip() for part in raw.replace(",", " ").split() if part.strip()}

    def allows(self, category: str = "") -> bool:
        """Whether `category` may be sent.

        An unset allowlist allows everything, and an uncategorised message is
        always allowed -- narrowing the channel is opt-in, so a caller that has
        not been taught about categories keeps working rather than going quiet
        without anyone deciding it should.
        """
        allowed = self.allowed
        return not allowed or not category or category in allowed

    def send(self, text: str, logger=None, category: str = "") -> bool:
        """True if Telegram accepted the message.

        False covers "not configured", "suppressed by the allowlist" and "it did
        not work"; the caller treats them the same, because none is worth
        failing a loop over. Callers that distinguish -- to keep a suppressed
        alert out of a "could not send" warning -- should ask `allows()` first.
        """
        if not self.configured:
            return False
        if not self.allows(category):
            _emit(logger, "info",
                  "telegram: %s alerts are not in ADBBOT_TELEGRAM_ALERTS; "
                  "not sending", category)
            return False
        body = text if len(text) <= MAX_MESSAGE_CHARS else (
            text[:MAX_MESSAGE_CHARS - 20].rsplit("\n", 1)[0] + "\n… (truncated)")
        fields = {
            "chat_id": self.chat_id,
            "text": body,
            "parse_mode": "HTML",
            # The alert is the message; a link preview would only add noise.
            "disable_web_page_preview": True,
        }
        if self.topic_id:
            fields["message_thread_id"] = int(self.topic_id)
        payload = json.dumps(fields).encode()
        req = urllib.request.Request(
            f"{API}/bot{self.token}/sendMessage", data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
                ok = bool(json.loads(response.read() or b"{}").get("ok"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read() or b"{}").get("description", "")
            except Exception:
                pass
            # 400 "chat not found" and 403 "bot was blocked" are the two a human
            # can actually fix, so say which it was rather than just "failed".
            _emit(logger, "warning",
                  "telegram: %s rejected the message: %s", exc.code, detail or exc)
            return False
        except Exception as exc:
            _emit(logger, "warning", "telegram: could not send (%s: %s)",
                  type(exc).__name__, exc)
            return False
        if not ok:
            _emit(logger, "warning", "telegram: the API returned ok=false")
        return ok

    def describe(self) -> str:
        """One line for a doctor check or a startup log."""
        if not self.configured:
            missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", self.token),
                                      ("TELEGRAM_CHAT_ID", self.chat_id)) if not v]
            return f"not configured (missing {', '.join(missing)})"
        where = f"chat {self.chat_id}"
        line = f"configured for {where}" + (
            f", topic {self.topic_id}" if self.topic_id else " (no topic -- General)")
        allowed = self.allowed
        if allowed:
            unknown = allowed - set(CATEGORIES)
            line += f"; sending only {', '.join(sorted(allowed))}"
            # A typo in the allowlist silences a category rather than erroring,
            # so it has to be visible somewhere a person looks -- this string is
            # what `doctor` prints.
            if unknown:
                line += f" (unrecognised: {', '.join(sorted(unknown))})"
        return line


def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    getattr(logger, level, None) and getattr(logger, level)(message, *args)
