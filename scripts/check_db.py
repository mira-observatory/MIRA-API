from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mira_api.config import get_settings  # noqa: E402
from mira_api.db.coverage import COVERAGE_SQL  # noqa: E402


CHECKS = {
    "DATABASE_URL_QUERY": (
        "database_url_query",
        "select count(*) from query.semantic_dictionary",
    ),
    "DATABASE_URL_LOG": (
        "database_url_log",
        "select current_user",
    ),
    "DATABASE_URL_WEB": (
        "database_url_web",
        f"select count(*) from ({COVERAGE_SQL}) coverage_check",
    ),
}


def describe_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    username = parts.username or "<missing-user>"
    host = parts.hostname or "<missing-host>"
    port = parts.port or 5432
    return f"user={username} host={host}:{port} db={parts.path.lstrip('/') or '<missing-db>'}"


def main() -> int:
    settings = get_settings()
    failed = False

    for env_name, (field_name, sql) in CHECKS.items():
        dsn = getattr(settings, field_name)
        print(f"\n{env_name}: {describe_dsn(dsn)}")
        try:
            with psycopg.connect(dsn, connect_timeout=15) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    print(f"  OK: {cur.fetchone()}")
        except Exception as exc:
            failed = True
            print(f"  ERROR: {type(exc).__name__}: {str(exc).splitlines()[0]}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
