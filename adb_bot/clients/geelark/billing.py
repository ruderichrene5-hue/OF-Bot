"""What Geelark costs, and how much runway is left.

This is the endpoint pair that makes Geelark *less* dangerous than MultiLogin,
where running out of minutes stops every launch and every log disguises it as a
server fault. Here the balance is readable before a run, so "the fleet stopped"
never has to be diagnosed from launch failures.

Both endpoints are rate limited far below everything else -- `/pay/wallet` at
10 requests a minute and `/pay/plan/info` at **1 a minute** -- so this must not
be called per phone. Read it once per cycle and pass the answer down.

The billing model, which decides what any of these numbers mean:

* **Parallels** are slots that run a phone with **no per-minute charge**, and
  they are dynamic -- stopping a phone frees its slot for the next one. They are
  not a concurrency cap: with 4 parallels you can still run 40 phones, but 36 of
  them bill per minute.
* **Per-minute** is $0.007/min, capped at $1.20 per device per day, after which
  that device is free for the rest of the day (UTC).
* Geelark's own documentation says parallels do **not** apply to *RPA* sessions
  -- meaning tasks run through Geelark's own `/task` engine, which bill per
  minute regardless. It does not mean API-started phones: a phone started
  through `/phone/start` and then driven over ADB was observed reporting
  `chargingMethod: "Parallels"`. Driving phones ourselves over ADB is therefore
  the arrangement that keeps the parallel slots; handing the same work to
  Geelark's RPA tasks would forfeit them.
"""

from __future__ import annotations

from .transport import GeelarkTransport

WALLET_PATH = "/pay/wallet"
PLAN_PATH = "/pay/plan/info"

# Published per-minute rate for a cloud phone outside a parallel slot.
COST_PER_MINUTE_USD = 0.007

PLAN_LABELS = {0: "Base", 1: "Pro"}


class GeelarkBillingClient:
    def __init__(self, transport: GeelarkTransport | None = None) -> None:
        self.transport = transport or GeelarkTransport()

    def wallet(self) -> dict:
        """`{balance, giftMoney, availableTimeAddOn}`.

        `availableTimeAddOn` is in **minutes** and is spent before cash;
        `giftMoney` is promotional credit spent before `balance`.
        """
        return self.transport.post(WALLET_PATH, {})

    def plan(self) -> dict:
        """`{plan, profiles, parallels, monthlyRental, expirationTime, ...}`.

        Rate limited to one call a minute -- cache it.
        """
        return self.transport.post(PLAN_PATH, {})

    def runway(self) -> dict:
        """One combined view of what is left before launches start failing.

        `minutes_left` is deliberately conservative: it counts purchased time
        add-ons plus every dollar of credit at the per-minute rate, and applies
        to phones running **outside** a parallel slot. Phones inside one cost
        nothing, so real runway is longer whenever the parallel slots are busy.
        """
        wallet = self.wallet()
        plan = self.plan()

        balance = float(wallet.get("balance") or 0.0)
        gift = float(wallet.get("giftMoney") or 0.0)
        add_on = int(wallet.get("availableTimeAddOn") or 0)
        credit = balance + gift

        return {
            "balance": balance,
            "gift": gift,
            "credit": credit,
            "time_addon_minutes": add_on,
            "minutes_left": add_on + int(credit / COST_PER_MINUTE_USD),
            "plan": PLAN_LABELS.get(plan.get("plan"), str(plan.get("plan"))),
            "profiles": int(plan.get("profiles") or 0),
            "profiles_available": int(plan.get("availableProfiles") or 0),
            "parallels": int(plan.get("parallels") or 0),
            "monthly_rentals": int(plan.get("monthlyRental") or 0),
            "monthly_fee": float(plan.get("monthlyFee") or 0.0),
            "expires_at": int(plan.get("expirationTime") or 0),
        }
