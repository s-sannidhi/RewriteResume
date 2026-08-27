@echo off
REM Local-only launcher for Windows. Binds to 127.0.0.1 — never exposed off this machine.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo No virtualenv found. Run this first:
  echo     python setup.py
  exit /b 1
)

.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8765 %*
