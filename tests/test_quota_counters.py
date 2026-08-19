from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from mira_api.config import get_settings
from mira_api.db.log_executor import LogExecutor
from mira_api.quota.counters import period_key, read_counter, record_spend


def _has_real_log_credentials() -> bool:
    try:
        settings = get_settings()
    except Exception:
        return False
    return bool(settings.database_url_log)


pytestmark = pytest.mark.skipif(
    not _has_real_log_credentials(),
    reason="requiere DATABASE_URL_LOG real -- prueba de integracion",
)


def test_period_key_formato() -> None:
    from datetime import UTC, datetime

    moment = datetime(2026, 8, 18, 15, 30, tzinfo=UTC)
    assert period_key("DAY", now=moment) == "2026-08-18"
    assert period_key("MONTH", now=moment) == "2026-08"


@pytest.mark.asyncio
async def test_100_incrementos_concurrentes_dan_exactamente_100() -> None:
    """T5.2, listo cuando: 100 peticiones concurrentes del mismo sujeto
    producen exactamente 100 incrementos, sin condiciones de carrera. El
    UPSERT (quota_counters.query_count + 1) lo hace atomico Postgres, no un
    lock en Python -- esto lo prueba contra la base real, no un mock."""
    settings = get_settings()
    # Pool propio de la prueba, con margenes mas generosos que el de produccion:
    # los 100 UPSERT caen sobre la MISMA fila (mismo subject_key/periodo), asi
    # que Postgres los serializa por el lock de fila sin importar cuantas
    # conexiones tenga el pool -- y esta maquina de desarrollo tiene bastante
    # mas latencia hacia Supabase que donde correra el servicio real. Lo que
    # se prueba es que no se pierde ningun incremento, no cuanto tarda aqui.
    pool = AsyncConnectionPool(
        conninfo=settings.database_url_log,
        min_size=2,
        max_size=10,
        timeout=60.0,
        max_waiting=200,
        open=False,
    )
    await pool.open()
    try:
        executor = LogExecutor(pool)
        subject_key = f"test-concurrency-{uuid.uuid4()}"

        async def _increment() -> None:
            await record_spend(
                executor, subject_key=subject_key, period_type="DAY", cost_usd=0.0001
            )

        await asyncio.gather(*(_increment() for _ in range(100)))

        state = await read_counter(executor, subject_key=subject_key, period_type="DAY")
        assert state.query_count == 100
        assert state.spent_usd == pytest.approx(0.01, abs=1e-6)
    finally:
        await pool.close()

    # mira_logger no tiene DELETE (a proposito -- ver docs/database_security.md
    # en MIRA-ETL, es el mismo diseño que le niega mart). La limpieza necesita
    # el rol admin, tomado de una variable de entorno real (no del .env, que es
    # especifico de cada maquina de desarrollo); si no esta se omite en vez de
    # fallar la prueba por un problema de permisos que es, en realidad, correcto.
    admin_dsn = os.environ.get("SUPABASE_DB_URL")
    if admin_dsn:
        async with await psycopg.AsyncConnection.connect(admin_dsn) as conn:
            await conn.execute(
                "delete from analytics.quota_counters where subject_key = %(subject_key)s",
                {"subject_key": subject_key},
            )
            await conn.commit()
