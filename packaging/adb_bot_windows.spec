# PyInstaller spec for building "ADB Bot.exe" on Windows.
#
# Build with: packaging\build_windows_exe.bat
# (or manually:  pyinstaller packaging\adb_bot_windows.spec --distpath dist --workpath build)
#
# Produces a single-file windowed executable (dist\ADB Bot.exe) that bundles
# the Python interpreter and all pip dependencies (opencv-python, numpy,
# pytesseract, Pillow, requests + ssl/certifi for HTTPS) so the machine running
# it does not need Python installed. Tesseract and adb remain external tools the
# app shells out to via PATH -- they must already be installed to run the OCR
# and device-automation steps.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_all

block_cipher = None

repo_root = Path(SPECPATH).resolve().parent

# requests/urllib3 need certifi's CA bundle at runtime; ssl/_ssl/certifi are
# forced in as hidden imports because PyInstaller's static analysis can miss
# them (they are imported lazily deep inside urllib3), which would otherwise
# leave the frozen app unable to make any HTTPS request (Multilogin API).
datas = collect_data_files("certifi")

# uiautomator2 (and its adbutils backend) load data files and import submodules
# dynamically, which PyInstaller's static analysis misses -- without collecting
# them the frozen app raises "No module named 'uiautomator2'" even though the
# package is installed in the build venv. collect_all pulls in their data,
# binaries, and hidden submodules so the update_bio_u2 flow works in the exe.
binaries = []
for _pkg in ("uiautomator2", "adbutils"):
    _datas, _binaries, _hidden = collect_all(_pkg)
    datas += _datas
    binaries += _binaries

a = Analysis(
    [str(repo_root / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(repo_root)],
    binaries=binaries,
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

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Single-file build: pass binaries/datas straight into EXE (no COLLECT step).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ADB Bot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
