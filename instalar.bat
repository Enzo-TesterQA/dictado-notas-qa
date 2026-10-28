@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === Instalando dictado de notas QA ===
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual en .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: no se pudo crear el entorno virtual. Verifica que Python este instalado y en el PATH.
        pause
        exit /b 1
    )
)

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: fallo la instalacion de dependencias.
    pause
    exit /b 1
)

echo.
echo Descargando modelo de Whisper (solo esta vez)...
".venv\Scripts\python.exe" dictado_qa.py --solo-descargar
if errorlevel 1 (
    echo ERROR: no se pudo descargar el modelo. Revisa la conexion a internet.
    pause
    exit /b 1
)

echo.
echo === Listo. Ya podes usar dictar.bat (funciona sin internet) ===
pause
