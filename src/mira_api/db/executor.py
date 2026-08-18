from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True)
class Rows:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    #: El SQL ya trae el LIMIT inyectado por el validador. Si se llenan exactamente
    #: max_rows filas no podemos distinguir "hay mas" de "sobraron justas"; se marca
    #: truncado como aproximacion conservadora hasta que T2.3 pida max_rows + 1.
    truncated: bool


class ReadOnlyExecutor:
    """Unica puerta de ejecucion contra la base de datos.

    Dos tipos de llamador, ninguno valida aqui:
    - SQL generado por el modelo, ya pasado por `nlq.validator.validate`.
    - SQL propio del servicio con parametros (p.ej. `nlq.entities.resolve_entities`),
      donde `params` va siempre ligado via `cur.execute(sql, params)` -- nunca
      interpolado en el string, para que el texto de busqueda del usuario no pueda
      inyectar SQL.

    `db/` es la unica frontera del servicio que importa psycopg; ningun otro modulo
    lo hace, para que migrar de proveedor sea cambiar `DATABASE_URL`.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def run(
        self, sql: str, *, max_rows: int, params: Mapping[str, Any] | None = None
    ) -> Rows:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await conn.set_read_only(True)
            await cur.execute(sql, params)
            fetched = await cur.fetchall()
            columns = [desc.name for desc in cur.description or []]
        return Rows(
            columns=columns,
            rows=fetched,
            row_count=len(fetched),
            truncated=len(fetched) >= max_rows,
        )
