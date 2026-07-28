#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

if [ ! -f ".venv/bin/activate" ]; then
    echo "Virtual environment non trovato, lo creo..."
    python3 -m venv .venv
fi
source .venv/bin/activate
if [ ! -d ".venv/lib/python3.12/site-packages/fastapi" ] && ! python -c "import fastapi" 2>/dev/null; then
    echo "Dipendenze non installate, le installo..."
    pip install -r requirements.txt
fi

python -m app.main "$@"
