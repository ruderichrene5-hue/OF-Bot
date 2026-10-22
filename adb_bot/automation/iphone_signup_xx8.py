"""Signup driver for the iPhone 7/8 farm phones (G, H, I).

Rene 13.10: the smaller phones get their own code instead of switches inside the iPhone X driver, so a fix
for one screen size can never move a tap on the other. Same flow and the same XXTouch/OCR mechanics as
`iphone_signup_xx.IphoneSignupDriverXX`; only the screen geometry differs.

Numbers measured live on H (iPhone 8, 750x1334 px panel, read through the scale-3 XX units every farm phone
uses): screen 250 x 444.7 units, the Email placeholder at y=150 with its field right on it, the birthday
wheels at y~357 with ~22 units per row (13.10, run H2).
"""
from __future__ import annotations

from adb_bot.automation.iphone_signup_xx import IphoneSignupDriverXX

# Phones that are iPhone 7/8 (container letter = phone letter). A/B/C/D/E (and F) are iPhone X.
SMALL_PHONE_LETTERS = {"G", "H", "I"}


class IphoneSignupDriverXX8(IphoneSignupDriverXX):
    SCREEN = (250.0, 444.7)
    FIELD_GAP = 0            # tap the placeholder itself -- 13.10 H3: label+19 landed under the field (tested: on it works)
    CODE_GAP = 57            # 85 on the iPhone X, same scaling

    def _clear_field(self, f: dict) -> None:
        # 13.10 H3 live: at 80 % of the width the tap hit the password field's eye icon (x~220 of 250) and the
        # field lost focus -- the password never went in. Put the cursor at 60 % instead, still after any text.
        self.x.tap(self.SCREEN[0] * 0.6, f["y"])
        self.x.pause(0.5, 0.8)
        self.x.lua_out('key.send_text(string.rep("\\b", 60)) OUT = "ok"')
        self.x.pause(0.4, 0.7)
