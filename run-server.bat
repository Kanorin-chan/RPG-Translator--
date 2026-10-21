@echo off
rem --- RPG-Translator : live XUnity server ---------------------------------
rem Double-click to start. Finds Python, checks it, then launches.
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [x] Python was not found.
  echo     Install Python 3.10 or newer from https://www.python.org/downloads/
  echo     and tick "Add python.exe to PATH" in the installer, then run this again.
  echo.
  pause
  exit /b 1
)

%PY% check_env.py --server
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

%PY% -m rpgt.unity.server
if errorlevel 1 (
  echo.
  echo [!] The server exited with an error. Read the message above.
  echo.
  pause
)
endlocal
