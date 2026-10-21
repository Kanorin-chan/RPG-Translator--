@echo off
rem --- Eroge Translator : live XUnity server ------------------------
rem Run this and LEAVE IT OPEN while you play a patched game.
rem (The UI can also start/stop the server for you.)
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

python server.py

echo.
echo [!] Server stopped.
pause
endlocal
