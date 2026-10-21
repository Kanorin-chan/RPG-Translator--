@echo off
rem --- Eroge Translator : control UI -------------------------------
rem Double-click to open the translator. Keep this console open while
rem it runs -- errors show up here.
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

python ui.py

if errorlevel 1 (
  echo.
  echo [!] The UI exited with an error. Read the message above.
  echo     If 'python' was not found, try replacing python with py
  echo.
  pause
)
endlocal
