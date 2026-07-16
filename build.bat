@echo off
rem Build a standalone OreHoldWatcher.exe with Nuitka (run on Windows).
rem First build takes a while (Nuitka compiles C). Output: build\OreHoldWatcher.exe
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
)
.venv\Scripts\python -m pip install -r requirements.txt nuitka

.venv\Scripts\python -m nuitka ^
    --onefile ^
    --assume-yes-for-downloads ^
    --enable-plugin=pyside6 ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico=icon.ico ^
    --company-name="Jared" ^
    --product-name="Ore Hold Watcher" ^
    --file-version=1.0.0 ^
    --output-filename=OreHoldWatcher.exe ^
    --output-dir=build ^
    app.py

echo.
echo Done. Exe: build\OreHoldWatcher.exe
