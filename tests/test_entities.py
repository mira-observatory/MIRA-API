from __future__ import annotations

from typing import Any

import pytest

from mira_api.db.executor import Rows
from mira_api.nlq.entities import _CONFIG, _build_sql, resolve_entities


class _FakeExecutor:
    """Reemplaza ReadOnlyExecutor.run sin tocar una base de datos real.

    Graba la llamada para que las pruebas puedan verificar que los parametros
    del usuario viajan ligados (nunca interpolados en el SQL).
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.last_sql: str | None = None
        self.last_params: dict[str, Any] | None = None

    async def run(self, sql: str, *, max_rows: int, params: dict[str, Any] | None = None) -> Rows:
        self.last_sql = sql
        self.last_params = params
        return Rows(columns=[], rows=self._rows, row_count=len(self._rows), truncated=False)


class _RaisingExecutor:
    """Prueba que resolve_entities corta antes de tocar la base con entrada vacia."""

    async def run(self, sql: str, *, max_rows: int, params: dict[str, Any] | None = None) -> Rows:
        raise AssertionError("no deberia llegar a ejecutar SQL con entrada vacia")


def test_build_sql_referencia_las_columnas_reales_de_cada_vista() -> None:
    buyer_sql = _build_sql(_CONFIG["buyer"])
    assert "query.v_buyers" in buyer_sql
    assert "buyer_id" in buyer_sql
    assert "buyer_tax_id" in buyer_sql
    assert "query.v_process_buyers" in buyer_sql

    supplier_sql = _build_sql(_CONFIG["supplier"])
    assert "query.v_suppliers" in supplier_sql
    assert "supplier_id" in supplier_sql
    assert "supplier_tax_id" in supplier_sql
    assert "query.v_award_suppliers" in supplier_sql


def test_build_sql_usa_query_f_unaccent_no_mart() -> None:
    # query.f_unaccent es la misma funcion, calificada igual, que arma el indice
    # GIN en MIRA-ETL (sql/002_indexes_and_views.sql). Si esto se desalinea el
    # indice deja de usarse en silencio -- sigue siendo correcto, solo lento.
    sql = _build_sql(_CONFIG["supplier"])
    assert "query.f_unaccent(" in sql
    assert "mart.f_unaccent(" not in sql
    assert "mart." not in sql, "el SQL de resolucion de entidades nunca toca mart"


def test_build_sql_califica_similarity_fuera_del_search_path_query() -> None:
    """pg_trgm vive en public, que el pool de lectura no incluye en search_path."""
    sql = _build_sql(_CONFIG["buyer"])
    assert "public.similarity(" in sql
    assert " similarity(" not in sql


@pytest.mark.asyncio
async def test_query_vacia_no_toca_la_base() -> None:
    result = await resolve_entities(
        _RaisingExecutor(),  # type: ignore[arg-type]
        query="   ",
        entity_type="supplier",
        countries=["CR"],
    )
    assert result == []


@pytest.mark.asyncio
async def test_sin_paises_no_toca_la_base() -> None:
    result = await resolve_entities(
        _RaisingExecutor(),  # type: ignore[arg-type]
        query="constructora",
        entity_type="supplier",
        countries=[],
    )
    assert result == []


@pytest.mark.asyncio
async def test_devuelve_todos_los_candidatos_sin_fusionar() -> None:
    # El caso Karro/Carro: dos candidatos parecidos, cada uno con su
    # record_count real. Nunca se combinan en un solo resultado.
    fake = _FakeExecutor(
        rows=[
            {
                "entity_id": 1,
                "country_code": "GT",
                "display_name": "Karro y Limon S.A",
                "name_normalised": "Karro y Limon S.A",
                "tax_id": None,
                "record_count": 6,
                "match_method": "NAME_FUZZY",
                "similarity": 0.9,
            },
            {
                "entity_id": 2,
                "country_code": "GT",
                "display_name": "Carro y Limon S.A",
                "name_normalised": "Carro y Limon S.A",
                "tax_id": None,
                "record_count": 9,
                "match_method": "NAME_FUZZY",
                "similarity": 0.85,
            },
        ]
    )

    candidates = await resolve_entities(
        fake,  # type: ignore[arg-type]
        query="karro y limon",
        entity_type="supplier",
        countries=["GT"],
    )

    assert len(candidates) == 2
    counts = {c.display_name: c.record_count for c in candidates}
    assert counts == {"Karro y Limon S.A": 6, "Carro y Limon S.A": 9}


@pytest.mark.asyncio
async def test_parametros_del_usuario_viajan_ligados_no_interpolados() -> None:
    fake = _FakeExecutor(rows=[])
    texto_hostil = "'; drop table query.v_suppliers; --"

    await resolve_entities(
        fake,  # type: ignore[arg-type]
        query=texto_hostil,
        entity_type="supplier",
        countries=["CR"],
    )

    assert fake.last_sql is not None
    assert texto_hostil not in fake.last_sql
    assert fake.last_params is not None
    assert fake.last_params["query"] == texto_hostil
