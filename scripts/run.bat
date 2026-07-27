@echo off
setlocal
cd /d "%~dp0.."

if not exist "venv\Scripts\activate.bat" (
    echo Virtual environment non trovato, lo creo...
    python -m venv venv
)
call venv\Scripts\activate.bat
if not exist "venv\Lib\site-packages\fastapi" (
    echo Dipendenze non installate, le installo...
    pip install -r requirements.txt
)

python -m app.main %*
