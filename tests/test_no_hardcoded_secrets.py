"""No credential may live in the source tree.

`adb_bot/config/config.py` used to carry a real MultiLogin workspace Automation
Token -- `owner` role, ten-year expiry -- as the fallback for
`get_bearer_token()`. It would have been published the moment this repo went to
GitHub. Tokens belong in the environment (`/etc/adbbot/env` on the server,
machine-level variables on Windows) or the app's dev settings.

This scans everything that git would actually commit, so a re-introduced secret
fails the build instead of reaching a remote.
"""

import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories .gitignore keeps out of the repo -- their contents are never pushed.
IGNORED_DIRS = {
    ".venv", "venv", "build_venv", "build", "dist", "dist_new",
    "__pycache__", ".pytest_cache", ".git", "old code that was working",
}

PATTERNS = [
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{20,}")),
    ("Airtable PAT", re.compile(r"\bpat[A-Za-z0-9]{14,}\.[A-Za-z0-9]{20,}")),
    ("Google private key", re.compile(r"-----BEGIN (RSA )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("hardcoded token assignment",
     re.compile(r"(?i)\b(bearer_token|api_key|apikey|secret|password)\s*=\s*[\"'][A-Za-z0-9._\-]{20,}[\"']")),
]

SCANNED_SUFFIXES = {".py", ".md", ".txt", ".sh", ".ps1", ".bat", ".json",
                    ".command", ".spec", ".cfg", ".toml", ".yml", ".yaml"}


def committable_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if name == "dev_settings.json":       # gitignored: holds real tokens
                continue
            if path.suffix.lower() in SCANNED_SUFFIXES:
                yield path


class NoHardcodedSecretsTest(unittest.TestCase):
    def test_no_secrets_in_committable_files(self):
        findings = []
        for path in committable_files():
            if path.name == Path(__file__).name:   # this file describes the patterns
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for label, pattern in PATTERNS:
                match = pattern.search(text)
                if match:
                    rel = path.relative_to(REPO_ROOT)
                    line = text[:match.start()].count("\n") + 1
                    findings.append(f"{label} at {rel}:{line}")
        self.assertEqual(findings, [], "secrets found in files git would commit:\n  "
                                       + "\n  ".join(findings))

    def test_get_bearer_token_has_no_builtin_fallback(self):
        """With nothing configured it must return empty, not a baked-in token."""
        from adb_bot.config import config, settings

        saved = {}
        original_env = {k: os.environ.pop(k, None)
                        for k in ("MULTILOGIN_BEARER_TOKEN", "MULTILOGIN_TOKEN")}
        original_loader = settings.load_settings
        settings.load_settings = lambda: saved
        try:
            self.assertEqual(config.get_bearer_token(), "")
            saved["bearer_token"] = "from-settings"
            self.assertEqual(config.get_bearer_token(), "from-settings")
            os.environ["MULTILOGIN_TOKEN"] = "from-env"
            self.assertEqual(config.get_bearer_token(), "from-env")
        finally:
            settings.load_settings = original_loader
            os.environ.pop("MULTILOGIN_TOKEN", None)
            for key, value in original_env.items():
                if value is not None:
                    os.environ[key] = value

    def test_default_bearer_token_constant_is_gone(self):
        from adb_bot.config import config
        self.assertFalse(hasattr(config, "DEFAULT_BEARER_TOKEN"))


if __name__ == "__main__":
    unittest.main()
