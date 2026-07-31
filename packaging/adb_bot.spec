# PyInstaller spec for building "ADB Bot.app" on macOS.
#
# Build with: ./packaging/build_mac_app.command
# (or manually: pyinstaller packaging/adb_bot.spec --distpath dist --workpath build)
#
# This bundles the Python interpreter and all pip dependencies (opencv-python,
# numpy, pytesseract, Pillow) so the client doesn't need a working Python
# install. Tesseract and adb are external command-line tools this app shells
# out to (via PATH / common Homebrew locations) and are NOT bundled here --
# they must already be installed on the machine running the app.

import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_all

block_cipher = None

repo_root = Path(SPECPATH).resolve().parent


def _find_homebrew_openssl_libs() -> list[tuple[str, str]]:
    """Locate the real libssl/libcrypto that this build's _ssl.so links against.

    opencv-python bundles its own private, differently-versioned copies of
    libssl.3.dylib/libcrypto.3.dylib. PyInstaller's automatic binary discovery
    can end up bundling that private copy under the same top-level @rpath
    basename Python's own _ssl.so needs, causing "Symbol not found" errors at
    runtime. Explicitly bundling the real Homebrew-provided files with
    top-level destination "." (matching _ssl.so's own rpath resolution)
    ensures the correct ones are the ones actually present.
    """
    candidates = []
    try:
        result = subprocess.run(
            ["brew", "--prefix", "openssl@3"],
            capture_output=True,
            text=True,
            check=False,
        )
        prefix = (result.stdout or "").strip()
        if prefix:
            candidates.append(Path(prefix) / "lib")
    except Exception:
        pass
    candidates += [
        Path("/opt/homebrew/opt/openssl@3/lib"),
        Path("/usr/local/opt/openssl@3/lib"),
    ]

    for lib_dir in candidates:
        libssl = lib_dir / "libssl.3.dylib"
        libcrypto = lib_dir / "libcrypto.3.dylib"
        if libssl.exists() and libcrypto.exists():
            return [(str(libssl), "."), (str(libcrypto), ".")]

    if sys.platform == "darwin":
        raise RuntimeError(
            "Could not locate Homebrew's openssl@3 libssl.3.dylib/libcrypto.3.dylib "
            "(tried 'brew --prefix openssl@3', /opt/homebrew/opt/openssl@3/lib, "
            "/usr/local/opt/openssl@3/lib). Install it with 'brew install openssl@3' "
            "and rebuild -- without it the frozen app cannot make HTTPS requests."
        )
    return []


# requests/urllib3 need certifi's CA bundle at runtime, and the ssl module
# must be forced in as a hidden import -- PyInstaller's static analysis can
# otherwise miss it since it's imported lazily deep inside urllib3, which
# leaves the frozen app unable to make any HTTPS request (Multilogin API).
datas = collect_data_files("certifi")
openssl_binaries = _find_homebrew_openssl_libs()

# uiautomator2 (and its adbutils backend) load data files and import submodules
# dynamically; collect them so the frozen app can import uiautomator2 for the
# update_bio_u2 flow instead of falling back to "not installed".
u2_binaries = []
for _pkg in ("uiautomator2", "adbutils"):
    _datas, _binaries, _hidden = collect_all(_pkg)
    datas += _datas
    u2_binaries += _binaries

a = Analysis(
    [str(repo_root / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(repo_root)],
    binaries=openssl_binaries + u2_binaries,
    datas=datas,
    hiddenimports=[
        "cv2",
        "numpy",
        "pytesseract",
        "PIL",
        "PIL.Image",
        "ssl",
        "_ssl",
        "certifi",
        "uiautomator2",
        "adbutils",
        "lxml",
        "lxml.etree",
        "lxml._elementpath",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# opencv-python bundles its own private copies of libssl.3.dylib /
# libcrypto.3.dylib (a transitive dependency it barely uses -- this app never
# exercises any TLS/crypto code path through cv2), which is a different,
# incompatible OpenSSL build from the one Python's own _ssl.so needs, causing
# "Symbol not found" errors. Simply deleting cv2's copy from the TOC doesn't
# work: PyInstaller's macOS BUNDLE step does its own later pass, re-derives
# cv2's dependency on that file via cv2's own rpath, and symlinks any
# same-named top-level copy to point at that (now-missing) nested path --
# producing a dangling symlink ("no such file") instead of using our copy.
#
# So instead of removing/re-adding entries, rewrite the *source* of every
# existing libssl/libcrypto TOC entry (wherever PyInstaller decided to put
# it -- top-level, cv2-nested, Resources, ...) to point at our known-correct
# Homebrew file. Whatever destination structure or symlinking PyInstaller's
# BUNDLE step ends up building, every copy traces back to correct content.
_openssl_sources_by_name = {Path(src).name: src for src, _ in openssl_binaries}
a.binaries = [
    (dest, _openssl_sources_by_name.get(Path(dest).name, src), typ)
    for dest, src, typ in a.binaries
]
_existing_names = {Path(dest).name for dest, _, _ in a.binaries}
for src, destdir in openssl_binaries:
    if Path(src).name not in _existing_names:
        a.binaries.append((f"{destdir}/{Path(src).name}".lstrip("./"), src, "BINARY"))

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ADB Bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ADB Bot",
)

app = BUNDLE(
    coll,
    name="ADB Bot.app",
    icon=None,
    bundle_identifier="com.adbbot.app",
    info_plist={
        "NSHighResolutionCapable": "True",
        "CFBundleShortVersionString": "1.0.0",
    },
)
