@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\pythonw.exe" goto setup
if not exist ".runtime\libreoffice\runtime.json" goto setup
".venv\Scripts\python.exe" -c "import codexdoc, PySide6, docx, pymupdf, pydantic" >nul 2>&1
if errorlevel 1 goto setup
goto launch
:setup
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
:launch
start "CodexDoc" ".venv\Scripts\pythonw.exe" "scripts\launch.py"
