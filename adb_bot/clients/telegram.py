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


class TelegramNotifier:
    """Posts to one chat. Never raises."""

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = (token if token is not None
                      else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id if chat_id is not None
                        else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, logger=None) -> bool:
        """True if Telegram accepted the message.

        False covers both "not configured" and "it did not work"; the caller
        treats them the same, because neither is worth failing a loop over.
        """
        if not self.configured:
            return False
        body = text if len(text) <= MAX_MESSAGE_CHARS else (
            text[:MAX_MESSAGE_CHARS - 20].rsplit("\n", 1)[0] + "\n… (truncated)")
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": body,
            "parse_mode": "HTML",
            # The alert is the message; a link preview would only add noise.
            "disable_web_page_preview": True,
        }).encode()
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
        return f"configured for chat {self.chat_id}"


def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    getattr(logger, level, None) and getattr(logger, level)(message, *args)
