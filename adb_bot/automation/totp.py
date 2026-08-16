"""Six-digit TOTP codes from a base32 secret, with no dependency.

Needed because the mailboxes in the `VA - ML I IG` base carry 2-Step
Verification, and signing one into a phone (through the **Play Store**, not
Gmail -- see `SIGNUP_RUN_2026-08-13.md` §2.1c) asks for an authenticator code.

The one operational lesson from the run that got this working: **a code is only
valid inside its own thirty-second window, and every CLI round trip to one of
these phones costs about twenty seconds.** Codes generated in one process and
typed immediately were accepted every time; the same codes generated a command
earlier were rejected, and Google's error says nothing about which step failed
-- it reads like a wrong password. So `seconds_left` is part of the API: never
start typing a code with only a few seconds on it.
"""

from __future__ import annotations

import base64
import hmac
import struct
import time
from hashlib import sha1

PERIOD = 30
DIGITS = 6

# Below this, the code will very likely expire between typing and submitting.
SAFE_SECONDS = 8


def normalise_secret(secret: str) -> str:
    """Accept a secret the way a person pasted it.

    The base's `2FA Secret Key` column holds them in Google's display form --
    lower case, in groups of four, e.g. `n4bc wdf5 tr4g sisu`. Base32 wants
    upper case and no spaces, and needs its padding back.
    """
    cleaned = "".join(str(secret).split()).upper().replace("-", "")
    padding = (-len(cleaned)) % 8
    return cleaned + ("=" * padding)


def code_at(secret: str, when: float) -> str:
    counter = int(when // PERIOD)
    key = base64.b32decode(normalise_secret(secret), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** DIGITS)).zfill(DIGITS)


def seconds_left(when: float | None = None) -> int:
    """How long the current code stays valid."""
    when = time.time() if when is None else when
    return PERIOD - int(when % PERIOD)


def fresh_code(secret: str, min_seconds: int = SAFE_SECONDS,
               sleep=time.sleep, now=time.time) -> tuple[str, int]:
    """A code with enough life left to be typed, and how many seconds that is.

    Waits out the tail of a window rather than handing back a code that will
    expire mid-typing -- which is the failure that cost an afternoon, because
    it looks exactly like a rejected password.

    `now` is injected so the waiting can be tested without waiting: the clock
    has to be read **again** after the sleep, or the returned figure describes
    the window that was just abandoned.
    """
    when = now()
    remaining = seconds_left(when)
    if remaining < min_seconds:
        sleep(remaining + 1)
        when = now()
        remaining = seconds_left(when)
    return code_at(secret, when), remaining
