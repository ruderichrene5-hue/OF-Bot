"""Invent an identity for a new account, and remember how to log into it.

Two jobs that have to stay together, because an account created without its
credentials stored is worse than no account: it warms up, it posts, and the
first time it is logged out nobody can get back in. Roughly sixteen profiles in
this fleet are already in that state.

**Where the credentials live is deliberately a local file, not Airtable.**
`TODO_2026-08-13 §2.4` leaves that open, and everyone with base access can read
an Airtable field. `~/.adb_bot/accounts/accounts.json`, mode 600, is the
smallest thing that is not lossy; exporting to Airtable later is a decision
somebody can make with the data already in hand rather than a decision that has
to be made before the first account exists.

Usernames must satisfy `ONBOARDING_A_MODEL.md`: unique across the workspace,
and never containing `link` (those are treated as link-in-bio accounts rather
than posting targets). The profile *name* carries the model join key --
`Profile Name.split()[0].lower()` -- which is an MLX-side concern; what is
generated here is the Instagram handle.
"""

from __future__ import annotations

import json
import os
import random
import string
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from adb_bot.automation.flows.signup import Identity

ACCOUNTS_DIR = Path.home() / ".adb_bot" / "accounts"
ACCOUNTS_FILE = ACCOUNTS_DIR / "accounts.json"

FIRST_NAMES = (
    "mia", "lena", "emma", "nora", "lina", "clara", "julia", "sara", "elif",
    "maja", "anna", "leni", "ida", "ella", "frida", "hanna", "romy", "alina",
)
LAST_NAMES = (
    "berg", "vogel", "keller", "brandt", "roth", "lange", "hoff", "winter",
    "sommer", "reich", "kraus", "engel", "falk", "sturm", "weiss", "koenig",
)
# Handle shapes that read like a person rather than a generated string.
#
# `{first}{n}` is gone. With a short first name and a small `n` it minted
# handles like `mia38` -- and Instagram silently appends its own digits to a
# handle that short (`mia38` came back as `mia385506`), so the field never
# echoes what was typed and the submit loop spends the whole username budget.
# Every remaining shape carries the surname, so none is short enough to pad.
# No underscores. The input path garbles a handle containing `_`:
# `alina_koenig` was read back correctly by the fill, then drifted to
# `ali_nakoenig` on the next screen, and the submit loop could never reconcile
# it. The dot shapes go in cleanly (they built every account that succeeded),
# so the handle stays a dot-or-plain form.
_PATTERNS = ("{first}.{last}", "{first}{last}{n}", "{first}.{last}{n}")

# The one word a handle may never contain: link-in-bio profiles are excluded
# from posting, so a handle carrying it would quietly opt the account out.
FORBIDDEN = ("link",)

MIN_AGE, MAX_AGE = 23, 38
MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


def _password(rng: random.Random) -> str:
    """Twelve mixed characters. Instagram wants six; being longer is free."""
    alphabet = string.ascii_lowercase + string.digits
    body = "".join(rng.choice(alphabet) for _ in range(9))
    return body + rng.choice("!@#$") + rng.choice(string.ascii_uppercase) + \
        rng.choice(string.digits)


def _birthday(rng: random.Random, today=None):
    today = today or datetime.now(timezone.utc).date()
    age = rng.randint(MIN_AGE, MAX_AGE)
    # 28 keeps every month valid without caring which year it is.
    return rng.randint(1, 28), MONTHS[rng.randint(0, 11)], today.year - age


def load_accounts() -> dict:
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        return json.loads(ACCOUNTS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def taken_usernames() -> set:
    """Every handle this machine has already handed out."""
    return {str(record.get("username", "")).lower()
            for record in load_accounts().values()
            if record.get("username")}


def make_identity(rng: random.Random | None = None, avoid=None) -> Identity:
    """A fresh identity whose handle collides with nothing we know about.

    `avoid` is any extra handles the caller knows are taken -- MLX remarks,
    Airtable rows -- so the uniqueness check is not limited to this file.
    """
    rng = rng or random.Random()
    used = taken_usernames() | {str(name).lower() for name in (avoid or ())}

    for _ in range(200):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        # A four-digit tail, never two: Instagram pads a handle it considers
        # too short, and a short numeric tail is exactly what invites that.
        username = rng.choice(_PATTERNS).format(
            first=first, last=last, n=rng.randint(1000, 9999))
        if any(word in username for word in FORBIDDEN):
            continue
        if username.lower() in used or not (3 <= len(username) <= 28):
            continue
        day, month, year = _birthday(rng)
        return Identity(
            full_name=f"{first.capitalize()} {last.capitalize()}",
            username=username,
            password=_password(rng),
            birth_day=day, birth_month=month, birth_year=year,
        )
    raise RuntimeError("could not invent an unused username in 200 tries")


def record_account(profile_id: str, profile_name: str, identity: Identity,
                   phone_number: str = "", status: str = "created") -> Path:
    """Write the credentials down before anything else can go wrong.

    Keyed by MLX profile id, so a second account on the same phone does not
    overwrite the first -- roughly twenty phones in this fleet hold two.
    """
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    accounts = load_accounts()
    key = f"{profile_id}:{identity.username}"
    accounts[key] = {
        "profile_id": profile_id,
        "profile_name": profile_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phone_number": phone_number,
        "status": status,
        "recovery_email": "",
        **asdict(identity),
    }
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2, sort_keys=True))
    # Passwords: readable by this user only.
    try:
        os.chmod(ACCOUNTS_FILE, 0o600)
    except OSError:
        pass
    return ACCOUNTS_FILE
