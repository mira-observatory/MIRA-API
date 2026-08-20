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
from psycopg import sql
from psycopg_pool import PoolTimeout

from mira_api.config import Settings
from mira_api.db.pool import build_read_pool

ADMIN_DSN = os.environ.get("MIRA_TEST_DB_ADMIN_URL")

pytestmark = pytest.mark.skipif(
    not ADMIN_DSN,
    reason="requiere Postgres real: exporta MIRA_TEST_DB_ADMIN_URL (ver ci.yml)",
)


async def _parece_la_base_real(admin: psycopg.AsyncConnection) -> bool:
    """El esquema `mart` solo existe donde corrio MIRA-ETL. Un Postgres
    desechable de CI no lo tiene."""
    cur = await admin.execute("select to_regnamespace('mart') is not null")
    row = await cur.fetchone()
    return bool(row and row[0])


@pytest_asyncio.fixture
async def readonly_dsn() -> AsyncIterator[str]:
    """Crea un esquema `query` de prueba y un rol de solo lectura sobre el, imitando
    lo que MIRA-ETL hara con el esquema real. Se limpia al terminar la prueba."""
    suffix = uuid.uuid4().hex[:8]
    role = f"mira_test_ro_{suffix}"
    password = "test-password"

    assert ADMIN_DSN is not None
    async with await psycopg.AsyncConnection.connect(ADMIN_DSN, autocommit=True) as admin:
        # Esta prueba crea y borra roles, y hace DELETE sobre query.v_process.
        # Contra la base real eso destruye datos. El unico resguardo era que
        # nadie exportara MIRA_TEST_DB_ADMIN_URL apuntando ahi -- muy poco para
        # lo que esta en juego. Se aborta antes de ejecutar nada destructivo.
        if await _parece_la_base_real(admin):
            pytest.fail(
                "MIRA_TEST_DB_ADMIN_URL apunta a una base con esquema `mart`: es la real. "
                "Esta prueba borra filas y crea roles; usa un Postgres desechable "
                "(ver el servicio postgres:16 en .github/workflows/ci.yml)."
            )
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
        # PostgreSQL no acepta parametros ($1) en sentencias de utilidad como
        # CREATE ROLE, asi que `execute(sql, (password,))` falla con
        # "syntax error at or near $1". Hay que componer la sentencia, no
        # interpolarla a mano: sql.Literal escapa las comillas de la
        # contrasena y sql.Identifier cita el nombre del rol.
        await admin.execute(
            sql.SQL("create role {} login password {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        await admin.execute(
            sql.SQL("grant usage on schema query to {}").format(sql.Identifier(role))
        )
        await admin.execute(
            sql.SQL("grant select on query.v_process to {}").format(sql.Identifier(role))
        )

        parts = urlsplit(ADMIN_DSN)
        netloc = f"{role}:{password}@{parts.hostname}:{parts.port or 5432}"
        dsn = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

        try:
            yield dsn
        finally:
            # DROP ROLE falla con DependentObjectsStillExist mientras al rol le
            # queden privilegios concedidos (aqui, sobre el esquema query y la
            # tabla v_process). DROP OWNED BY los retira todos en esta base, y
            # cubre de paso cualquier GRANT que se agregue arriba en el futuro
            # y alguien olvide revocar a mano.
            await admin.execute(sql.SQL("drop owned by {}").format(sql.Identifier(role)))
            await admin.execute(
                sql.SQL("drop role if exists {}").format(sql.Identifier(role))
            )


def _settings_for(dsn: str) -> Settings:
    return Settings(
        database_url_query=dsn,
        # Obligatorio aunque estas pruebas no lo usen: Settings falla al
        # construirse si falta, y ese fallo aqui solo aparecia en CI porque
        # el modulo entero se salta sin MIRA_TEST_DB_ADMIN_URL.
        database_url_web=dsn,
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
