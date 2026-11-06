@echo off
chcp 65001 >nul
set "RELEASE_DIR=%~dp0"
cd /d "%RELEASE_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%RELEASE_DIR%run_portable_app.ps1"
