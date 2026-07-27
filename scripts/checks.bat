@echo off
setlocal
cd /d "%~dp0.."

if not exist "venv\Scripts\activate.bat" (
    echo Virtual environment non trovato, lo creo...
    python -m venv venv
)
call venv\Scripts\activate.bat
if not exist "venv\Lib\site-packages\ruff" (
    echo Dipendenze non installate, le installo...
    pip install -r requirements.dev.txt
)

echo === ruff ===
ruff check .
if errorlevel 1 exit /b 1

echo === mypy ===
mypy app
if errorlevel 1 exit /b 1

echo === pytest ===
pytest
if errorlevel 1 exit /b 1

echo Tutti i check sono passati.
