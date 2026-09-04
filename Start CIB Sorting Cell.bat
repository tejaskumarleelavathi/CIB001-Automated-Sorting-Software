@echo off
REM Start CIB Sorting Cell.bat
REM -----------------------------------------------------------------------
REM One-tap launcher for the CIB Automated Sample Sorting Cell.
REM Double-click this file (or a shortcut to it) to start the program --
REM no terminal, no typing required. Meant to live right next to main.py,
REM wherever this project is deployed (dev laptop or the HMI).
REM
REM If the program crashes or exits immediately, this window stays open
REM (see "pause" below) so the error is actually visible instead of the
REM window just vanishing -- close it manually once you've read it.
REM -----------------------------------------------------------------------

cd /d "%~dp0"
title CIB Sorting Cell

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [ERROR] Could not find .venv\Scripts\python.exe in this folder.
    echo         Expected a Python virtual environment at:
    echo           %~dp0.venv
    echo         See the project setup instructions before running this again.
    echo.
    pause
    exit /b 1
)

echo Starting CIB Sorting Cell...
echo.
".venv\Scripts\python.exe" main.py

echo.
echo -----------------------------------------------------------------------
echo The program has exited. If this was unexpected, read any error above.
echo -----------------------------------------------------------------------
pause
