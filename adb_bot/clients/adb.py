from adb_bot.core.models import Profile
from adb_bot.core.adb_commands import tap, swipe, back, write_text
from adb_bot.core.proc import run as run_hidden
import subprocess
import time


def _emit(logger, level: str, message: str, *args) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message, *args)


def argv_from_command(command: str) -> list[str]:
    """Turn an ``adb ...`` command string into an argv list, no shell involved.

    Callers all over the flows build these commands as strings, and they used to
    be run with ``shell=True``. That is unsafe and, on Linux, wrong: POSIX ``sh``
    strips the backslashes that ``escape_text_for_input`` adds for the *Android*
    shell, so a caption like ``it's a good day`` fails to parse and ``a; rm b``
    would run as a second command on the phone. cmd.exe happened to leave those
    backslashes alone, which is the only reason this ever worked on Windows.

    Everything up to and including ``shell`` is adb's own arguments -- targets,
    subcommands and flags we construct ourselves, so splitting on whitespace is
    safe. Everything *after* ``shell`` is the device command and is kept as a
    single element: adb joins its arguments with spaces without escaping them,
    so one element reaches the device shell exactly as written, which is what
    the Android-side escaping already assumes. That makes the string the device
    sees byte-identical to the old Windows behaviour, on both platforms.
    """
    head, separator, device_command = command.partition(" shell ")
    argv = head.split()
    if separator:
        argv.extend(("shell", device_command))
    return argv


# Every ADB command a session with a dropped glogin auth returns this,
# whether it went to a shell command, an `exec-out` screencap, or a `pull`.
# Confirmed live 2026-08-25: a 22-minute Gmail-install retry loop burned its
# entire budget on this -- glogin's session expired partway through a long
# run, nothing detected it, and every screenshot/dump after that came back
# empty (this text decodes as no valid PNG or XML), which every caller
# upstream can only read as "still nothing to tap".
GLOGIN_REQUIRED_MARKER = "you should run glogin to login first"


class ADBClient:
    def __init__(self) -> None:
        # target -> pwd, populated by `authenticate()` on success. Lets a
        # caller that notices its session has silently dropped
        # (`GLOGIN_REQUIRED_MARKER`) re-authenticate and retry, rather than
        # burning its whole budget reading empty screens forever.
        self._pwds: dict[str, str] = {}

    def reauthenticate(self, target: str, logger=None) -> bool:
        """Re-run glogin for `target` using the password seen at its last
        successful `authenticate()`. False if no password was ever recorded
        for it -- there is nothing to retry with."""
        pwd = self._pwds.get(target)
        if not pwd:
            _emit(logger, "warning", "no remembered password for %s; cannot "
                                     "re-authenticate", target)
            return False
        rc, out, err = self._run_capture(f"adb -s {target} shell glogin {pwd}")
        combined = " ".join(part for part in (out, err) if part)
        _emit(logger, "info", "re-glogin for %s -> rc=%s output=%s", target,
             rc, combined or "<no output>")
        return rc == 0

    def run_command(self, command: str) -> str | None:
        try:
            result = run_hidden(
                argv_from_command(command),
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as error:
            print(f"[-] ADB command failed: {error.stderr.strip()}")
            return None

    def _run_capture(self, command: str) -> tuple[int, str, str]:
        """Run a command and always return (returncode, stdout, stderr) without raising."""
        try:
            result = run_hidden(argv_from_command(command),
                                capture_output=True, text=True, check=False)
            return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
        except Exception as exc:  # pragma: no cover - defensive
            return 1, "", str(exc)

    def disconnect(self, target: str) -> None:
        # Clears any stale/offline entry for this endpoint. Output is ignored;
        # "no such device" when nothing was connected is expected and harmless.
        self._run_capture(f"adb disconnect {target}")

    def get_state(self, target: str) -> str:
        """Return adb's state for the target ('device', 'offline', ...)."""
        _rc, out, err = self._run_capture(f"adb -s {target} get-state")
        return (out or err or "").strip().lower()

    def _wait_for_device_state(self, target: str, logger=None, max_wait_seconds: int = 12) -> bool:
        """`adb connect` can report success while the device is still 'offline'.
        Poll until it reaches the usable 'device' state (or give up)."""
        for _ in range(max_wait_seconds):
            state = self.get_state(target)
            if state == "device":
                _emit(logger, "info", "Device %s reached 'device' state", target)
                return True
            _emit(logger, "info", "Device %s state is '%s'; waiting for it to come online", target, state or "<empty>")
            time.sleep(1)
        _emit(logger, "warning", "Device %s connected but never reached 'device' state", target)
        return False

    def connect(self, profile: Profile, logger=None) -> bool:
        if not profile.target:
            _emit(logger, "warning", "Profile %s missing address information", profile.id)
            return False

        target = profile.target
        # Ensure the local adb server is up, then clear any stale entry so a
        # previously-'offline' endpoint does not report a bogus "already
        # connected" without actually being usable.
        self._run_capture("adb start-server")
        self.disconnect(target)

        rc, out, err = self._run_capture(f"adb connect {target}")
        combined = " ".join(part for part in (out, err) if part)
        _emit(logger, "info", "adb connect %s -> rc=%s output=%s", target, rc, combined or "<no output>")

        lowered = combined.lower()
        if "connected to" not in lowered and "already connected" not in lowered:
            _emit(logger, "warning", "adb connect did not report success for %s: %s", target, combined or "<no output>")
            return False

        return self._wait_for_device_state(target, logger=logger)

    def authenticate(self, profile: Profile, logger=None) -> bool:
        if not profile.target or not profile.pwd:
            _emit(logger, "warning", "Profile %s missing target or password for authentication", profile.id)
            return False

        rc, out, err = self._run_capture(f"adb -s {profile.target} shell glogin {profile.pwd}")
        combined = " ".join(part for part in (out, err) if part)
        _emit(logger, "info", "glogin for %s -> rc=%s output=%s", profile.target, rc, combined or "<no output>")

        lowered = combined.lower()
        if any(token in lowered for token in ("success", "authenticated", "ok", "logged in")):
            self._pwds[profile.target] = profile.pwd
            return True
        # Some glogin builds print nothing on success. Treat a clean exit with
        # no error markers as authenticated rather than failing the whole
        # connection over a missing success string.
        if rc == 0 and not any(token in lowered for token in ("error", "fail", "denied", "invalid", "unauthor")):
            _emit(logger, "info", "glogin gave no explicit success text for %s; treating rc=0 as authenticated", profile.target)
            self._pwds[profile.target] = profile.pwd
            return True

        _emit(logger, "warning", "glogin did not confirm authentication for %s: %s", profile.target, combined or "<no output>")
        return False

    def connect_and_auth(self, profile: Profile, logger=None) -> str | None:
        if not self.connect(profile, logger=logger):
            return None
        if not self.authenticate(profile, logger=logger):
            return None
        return profile.target

    def shell_tap(self, target: str, x: int, y: int) -> str | None:
        return self.run_command(f"adb -s {target} shell {tap(x, y)}")

    def shell_swipe(self, target: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str | None:
        return self.run_command(
            f"adb -s {target} shell {swipe(x1, y1, x2, y2, duration_ms)}"
        )

    def shell_back(self, target: str) -> str | None:
        return self.run_command(f"adb -s {target} shell {back()}")

    def shell_write(self, target: str, text: str) -> str | None:
        return self.run_command(f"adb -s {target} shell {write_text(text)}")
