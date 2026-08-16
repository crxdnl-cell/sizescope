@echo off
rem Launch SizeScope from source. Requires Python 3.8+ (with tkinter) on PATH.
rem For the no-install version, download SizeScope.exe from GitHub Releases.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0sizescope.py"
) else (
    echo pythonw not found. Install Python from https://python.org
    echo (tick "Add python.exe to PATH" in the installer), or download the
    echo portable exe from https://github.com/crxdnl-cell/sizescope/releases
    pause
)
