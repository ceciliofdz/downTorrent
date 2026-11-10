@echo off
setlocal
cd /d "%~dp0"

set "PYW=python313\pythonw.exe"
if not exist "%PYW%" set "PYW=venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
    where pythonw >nul 2>nul
    if errorlevel 1 (
        echo No se encontro Python para Windows.
        echo Instala Python 3.9 o superior y prepara el entorno:
        echo     py -m venv venv
        echo     venv\Scripts\pip install -r requirements.txt
        pause
        exit /b 1
    )
    set "PYW=pythonw.exe"
)

start "" "%PYW%" "downtorrent_gui.py"
