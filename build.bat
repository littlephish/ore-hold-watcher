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
rem The program folder also carries LICENSE.txt (GPLv3, this app) and, when
rem update.exe is built, LICENSE-updater.txt (MIT, Jammy LLC).
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

rem License notices - both GPLv3 (this app) and MIT (updater/) require the
rem notice to travel with the binaries. Staged into the program folder so the
rem zip and the Inno installer both pick them up with no extra wiring.
copy /y "LICENSE" "dist\OreHoldWatcher\LICENSE.txt" >nul
if errorlevel 1 (
    echo BUILD FAILED - could not stage LICENSE into the program folder.
    exit /b 1
)

rem static, dependency-free update.exe (Rust) - shipped inside the folder
where cargo >nul 2>&1
if %errorlevel%==0 (
    cargo build --release --manifest-path updater\Cargo.toml
    if exist "updater\target\release\update.exe" (
        copy /y "updater\target\release\update.exe" "dist\OreHoldWatcher\update.exe" >nul
        rem update.exe is MIT/Jammy LLC, not GPL like the rest - its notice
        rem ships only when the binary it covers does.
        copy /y "updater\LICENSE" "dist\OreHoldWatcher\LICENSE-updater.txt" >nul
        if errorlevel 1 (
            echo BUILD FAILED - update.exe staged without its MIT notice.
            exit /b 1
        )
    ) else (
        echo WARNING: cargo build produced no update.exe - shipping without it.
    )
) else (
    echo.
    echo Rust/cargo not found - the in-app updater ^(update.exe^) will be omitted.
    echo Install Rust from https://rustup.rs to include it.
)

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
