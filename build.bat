@echo off
rem Build Ore Hold Watcher for Windows with Nuitka (run on Windows).
rem
rem Produces a STANDALONE program folder (not a onefile exe): a onefile stub
rem unpacks to %TEMP% and runs itself, which Defender/CrowdStrike flag as
rem dropper behaviour. Outputs:
rem   dist\OreHoldWatcher\OreHoldWatcher.exe   (portable program folder)
rem   dist\OreHoldWatcher-<ver>-win64.zip      (portable zip)
rem   dist\OreHoldWatcher-<ver>-setup.exe      (installer, if Inno Setup found)
rem
rem First build takes a while (Nuitka compiles C).
setlocal enableextensions
cd /d "%~dp0"
set "VER=1.0.0"

if not exist .venv (
    py -3 -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
)
.venv\Scripts\python -m pip install -r requirements.txt nuitka

rem stop a running copy so the destination folder isn't locked
taskkill /f /im OreHoldWatcher.exe >nul 2>&1

if exist build rmdir /s /q build
.venv\Scripts\python -m nuitka ^
    --standalone ^
    --assume-yes-for-downloads ^
    --enable-plugin=pyside6 ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico=icon.ico ^
    --company-name="LittlePhish" ^
    --product-name="Ore Hold Watcher" ^
    --file-version=%VER% ^
    --product-version=%VER% ^
    --file-description="Ore Hold Watcher" ^
    --copyright="Ore Hold Watcher" ^
    --output-dir=build ^
    --output-filename=OreHoldWatcher.exe ^
    app.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED - dist\ left untouched.
    exit /b 1
)

if not exist "build\app.dist\OreHoldWatcher.exe" (
    echo BUILD FAILED - Nuitka produced no program folder.
    exit /b 1
)

if not exist dist mkdir dist
if exist "dist\OreHoldWatcher" rmdir /s /q "dist\OreHoldWatcher"
xcopy /e /i /y "build\app.dist" "dist\OreHoldWatcher" >nul

rem portable zip (name contains "win" so the in-app updater picks it up)
set "ZIP=dist\OreHoldWatcher-%VER%-win64.zip"
if exist "%ZIP%" del /q "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\OreHoldWatcher' -DestinationPath '%ZIP%'"

rem optional installer when Inno Setup is present
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
    "%ISCC%" /DAppVersion=%VER% packaging\ore-hold-watcher.iss
) else (
    echo.
    echo Inno Setup not found - skipping installer. Install it with:
    echo     winget install JRSoftware.InnoSetup
)

echo.
echo Done.
echo   Portable folder: dist\OreHoldWatcher\OreHoldWatcher.exe
echo   Portable zip:    %ZIP%
endlocal
