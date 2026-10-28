@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Dictado de notas QA

if not exist ".venv\Scripts\python.exe" (
    echo No encuentro el entorno virtual. Ejecuta primero instalar.bat
    pause
    exit /b 1
)

".venv\Scripts\python.exe" dictado_qa.py %*

echo.
pause
