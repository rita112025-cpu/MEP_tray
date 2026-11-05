@echo off
rem MEP tray local web UI launcher (binds 127.0.0.1 only).
rem Uses the project .venv if present, otherwise the system Python.
rem Copy the printed URL (it contains a token) into your browser. Ctrl+C stops it.

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m mep_tray.webui %*
if errorlevel 1 (
    echo.
    echo Launch failed with exit code %errorlevel%. Check that requirements.txt is installed.
    pause
)
