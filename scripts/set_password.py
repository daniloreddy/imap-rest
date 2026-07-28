from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VENV_DIR = PROJECT_ROOT / ("venv" if sys.platform == "win32" else ".venv")
_VENV_PYTHON = _VENV_DIR / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _bootstrap() -> None:
    # Deps already importable (Docker: installed globally) — skip venv entirely.
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        import redberry_webkit.auth  # noqa: F401

        return
    except ImportError:
        sys.path.pop(0)

    if not _VENV_PYTHON.exists():
        print(f"venv non trovato in {_VENV_DIR}. Crealo prima (vedi scripts/run.bat).", file=sys.stderr)
        sys.exit(1)
    if Path(sys.executable).resolve() != _VENV_PYTHON.resolve():
        sys.exit(subprocess.run([str(_VENV_PYTHON), *sys.argv]).returncode)


_bootstrap()

from app.ui.router import auth  # noqa: E402


def main() -> None:
    password = getpass.getpass("Nuova password admin: ")
    confirm = getpass.getpass("Conferma password: ")
    if password != confirm:
        print("Le password non coincidono.", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("Password troppo corta (minimo 8 caratteri).", file=sys.stderr)
        sys.exit(1)

    auth.set_password(password)
    print(f"Password impostata. File: {auth.auth_file}")


if __name__ == "__main__":
    main()
