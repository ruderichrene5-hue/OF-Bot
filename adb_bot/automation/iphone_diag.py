"""Failure diagnostics for the iPhone farm (22.10, Rene: "ueberall, wenn der Bot nicht weiter weiss, brauche ich
einen Screenshot").

`keep_screen(ref, reason, detail, container=..., wda=..., xx=...)` saves a PNG of the phone screen plus a .txt with the
reason, the exception text and whatever text is on screen (WDA element labels, or XXTouch OCR) under
/opt/adbbot/logs/screens/<YYYY-MM-DD>/<HHMMSS>_<phone>_<container>_<reason>.png and returns that path. It never raises:
diagnostics must not be able to break a post, a signup or a warm-up. WDA is preferred (works while XXTouch is 403 or
busy); XXTouch's screen.image is the fallback. The path goes into the log line and, where a flow writes Airtable
notes, into the note.
"""
from __future__ import annotations

import html
import logging
import os
import re
import subprocess
import time

SCREEN_DIR = "/opt/adbbot/logs/screens"
log = logging.getLogger("iphone_diag")

_LETTER = {"iphone1": "A", "iphone2": "B", "iphone3": "C", "iphone4": "D", "iphone5": "E", "iphone6": "I", "iphone7": "G",
           "iphone8": "H", "iphone9": "F", "iphone11": "J", "iphone12": "K", "iphone13": "L"}


def _phone_letter(ref: str) -> str:
    try:
        from adb_bot.automation.iphone_farm_wda import resolve
        _rec, phone = resolve(ref)
        return _LETTER.get(str(phone.get("stream")), str(ref)[-1:].upper())
    except Exception:                                       # noqa: BLE001
        return re.sub(r"[^A-Za-z0-9]", "", str(ref))[-1:].upper() or "X"


def _wda_text(src: str) -> str:
    """Visible labels/values out of a WDA XML source, de-duplicated, in document order."""
    seen, out = set(), []
    for m in re.finditer(r"<XCUIElementType(\w+)\s([^>]*?)/?>", src):
        kind, attrs = m.group(1), dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
        if attrs.get("visible") != "true" or kind in ("Application", "Window", "Other", "ScrollView", "Table", "CollectionView"):
            continue
        for key in ("label", "value", "name"):
            t = html.unescape(attrs.get(key, "")).strip()
            if t and t not in seen:
                seen.add(t)
                out.append(f"[{kind}] {t}")
                break
    return "\n".join(out[:120])


def keep_screen(ref: str, reason: str, detail: str = "", container: str = "", wda=None, xx=None, log_fn=None) -> str | None:
    """Screenshot + on-screen text -> /opt/adbbot/logs/screens/<date>/..., returns the .png path (or None)."""
    say = log_fn or (lambda m: log.info(m))
    try:
        day, stamp = time.strftime("%Y-%m-%d"), time.strftime("%H%M%S")
        d = os.path.join(SCREEN_DIR, day)
        os.makedirs(d, exist_ok=True)
        safe = lambda s: re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))[:40]
        base = os.path.join(d, f"{stamp}_{_phone_letter(ref)}{'_' + safe(container) if container else ''}_{safe(reason)}")
        png, text, how = b"", "", ""
        # 1) WDA: screenshot + element labels (works even when XXTouch is unlicensed/busy)
        try:
            p = wda
            if p is None:
                from adb_bot.automation.iphone_farm_wda import connect
                p = connect(ref, timeout=15)
                # 23.10 live (E7): a fresh WDA session from a diagnostic probe invalidated the running flow's session
                # ("invalid session id"). Reuse the session WDA already has, never create one on a possibly busy phone.
                try:
                    import requests as _rq
                    p.sid = _rq.get(f"{p.base}/status", timeout=6).json().get("sessionId") or p.sid
                except Exception:                           # noqa: BLE001
                    pass
            png = p.screenshot()
            try:
                text = _wda_text(p.source("xml"))
            except Exception:                               # noqa: BLE001
                text = ""
            try:
                a = p.alert_text()
                if a:
                    text = f"[Alert] {a}\n" + text
            except Exception:                               # noqa: BLE001
                pass
            how = "wda"
        except Exception as exc:                            # noqa: BLE001
            how = f"wda failed ({str(exc)[:60]})"
        # 2) XXTouch: screen.image + OCR, when WDA could not deliver a picture (xx may be an iphone_xx_flows.XX
        #    or a bare iphone_xxtouch.Touch -- both have run(); only XX has lua_out/ocr/dest)
        if not png and xx is not None:
            try:
                remote = f"/var/mobile/Media/keep_{stamp}.png"
                lua = f'screen.image():save_to_png_file("{remote}") os.execute("chmod 644 {remote}")'
                if hasattr(xx, "lua_out"):
                    xx.lua_out(lua + ' OUT = "ok"')
                else:
                    xx.run(lua)
                from adb_bot.automation import iphone_farm_config as C
                dest = getattr(xx, "dest", None)
                if not dest:
                    from adb_bot.automation.iphone_farm_wda import resolve
                    dest = resolve(ref)[1].get("ssh") or ref
                png = subprocess.run(["ssh", "-i", C.SSH_KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20",
                                      *C.lan_ssh_opts(dest), dest, f"cat {remote}; rm -f {remote}"], capture_output=True, timeout=60).stdout
                if not text and hasattr(xx, "ocr"):
                    text = "\n".join(w.get("text", "") for w in xx.ocr())
                how += " +xxtouch"
            except Exception as exc:                        # noqa: BLE001
                how += f" xxtouch failed ({str(exc)[:60]})"
        header = (f"reason: {reason}\nphone: {ref}  container: {container or '-'}\nwhen: {day} {stamp}\nvia: {how}\n"
                  f"detail: {detail}\n---- on screen ----\n")
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(header + (text or "(no text captured)"))
        if png:
            with open(base + ".png", "wb") as f:
                f.write(png)
            say(f"screen kept: {base}.png")
            return base + ".png"
        say(f"screen text kept (no picture, {how}): {base}.txt")
        return base + ".txt"
    except Exception as exc:                                # noqa: BLE001
        say(f"screen keep failed: {exc}")
        return None


def notify(text: str, category: str = "phones") -> None:
    """Telegram (category 'phones') when configured; always the log."""
    log.warning("ALERT %s", text)
    try:
        from adb_bot.clients.telegram import TelegramNotifier
        TelegramNotifier().send(f"📱 <b>iPhone farm</b>\n{text}", logger=log, category=category)
    except Exception as exc:                                # noqa: BLE001
        log.warning("telegram failed: %s", exc)
