"""Professional machine translation for the portal (18.11, Rene: chatters on an English channel see everything in English,
"keine KI-Englisch, sondern eine professionelle Übersetzung"). DeepL API; the key is pasted once by Rene in
Manager › Einstellungen and lives only in the env file (DEEPL_API_KEY). Free keys end in ":fx" and use api-free.deepl.com.

    translate.ready()                       -> bool
    translate.texts(["…", …], "EN-US")      -> list of translations (same order)
    translate.store_key(key)                -> {"ok": True, "used": n, "limit": m}   (checks the key against /usage first)
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

ENV_FILE = Path(os.environ.get("ADBBOT_ENV_FILE", "/etc/adbbot/env"))


def _base(key: str) -> str:
    return "https://api-free.deepl.com/v2" if key.endswith(":fx") else "https://api.deepl.com/v2"


def ready() -> bool:
    return bool(os.environ.get("DEEPL_API_KEY"))


def texts(items: list[str], target: str = "EN-US", source: str = "DE") -> list[str]:
    key = os.environ.get("DEEPL_API_KEY", "")
    if not key:
        raise RuntimeError("DeepL key missing (Manager › Einstellungen)")
    if not items:
        return []
    r = requests.post(f"{_base(key)}/translate", headers={"Authorization": f"DeepL-Auth-Key {key}"},
                      json={"text": items, "target_lang": target, "source_lang": source, "formality": "prefer_less"}, timeout=30)
    r.raise_for_status()
    return [t["text"] for t in r.json()["translations"]]


def store_key(key: str) -> dict:
    """Check the key against DeepL, then write DEEPL_API_KEY into the env file and the running process."""
    key = (key or "").strip()
    if len(key) < 20:
        raise ValueError("Schlüssel fehlt oder ist zu kurz")
    r = requests.get(f"{_base(key)}/usage", headers={"Authorization": f"DeepL-Auth-Key {key}"}, timeout=20)
    if r.status_code != 200:
        raise ValueError(f"DeepL lehnt den Schlüssel ab ({r.status_code})")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [ln for ln in lines if not ln.startswith("DEEPL_API_KEY=")] + [f"DEEPL_API_KEY={key}"]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["DEEPL_API_KEY"] = key
    u = r.json()
    return {"ok": True, "used": u.get("character_count", 0), "limit": u.get("character_limit", 0)}
