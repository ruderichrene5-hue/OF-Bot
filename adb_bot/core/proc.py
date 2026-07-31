import subprocess

# On Windows, a GUI process with no console of its own (e.g. the packaged
# PyInstaller .exe built with console=False) spawns every child process in a
# brand-new console window, which flashes on screen. CREATE_NO_WINDOW prevents
# that console from being allocated. The flag does not exist on other
# platforms, so it resolves to 0 there and is a harmless no-op.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(*args, **kwargs):
    """Drop-in replacement for subprocess.run that never flashes a console
    window on Windows. All arguments are forwarded unchanged; only the Windows
    creation flags are augmented."""
    if _CREATE_NO_WINDOW:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
    return subprocess.run(*args, **kwargs)


def adb(*args, **kwargs):
    """Run an adb command from an explicit argv list -- never through a shell.

    Why this exists: these commands used to be built as one string and run with
    ``shell=True``. That worked only by accident of running on Windows. cmd.exe
    does not treat ``\\`` as an escape, so the backslashes that
    ``escape_text_for_input`` adds for the *Android* shell survived the trip.
    POSIX ``sh`` eats them, so on Linux a caption like ``it's a good day`` fails
    to parse locally, ``$20`` gets expanded away, and ``a; rm -rf b`` would run
    as a second command on the phone. Captions and bios come from Airtable, so
    that was an injection path as well as a corruption bug.

    Passing argv means only the device's shell ever interprets the text, on
    every platform. Keep a device-side command (``input text ...``,
    ``screencap -p ...``) as ONE element: adb joins its arguments with spaces
    without escaping them, so a single element arrives at the device shell
    exactly as written -- which is what the Android-side escaping expects.
    """
    return run(["adb", *[str(a) for a in args]], **kwargs)
