@echo off
REM ScholarScout Feishu websocket long-connection - one-click launcher (double-click me).
REM Kept ASCII-only on purpose: cmd.exe reads .bat bytes as GBK on zh-CN Windows,
REM so a Chinese literal here would show up garbled. The Chinese banner is printed
REM by scripts/feishu_ws_up.py instead (chcp 65001 below makes it render correctly).
REM NOTE: no tunnel / no public callback URL is needed - the long connection dials out.
cd /d "%~dp0"
title ScholarScout Feishu On - %DATE% %TIME%
chcp 65001 >nul
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ScholarScout Feishu websocket long connection - one-click up
echo   project : %CD%
echo   python  : %PY%
echo   note    : no tunnel needed (outbound long connection)
echo.
"%PY%" "%~dp0scripts\feishu_ws_up.py" %*
echo.
echo [done] exit code %ERRORLEVEL%  (0 = ok; 2 = prerequisites missing; 3 = start failed)
echo Press any key to close this window...
pause >nul
