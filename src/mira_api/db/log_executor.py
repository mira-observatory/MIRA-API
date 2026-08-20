from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class LogExecutor:
    """Unica puerta de escritura del servicio, siempre contra `analytics.*` con
    el rol mira_logger (separado de mira_query -- ver docs/database_security.md
    en MIRA-ETL). A diferencia de ReadOnlyExecutor, este SI escribe: se usa para
    contadores de presupuesto/cuota (T5.2) y, mas adelante, el registro de
    auditoria (Hito 4).

    Sigue viviendo en `db/`: la frontera de aislamiento del driver es por
    paquete, no por si el pool es de lectura o escritura.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def fetch_one(
        self, sql: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
