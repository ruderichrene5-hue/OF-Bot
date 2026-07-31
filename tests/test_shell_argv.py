"""The adb shell-out layer must never go through a local shell.

These commands used to run with ``shell=True``. That only ever worked because
cmd.exe leaves ``\\`` alone, so the backslashes ``escape_text_for_input`` adds
for the *Android* shell survived. POSIX ``sh`` strips them, so on the Linux
server a caption like "it's a good day" fails to parse, "$20" expands to
nothing, and "a; rm -rf b" would run as a second command on the phone. Captions
and bios come from Airtable, so that was an injection path too.

The contract these tests pin down: the string the *device* shell receives is
byte-identical to what the old Windows path produced, and no caption content
can ever become a separate argument or a shell operator.
"""

import subprocess
import sys
import unittest
from pathlib import Path

from adb_bot.clients.adb import argv_from_command
from adb_bot.core.adb_commands import write_text

REPO_ROOT = Path(__file__).resolve().parents[1]


def _windows_parse(command_line: str) -> list:
    """Split a command line the way a C program's argv is built on Windows."""
    import ctypes
    from ctypes import wintypes

    ctypes.windll.shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    ctypes.windll.shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR,
                                                         ctypes.POINTER(ctypes.c_int)]
    count = ctypes.c_int(0)
    pointer = ctypes.windll.shell32.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not pointer:
        raise ctypes.WinError()
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(pointer)


def device_command(command: str) -> str:
    """What the phone's shell ends up executing.

    adb joins its post-``shell`` arguments with spaces and does not escape them
    ("We don't escape here, just like ssh(1)" -- AOSP commandline.cpp), so the
    device string is just those elements joined.
    """
    argv = argv_from_command(command)
    index = argv.index("shell")
    return " ".join(argv[index + 1:])


class ArgvSplitTest(unittest.TestCase):
    def test_device_command_is_kept_as_one_element(self):
        argv = argv_from_command("adb -s 127.0.0.1:21503 shell input text hello%sworld")
        self.assertEqual(
            argv,
            ["adb", "-s", "127.0.0.1:21503", "shell", "input text hello%sworld"],
        )

    def test_client_arguments_split_normally(self):
        argv = argv_from_command("adb -s 127.0.0.1:21503 pull /sdcard/a.png /tmp/b.png")
        self.assertEqual(
            argv,
            ["adb", "-s", "127.0.0.1:21503", "pull", "/sdcard/a.png", "/tmp/b.png"],
        )

    def test_command_without_target(self):
        self.assertEqual(argv_from_command("adb connect 127.0.0.1:21503"),
                         ["adb", "connect", "127.0.0.1:21503"])


class CaptionSurvivesIntactTest(unittest.TestCase):
    """Every one of these is mangled by POSIX sh when run with shell=True."""

    CAPTIONS = [
        "New drop $20 today",
        "she said `hi`",
        "it's a good day",
        'say "hello"',
        "A & B",
        "50% off; DM me",
        "cost $5 | free",
        "back\\slash",
    ]

    def test_escaping_reaches_the_device_untouched(self):
        for caption in self.CAPTIONS:
            with self.subTest(caption=caption):
                expected = write_text(caption)
                got = device_command(f"adb -s 127.0.0.1:21503 shell {expected}")
                self.assertEqual(got, expected)

    def test_caption_never_becomes_extra_arguments(self):
        """A caption with shell operators must stay inside the single device
        argument -- never split into argv the local shell could act on."""
        for caption in self.CAPTIONS:
            with self.subTest(caption=caption):
                argv = argv_from_command(
                    f"adb -s 127.0.0.1:21503 shell {write_text(caption)}")
                self.assertEqual(len(argv), 5, f"caption leaked into argv: {argv}")

    def test_injection_attempt_stays_one_argument(self):
        argv = argv_from_command(
            f"adb -s 127.0.0.1:21503 shell {write_text('bye; rm -rf /sdcard/DCIM')}")
        self.assertEqual(len(argv), 5)
        self.assertNotIn("rm", argv)

    @unittest.skipUnless(sys.platform == "win32", "needs the Windows argv parser")
    def test_windows_round_trip_is_unchanged(self):
        """On Windows subprocess rebuilds the argv into a command line and adb
        re-parses it with the MSVCRT rules. Round-trip it for real: the device
        string that comes back out must be exactly what went in."""
        for caption in self.CAPTIONS:
            with self.subTest(caption=caption):
                payload = write_text(caption)
                argv = argv_from_command(f"adb -s 127.0.0.1:21503 shell {payload}")
                parsed = _windows_parse(subprocess.list2cmdline(argv))
                self.assertEqual(parsed, argv)
                self.assertEqual(" ".join(parsed[parsed.index("shell") + 1:]), payload)


class NoLocalShellTest(unittest.TestCase):
    def test_package_has_no_shell_true(self):
        """Regression guard: a new shell=True would silently reintroduce the bug
        on Linux while still passing on Windows."""
        offenders = []
        for path in (REPO_ROOT / "adb_bot").rglob("*.py"):
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if "shell=True" in stripped and not stripped.startswith(("#", "``", '"')):
                    if "``shell=True``" in stripped:   # prose in a docstring
                        continue
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [], f"shell=True found: {offenders}")


if __name__ == "__main__":
    unittest.main()
