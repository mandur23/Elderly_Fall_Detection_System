@echo off
rem Fall detection central server - one-click setup and start (Windows).
rem Double-click this file. It will:
rem   1. find Python 3.10-3.13, or install Python 3.11 with winget if none is found
rem   2. create the virtual environment (.venv) on first run
rem   3. run "run_server.py --auto": install packages (GPU PyTorch if an NVIDIA GPU is present),
rem      install mkcert, create the HTTPS certificate, open the firewall port,
rem      start the server and open the admin page in the browser.
rem Test without models:  run_server.bat --fake
setlocal
cd /d "%~dp0"
title Fall Detection Server
chcp 65001 >nul

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto run

echo [1/3] Looking for Python 3.10 - 3.13 ...
set "PYEXE="
call :try py -3.12
if not defined PYEXE call :try py -3.11
if not defined PYEXE call :try py -3.10
if not defined PYEXE call :try py -3.13
if not defined PYEXE call :try python
if defined PYEXE goto mkvenv

echo Python was not found. Installing Python 3.11 with winget ...
where winget >nul 2>nul
if errorlevel 1 goto nopython
winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE call :try py -3.11
if defined PYEXE goto mkvenv

:nopython
echo.
echo Could not install Python automatically.
echo Install Python 3.11 from https://www.python.org/downloads/
echo (check "Add python.exe to PATH"), then run this file again.
start "" https://www.python.org/downloads/
goto end

:mkvenv
echo [2/3] Creating the virtual environment (.venv) with %PYEXE% ...
%PYEXE% -m venv .venv
if errorlevel 1 (
    echo Failed to create .venv
    goto end
)

:run
echo [3/3] Setting up and starting the server ... (first run downloads packages, this can take several minutes)
"%VENV_PY%" run_server.py --auto %*
goto end

:try
rem Use the given command only if it really runs Python 3.10 - 3.13.
%* -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=%*"
exit /b 0

:end
echo.
pause
