# ADB Bot UI

## Quick Start

### Windows

1. **First time only**: Run `install_requirements.ps1`
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install_requirements.ps1
   ```

2. **Run the UI**: Double-click `run_ui.bat`

   Or from terminal:
   ```powershell
   .\run_ui.bat
   ```

### macOS / Linux

For the easiest experience on macOS, use the double-click launchers:

- Double-click `install_requirements.command` once to create the virtual environment and install dependencies.
- Double-click `run_ui.command` to start the app.

If Finder blocks them, run them from Terminal instead:

```bash
chmod +x ./install_requirements.command ./run_ui.command ./install_requirements.sh ./run_ui.sh
./install_requirements.command
./run_ui.command
```

You can also use the shell scripts directly:

```bash
chmod +x ./install_requirements.sh
./install_requirements.sh

chmod +x ./run_ui.sh
./run_ui.sh
```

## Manual Setup (without launcher scripts)

### macOS / Linux

```bash
cd /path/to/ADB_Bot
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m adb_bot.ui
```

## What it includes

- Profile selection with checkboxes
- Flow dropdown (Instagram Scroll, Instagram Like Feed)
- Live log panel for workflow activity
- Refresh button that loads profiles from the Multilogin mobile profile API
- Dev controls for customizing launch delays and readiness settings (settings are saved)

Then start the app with:

```bash
python ui.py
```
