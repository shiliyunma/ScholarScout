@echo off
REM ScholarScout Feishu chain - one-click launcher (double-click this file to run).
REM Kept ASCII-only on purpose: cmd.exe reads .bat bytes as GBK on zh-CN Windows,
REM so a Chinese literal here would show up garbled. The Chinese banner is printed
REM by scripts/feishu_up.py instead (chcp 65001 below makes it render correctly).
cd /d "%~dp0"
title ScholarScout Feishu Up - %DATE% %TIME%
chcp 65001 >nul
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ScholarScout Feishu chain - one-click up
echo   project : %CD%
echo   python  : %PY%
echo.
"%PY%" "%~dp0scripts\feishu_up.py" %*
echo.
echo [done] exit code %ERRORLEVEL%  (0 = ok; 2/3/4/5/6 see the "why" printed above)
echo Press any key to close this window...
pause >nul
