import re


def tap(x: int, y: int) -> str:
    """Return an adb shell command that taps at the given screen coordinates."""
    return f"input tap {x} {y}"


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    """Return an adb shell command that swipes between two screen coordinates."""
    return f"input swipe {x1} {y1} {x2} {y2} {duration_ms}"


def back() -> str:
    """Return an adb shell command that presses the Android back button."""
    return "input keyevent 4"


def home() -> str:
    """Return an adb shell command that presses the Android home button."""
    return "input keyevent 3"


def write_text(text: str) -> str:
    """Return an adb shell command that writes text into the focused field."""
    return f"input text {escape_text_for_input(text)}"


def escape_text_for_input(text: str) -> str:
    """Escape text for adb shell input text, replacing spaces and shell-sensitive characters."""
    if not text:
        return ""

    escaped = text.replace(" ", "%s")
    escaped = escaped.replace("\"", "\\\"")
    escaped = escaped.replace("'", "\\'")
    escaped = re.sub(r"[^\w%\-\.,@:=\\']", lambda m: f"\\{m.group(0)}", escaped)
    return escaped
