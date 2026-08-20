"""Pruebas de integracion del pool contra un Postgres real (T0.3).

Se saltan si no hay `MIRA_TEST_DB_ADMIN_URL` en el entorno -- no hay Docker
disponible en todos los entornos de desarrollo. En CI, `.github/workflows/ci.yml`
levanta un servicio Postgres y exporta esa variable, asi que ahi siempre corren.

Estas pruebas usan psycopg directamente para preparar el esquema de prueba con un
rol admin; eso esta fuera de la frontera de aislamiento del driver (`db/`) porque
vive en `tests/`, no en el servicio.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import pytest_asyncio
from psycopg_pool import PoolTimeout

from mira_api.config import Settings
from mira_api.db.pool import build_read_pool

ADMIN_DSN = os.environ.get("MIRA_TEST_DB_ADMIN_URL")

pytestmark = pytest.mark.skipif(
    not ADMIN_DSN,
    reason="requiere Postgres real: exporta MIRA_TEST_DB_ADMIN_URL (ver ci.yml)",
)


@pytest_asyncio.fixture
async def readonly_dsn() -> AsyncIterator[str]:
    """Crea un esquema `query` de prueba y un rol de solo lectura sobre el, imitando
    lo que MIRA-ETL hara con el esquema real. Se limpia al terminar la prueba."""
    suffix = uuid.uuid4().hex[:8]
    role = f"mira_test_ro_{suffix}"
    password = "test-password"

    assert ADMIN_DSN is not None
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        await admin.execute("create schema if not exists query")
        await admin.execute(
            "create table if not exists query.v_process "
            "(process_id bigint primary key, country_code text)"
        )
        await admin.execute("delete from query.v_process")
        await admin.execute(
            "insert into query.v_process values (1, 'GT'), (2, 'CR') "
            "on conflict do nothing"
        )
        await admin.execute(f"create role {role} login password %s", (password,))
        await admin.execute(f"grant usage on schema query to {role}")
        await admin.execute(f"grant select on query.v_process to {role}")

        parts = urlsplit(ADMIN_DSN)
        netloc = f"{role}:{password}@{parts.hostname}:{parts.port or 5432}"
        dsn = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

        try:
            yield dsn
        finally:
            await admin.execute(f"drop role if exists {role}")


def _settings_for(dsn: str) -> Settings:
    return Settings(
        database_url=dsn,
        database_url_log=dsn,
        token_hmac_secret="test-secret",
        pool_min_size=1,
        pool_max_size=1,
        pool_timeout_s=1.0,
        _env_file=None,
    )  # type: ignore[call-arg]


async def test_select_sobre_vista_permitida(readonly_dsn: str) -> None:
    pool = build_read_pool(_settings_for(readonly_dsn))
    await pool.open()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("select country_code from v_process order by 1")
            rows = await cur.fetchall()
        assert rows == [("CR",), ("GT",)]
    finally:
        await pool.close()


async def test_insert_prohibido_por_permisos(readonly_dsn: str) -> None:
    pool = build_read_pool(_settings_for(readonly_dsn))
    await pool.open()
    try:
        prohibited = (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)
        with pytest.raises(prohibited):
            async with pool.connection() as conn:
                await conn.execute("insert into v_process values (99, 'HN')")
    finally:
        await pool.close()


async def test_agotamiento_de_pool_no_cuelga_y_pool_queda_sano(readonly_dsn: str) -> None:
    settings = _settings_for(readonly_dsn)
    pool = build_read_pool(settings)
    await pool.open()
    try:
        async with pool.connection():
            # El pool tiene max_size=1: una segunda peticion concurrente debe fallar
            # con PoolTimeout en un tiempo acotado, nunca colgarse indefinidamente.
            with pytest.raises(PoolTimeout):
                async with asyncio.timeout(5):
                    async with pool.connection():
                        pass

        # Liberada la primera conexion, el pool sigue sirviendo peticiones normales.
        async with pool.connection() as conn:
            result = await (await conn.execute("select 1")).fetchone()
        assert result == (1,)
    finally:
        await pool.close()
