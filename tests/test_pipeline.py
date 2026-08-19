from __future__ import annotations

import psycopg
import pytest

from mira_api.api.schemas import QueryRequest
from mira_api.audit.outcomes import Outcome
from mira_api.db.executor import Rows
from mira_api.nlq.pipeline import normalise_question, run_query

MAX_ROWS = 500


class _ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete_text(
        self, *, model: str, system: list, messages: list, max_tokens: int
    ) -> str:
        return self._responses.pop(0)


class _ScriptedExecutor:
    def __init__(self, *, result: Rows | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    async def run(self, sql: str, *, max_rows: int, params: dict | None = None) -> Rows:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _request(question: str = "cuantos procesos hay en CR") -> QueryRequest:
    return QueryRequest(question=question, countries=["cr"])


def test_normalise_question_recorta_y_colapsa_espacios() -> None:
    assert normalise_question("  hola   mundo  ") == "hola mundo"
    largo = "a" * 500
    assert len(normalise_question(largo)) == 400


@pytest.mark.asyncio
async def test_pregunta_respondible_devuelve_filas_reales() -> None:
    client = _ScriptedClient(
        ["select process_id, awarded_amount, currency_code from query.v_awards"]
    )
    executor = _ScriptedExecutor(
        result=Rows(
            columns=["process_id", "awarded_amount", "currency_code"],
            rows=[{"process_id": "p1", "awarded_amount": 1000, "currency_code": "CRC"}],
            row_count=1,
            truncated=False,
        )
    )

    response = await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        max_rows=MAX_ROWS,
    )

    assert response.outcome is Outcome.OK
    assert response.strategy == "generated_sql"
    assert response.sql_executed is not None
    assert response.row_count == 1
    assert response.countries_filter == ["CR"]
    money_columns = [c for c in response.columns if c.kind == "money"]
    assert len(money_columns) == 1
    assert money_columns[0].currency_code == "CRC"


@pytest.mark.asyncio
async def test_cero_filas_es_ok_zero_rows_no_error() -> None:
    client = _ScriptedClient(
        ["select process_id from query.v_process where country_code = 'HN'"]
    )
    executor = _ScriptedExecutor(
        result=Rows(columns=["process_id"], rows=[], row_count=0, truncated=False)
    )

    response = await run_query(
        QueryRequest(question="procesos en Honduras en enero", countries=["HN"]),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        max_rows=MAX_ROWS,
    )

    assert response.outcome is Outcome.OK_ZERO_ROWS
    assert response.rows == []


@pytest.mark.asyncio
async def test_pregunta_fuera_de_dominio_no_ejecuta_nada() -> None:
    client = _ScriptedClient(["OUT_OF_SCOPE"])
    executor = _ScriptedExecutor(error=AssertionError("no deberia ejecutarse"))

    response = await run_query(
        _request("cual es la capital de Francia"),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        max_rows=MAX_ROWS,
    )

    assert response.outcome is Outcome.OUT_OF_SCOPE
    assert response.strategy == "out_of_scope"
    assert response.sql_executed is None
    assert response.rows == []


@pytest.mark.asyncio
async def test_sql_irrecuperable_no_ejecuta_nada() -> None:
    from mira_api.nlq.sql_generation import MAX_ATTEMPTS

    client = _ScriptedClient(["select * from mart.processes"] * MAX_ATTEMPTS)
    executor = _ScriptedExecutor(error=AssertionError("no deberia ejecutarse"))

    response = await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        max_rows=MAX_ROWS,
    )

    assert response.outcome is Outcome.REJECTED_SQL_RELATION
    assert response.sql_executed is None


@pytest.mark.asyncio
async def test_timeout_de_base_de_datos_se_reporta_como_tal() -> None:
    client = _ScriptedClient(
        ["select process_id from query.v_process where country_code = 'CR'"]
    )
    executor = _ScriptedExecutor(error=psycopg.errors.QueryCanceled("statement timeout"))

    response = await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        max_rows=MAX_ROWS,
    )

    assert response.outcome is Outcome.FAILED_DB_TIMEOUT
    # El SQL sí se intento ejecutar -- se conserva para diagnostico, aunque
    # no haya filas.
    assert response.sql_executed is not None
    assert response.rows == []
