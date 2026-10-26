@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM   XiaoA - single entry point
REM   Sets up Python + Node dependencies, then starts the app.
REM   Electron launches the backend itself, so no second window.
REM ============================================================

set "VENV=%USERPROFILE%\xiaoa-venv"
set "PY=%VENV%\Scripts\python.exe"
set "HF_HOME=%USERPROFILE%\xiaoa-cache"
set "XIAOA_HOME=%~dp0"

REM Use the configured npm registry and Electron verified-download defaults.

echo ============================================
echo    XiaoA Launcher
echo ============================================
echo.

REM --- 1. Python ---
where python >nul 2>nul
if errorlevel 1 goto no_python

REM --- 2. Virtual environment ---
if exist "%PY%" goto venv_ok
echo [1/4] Creating virtual environment at %VENV% ...
python -m venv "%VENV%"
if errorlevel 1 goto venv_fail
goto venv_done
:venv_ok
echo [1/4] Virtual environment ready.
:venv_done
echo.

REM --- 3. Python dependencies (reinstall when requirements change) ---
REM The marker stores a hash of both requirement files. If you pull code that
REM adds a dependency, the hash changes and we install it automatically.
set "REQ_MARK=%VENV%\.deps_hash"
REM Let PowerShell calculate, compare, and write the dependency marker.
REM Avoid cmd capture and echo redirection, which break on pipes or empty values.
REM Exit code 0 means current; exit code 1 means installation is needed.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop';" ^
 "$files = @('backend\requirements.txt','backend\requirements-optional.txt');" ^
 "$now = -join ($files | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash });" ^
 "$mark = $env:REQ_MARK;" ^
 "$old = ''; if (Test-Path -LiteralPath $mark) { $old = [Convert]::ToString((Get-Content -LiteralPath $mark -Raw)) };" ^
 "if ($old.Trim() -eq $now) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto req_ok

:req_install
echo [2/4] Installing Python dependencies. This can take a few minutes...
"%PY%" -m pip install --upgrade pip -q
"%PY%" -m pip install -r backend\requirements.txt
if errorlevel 1 goto deps_fail
echo.
echo [2/4] Installing optional vector-memory (fastembed). Failure here is OK.
"%PY%" -m pip install -r backend\requirements-optional.txt
REM Record the requirements hash only after both installs complete.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop';" ^
 "$files = @('backend\requirements.txt','backend\requirements-optional.txt');" ^
 "$now = -join ($files | ForEach-Object { (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash });" ^
 "Set-Content -LiteralPath $env:REQ_MARK -Value $now -NoNewline -Encoding ascii"
goto req_done
:req_ok
echo [2/4] Python dependencies up to date.
:req_done
echo.

REM --- 4. Node + Electron ---
where node >nul 2>nul
if errorlevel 1 goto no_node

pushd frontend

REM Reinstall when the lockfile changes, or when electron is missing/half-installed.
REM Keep the marker outside node_modules because npm ci removes that directory.
set "NPM_MARK=%VENV%\.node_lock_hash"
REM Electron 43 downloads its binary on demand after the JavaScript package install.
REM Ask Electron for its real path instead of assuming a dist filename.
REM The runtime location differs between Electron versions.
if not exist "node_modules\electron\index.js" goto npm_install
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop';" ^
 "$now = (Get-FileHash -Algorithm SHA256 -LiteralPath 'package-lock.json').Hash;" ^
 "$mark = $env:NPM_MARK;" ^
 "$old = ''; if (Test-Path -LiteralPath $mark) { $old = [Convert]::ToString((Get-Content -LiteralPath $mark -Raw)) };" ^
 "if ($old.Trim() -eq $now) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto npm_ok

:npm_install
echo [3/4] Installing Electron via CHINA MIRROR (npmmirror), please wait...
REM Remove a half-finished tree so npm ci starts from a clean state.
if exist "node_modules" rmdir /s /q node_modules
REM ELECTRON_MIRROR is already global; install.js and @electron/get use it.
REM npm 10 rejects npm_config_electron_mirror as an unknown npm setting.
REM npm ci uses package-lock.json for a reproducible dependency tree.
call npm ci
if errorlevel 1 goto npm_fail

REM Electron 43 has no postinstall; npm ci does not download the binary.
REM Use a valid external binary when configured; otherwise fetch from the mirror.
if not defined ELECTRON_OVERRIDE_DIST_PATH goto fetch_electron
if exist "%ELECTRON_OVERRIDE_DIST_PATH%\electron.exe" goto npm_binary_ready
goto electron_missing
:fetch_electron
echo [3/4] Fetching the Electron binary (first time only)...
pushd node_modules\electron
call node install.js
popd
if errorlevel 1 goto electron_missing
:npm_binary_ready
REM Record the lockfile hash only after the Electron binary is present.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop';" ^
 "$now = (Get-FileHash -Algorithm SHA256 -LiteralPath 'package-lock.json').Hash;" ^
 "Set-Content -LiteralPath $env:NPM_MARK -Value $now -NoNewline -Encoding ascii"
goto npm_done
:npm_ok
echo [3/4] Electron dependencies up to date.
:npm_done
echo.

REM --- 5. Start ---
echo [4/4] Starting XiaoA. The backend starts automatically.
echo       Look for the floating ball; click it to open the chat panel.
echo.
REM Match electron\index.js: the override directory directly contains electron.exe.
set "ELECTRON_BIN="
if not defined ELECTRON_OVERRIDE_DIST_PATH goto bundled_electron
set "ELECTRON_BIN=%ELECTRON_OVERRIDE_DIST_PATH%\electron.exe"
goto electron_path_ready
:bundled_electron
REM Read Electron path.txt instead of hard-coding a dist filename.
REM Capturing require(electron) would mix download progress into the path.
REM Check the binary first so path.txt contains only the real relative name.
if exist "node_modules\electron\path.txt" goto have_path
echo [4/4] Electron binary not present yet, fetching it from:
echo       %ELECTRON_MIRROR%
pushd node_modules\electron
call node install.js
popd
if not exist "node_modules\electron\path.txt" goto electron_missing
:have_path
REM path.txt contains a path relative to the Electron dist directory.
for /f "usebackq delims=" %%e in ("node_modules\electron\path.txt") do set "ELECTRON_BIN=node_modules\electron\dist\%%e"
:electron_path_ready
if not defined ELECTRON_BIN goto electron_missing
if not exist "%ELECTRON_BIN%" goto electron_missing
start "" "%ELECTRON_BIN%" .
popd
exit /b 0

:no_python
echo [ERROR] Python not found. Install Python 3.10+ and tick "Add Python to PATH".
goto end

:venv_fail
echo [ERROR] Could not create the virtual environment. Screenshot this and send it to me.
goto end

:deps_fail
echo [ERROR] Python dependency install failed. Screenshot the red error and send it to me.
goto end

:no_node
echo [ERROR] Node.js not found, so the desktop app cannot start.
echo         Install the LTS build from https://nodejs.org/ and run this again.
echo         (To debug the backend, run uvicorn directly as documented in README.md.)
goto end

:npm_fail
popd
echo [ERROR] Electron install failed. Screenshot the error and send it to me.
goto end

:electron_missing
popd
echo [ERROR] Could not obtain the Electron binary (about 100 MB).
if defined ELECTRON_OVERRIDE_DIST_PATH echo         ELECTRON_OVERRIDE_DIST_PATH does not contain electron.exe: %ELECTRON_OVERRIDE_DIST_PATH%
echo         Electron 43+ downloads it on first use, not at install time,
echo         and it does NOT resume - each attempt starts from zero.
echo.
echo         Option 1: just run this script again. Most failures are transient.
echo.
echo         Option 2: watch the real progress and error yourself:
echo           cd frontend\node_modules\electron
echo           node install.js
echo         (the mirror is already set by this script, no need to set it again)
echo.
echo         Option 3: if the network keeps failing, fetch the zip another way
echo         (browser, phone hotspot, another machine):
echo           https://npmmirror.com/mirrors/electron/43.4.0/electron-v43.4.0-win32-x64.zip
echo         Extract it so electron.exe is directly inside the chosen folder,
echo         then point Electron at that folder permanently:
echo           setx ELECTRON_OVERRIDE_DIST_PATH "D:\electron-dist"
echo         Reopen the terminal and run this script again.
goto end

:end
echo.
echo Press any key to close.
pause >nul
