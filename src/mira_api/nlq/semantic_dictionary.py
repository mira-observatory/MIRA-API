from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mira_api.db.executor import ReadOnlyExecutor

#: query.semantic_dictionary no esta en ALLOWED_RELATIONS -- el modelo nunca la
#: consulta por SQL, solo la lee este loader al arrancar el servicio.
_SELECT_SQL = """
    select view_name, column_name, description_es, data_type,
           enum_values, unit, is_aggregable, caveat
    from query.semantic_dictionary
    order by view_name, column_name
"""


@dataclass(frozen=True)
class ColumnDoc:
    view_name: str
    column_name: str
    description_es: str
    data_type: str
    enum_values: list[str] | None
    unit: str | None
    is_aggregable: bool
    caveat: str | None


async def load_semantic_dictionary(executor: ReadOnlyExecutor) -> list[ColumnDoc]:
    """Carga query.semantic_dictionary completa. Se llama una vez al arrancar
    (T3.3): MIRA-API nunca escribe una segunda descripcion de las columnas a
    mano, porque dos copias se desalinean en silencio -- MIRA-ETL es la unica
    fuente de verdad para este texto."""
    result = await executor.run(_SELECT_SQL, max_rows=1000)
    return [_row_to_doc(row) for row in result.rows]


def _row_to_doc(row: dict[str, Any]) -> ColumnDoc:
    return ColumnDoc(
        view_name=row["view_name"],
        column_name=row["column_name"],
        description_es=row["description_es"],
        data_type=row["data_type"],
        enum_values=row["enum_values"],
        unit=row["unit"],
        is_aggregable=row["is_aggregable"],
        caveat=row["caveat"],
    )


def format_for_prompt(columns: list[ColumnDoc]) -> str:
    """Una linea por columna, agrupada por vista. Formato compacto a proposito
    -- esto viaja en cada llamada de generacion de SQL (aunque cacheado)."""
    lines: list[str] = []
    current_view: str | None = None
    for col in sorted(columns, key=lambda c: (c.view_name, c.column_name)):
        if col.view_name != current_view:
            current_view = col.view_name
            lines.append(f"\n{current_view}:")
        parts = [f"  - {col.column_name} ({col.data_type}): {col.description_es}"]
        if col.enum_values:
            parts.append(f"[valores: {', '.join(col.enum_values)}]")
        if col.unit:
            parts.append(f"[moneda en columna: {col.unit}]")
        if col.is_aggregable:
            parts.append("[agregable]")
        if col.caveat:
            parts.append(f"[ADVERTENCIA: {col.caveat}]")
        lines.append(" ".join(parts))
    return "\n".join(lines).strip()
