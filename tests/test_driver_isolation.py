from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "mira_api"

#: Unica frontera que puede hablar con el driver de PostgreSQL. El resto del
#: servicio pasa SQL ya validado a `db.executor.ReadOnlyExecutor` y nunca ve una
#: conexion; asi migrar de proveedor es cambiar `DATABASE_URL`, no reescribir codigo.
ALLOWED_DIR = SRC / "db"


def _is_psycopg(module: str) -> bool:
    root = module.split(".")[0]
    return root == "psycopg" or root.startswith("psycopg_")


def _imports_psycopg(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_psycopg(alias.name) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and _is_psycopg(node.module):
                return True
    return False


def test_solo_db_importa_psycopg() -> None:
    offenders = [
        path
        for path in SRC.rglob("*.py")
        if ALLOWED_DIR not in path.parents and _imports_psycopg(path)
    ]
    assert offenders == [], (
        f"Estos modulos importan psycopg fuera de db/: {offenders}. "
        "Deben pasar por db.executor.ReadOnlyExecutor en su lugar."
    )


def test_db_si_puede_importar_psycopg() -> None:
    assert _imports_psycopg(ALLOWED_DIR / "pool.py")
    assert _imports_psycopg(ALLOWED_DIR / "executor.py")
