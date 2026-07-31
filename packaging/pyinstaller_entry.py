# Entry point used only by the PyInstaller build (see adb_bot.spec).
#
# adb_bot/ui/__main__.py uses a package-relative import ("from .ui import
# WorkflowUI"), which works fine for `python -m adb_bot.ui` but fails when
# PyInstaller runs it directly as the top-level script (there's no parent
# package at that point, so the relative import raises ImportError). This
# script does the same thing with an absolute import instead.

import sys
import traceback

# Diagnostic: urllib3 catches ImportError internally when it tries to use
# ssl and reports a generic "SSL module is not available" message that
# discards the real underlying exception. Import ssl directly here first so
# the actual root cause (if any) prints to stderr instead of being swallowed.
try:
    import ssl

    print(f"[diagnostic] ssl imported OK: {ssl.OPENSSL_VERSION}", file=sys.stderr)
except Exception:
    print("[diagnostic] import ssl FAILED with the following real traceback:", file=sys.stderr)
    traceback.print_exc()

from adb_bot.ui.ui import WorkflowUI

if __name__ == "__main__":
    ui = WorkflowUI()
    ui.root.mainloop()
