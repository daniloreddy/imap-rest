#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

if [ ! -f ".venv/bin/activate" ]; then
    echo "Virtual environment non trovato, lo creo..."
    python3 -m venv .venv
fi
source .venv/bin/activate
if ! python -c "import ruff" 2>/dev/null; then
    echo "Dipendenze non installate, le installo..."
    pip install -r requirements.dev.txt
fi

echo "=== ruff ==="
ruff check .

echo "=== mypy ==="
mypy app

echo "=== pytest ==="
pytest

echo "Tutti i check sono passati."
