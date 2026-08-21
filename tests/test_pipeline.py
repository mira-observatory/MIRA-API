from __future__ import annotations

import psycopg
import pytest

from mira_api.api.schemas import Column, QueryRequest
from mira_api.audit.outcomes import Outcome
from mira_api.db.executor import Rows
from mira_api.llm.client import Completion
from mira_api.nlq.pipeline import (
    _infer_column_kind,
    mixed_currency_warning,
    normalise_question,
    run_query,
    wait_for_audit_tasks,
)
from mira_api.quota.counters import period_key

MAX_ROWS = 500
#: Generosos a proposito -- estas pruebas no ejercitan el bloqueo de
#: presupuesto salvo las que lo hacen explicitamente.
BUDGET_DAILY = 1000.0
BUDGET_MONTHLY = 1000.0


def _completion(text: str) -> Completion:
    return Completion(
        text=text, input_tokens=100, output_tokens=20, cache_read_tokens=0, cache_creation_tokens=0
    )


class _ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete_text(
        self, *, model: str, system: list, messages: list, max_tokens: int
    ) -> Completion:
        return _completion(self._responses.pop(0))


class _ScriptedExecutor:
    def __init__(self, *, result: Rows | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    async def run(self, sql: str, *, max_rows: int, params: dict | None = None) -> Rows:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _FakeLogExecutor:
    """Simula analytics.quota_counters en memoria: suficiente para probar la
    logica de presupuesto sin una base de datos real. El UPSERT atomico de
    verdad se prueba por separado, contra Postgres real, en
    test_quota_counters.py."""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, str, str], dict[str, float]] = {}
        #: Renglones que el escritor de auditoria (Hito 4) hubiera insertado --
        #: expuestos para que las pruebas de auditoria los inspeccionen.
        self.query_log_rows: list[dict] = []
        self.query_attempt_rows: list[dict] = []
        self._next_query_log_id = 1

    async def fetch_one(self, sql: str, params: dict | None = None) -> dict | None:
        params = params or {}
        lowered = sql.lower()
        if "analytics.query_log" in lowered:
            row = {"id": self._next_query_log_id, **params}
            self.query_log_rows.append(row)
            self._next_query_log_id += 1
            return {"id": row["id"]}
        key = (params.get("subject_key"), params.get("period_type"), params.get("period_key"))
        if "insert into" in lowered:
            state = self._counters.setdefault(key, {"query_count": 0, "spent_usd": 0.0})
            state["query_count"] += 1
            state["spent_usd"] += float(params.get("cost_usd", 0.0))
            return {"query_count": state["query_count"], "spent_usd": state["spent_usd"]}
        state = self._counters.get(key)
        return dict(state) if state is not None else None

    async def execute(self, sql: str, params: dict | None = None) -> None:
        if params is not None and "analytics.query_attempt" in sql.lower():
            self.query_attempt_rows.append(dict(params))


def _request(
    question: str = "cuantos procesos hay en CR", *, narrative: bool = False
) -> QueryRequest:
    # narrative=False por defecto: la mayoria de estas pruebas no le
    # interesa la redaccion, y asi no hace falta encolar una segunda
    # respuesta del modelo. Las pruebas de narrativa la piden explicitamente.
    return QueryRequest(question=question, countries=["cr"], narrative=narrative)


def test_columnas_id_son_texto_no_numero() -> None:
    # award_id/process_id son codigos como "MIRA-CR-AWARD-BA79BA102334", no
    # enteros -- verificado en vivo contra produccion (2026-08-19), donde
    # clasificarlas como "number" le hacia perder el dato a cualquier cliente
    # que confiara en `kind` (esperaba un number de JS, no un string).
    assert _infer_column_kind("award_id") == "text"
    assert _infer_column_kind("process_id") == "text"
    assert _infer_column_kind("row_count") == "number"
    assert _infer_column_kind("item_count") == "number"
    assert _infer_column_kind("awarded_amount") == "money"
    assert _infer_column_kind("award_date") == "date"


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
    log_executor = _FakeLogExecutor()

    response = await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=log_executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
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
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
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
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )

    assert response.outcome is Outcome.OUT_OF_SCOPE
    assert response.strategy == "out_of_scope"
    assert response.sql_executed is None
    assert response.rows == []


@pytest.mark.asyncio
async def test_sql_irrecuperable_no_ejecuta_nada() -> None:
    MAX_ATTEMPTS = 3
    client = _ScriptedClient(["select * from mart.processes"] * MAX_ATTEMPTS)
    executor = _ScriptedExecutor(error=AssertionError("no deberia ejecutarse"))

    response = await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
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
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )

    assert response.outcome is Outcome.FAILED_DB_TIMEOUT
    # El SQL sí se intento ejecutar -- se conserva para diagnostico, aunque
    # no haya filas.
    assert response.sql_executed is not None
    assert response.rows == []


# --- Presupuesto (Hito 5) ----------------------------------------------------


class _RaisingClient:
    async def complete_text(self, **kwargs: object) -> Completion:
        raise AssertionError("no deberia llamarse al modelo con el presupuesto agotado")


@pytest.mark.asyncio
async def test_presupuesto_agotado_bloquea_antes_de_llamar_al_modelo() -> None:
    executor = _ScriptedExecutor(error=AssertionError("no deberia ejecutarse"))
    log_executor = _FakeLogExecutor()
    # Simula gasto ya acumulado hoy, por encima del limite.
    await log_executor.fetch_one(
        "insert into analytics.quota_counters ...",
        {
            "subject_key": "GLOBAL",
            "period_type": "DAY",
            "period_key": period_key("DAY"),
            "cost_usd": 999.0,
        },
    )

    response = await run_query(
        _request(),
        client=_RaisingClient(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=log_executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=2.5,
        budget_monthly_usd=75.0,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )

    assert response.outcome is Outcome.THROTTLED_BUDGET
    assert response.sql_executed is None
    assert response.rows == []


@pytest.mark.asyncio
async def test_gasto_real_se_registra_despues_de_generar() -> None:
    client = _ScriptedClient(
        ["select process_id from query.v_process where country_code = 'CR'"]
    )
    executor = _ScriptedExecutor(
        result=Rows(
            columns=["process_id"], rows=[{"process_id": "p1"}], row_count=1, truncated=False
        )
    )
    log_executor = _FakeLogExecutor()

    await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=log_executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )

    # 100 tokens de entrada + 20 de salida a precio de Sonnet 5 ($3/$15 por
    # millon) es un costo real y positivo -- se registro, no se dejo en 0.
    from mira_api.quota.counters import period_key

    state = await log_executor.fetch_one(
        "select query_count, spent_usd from analytics.quota_counters where ...",
        {"subject_key": "GLOBAL", "period_type": "DAY", "period_key": period_key("DAY")},
    )
    assert state is not None
    assert state["spent_usd"] > 0


# --- Redaccion y verificacion (T3.5/T3.6) ------------------------------------


@pytest.mark.asyncio
async def test_narrativa_verificada_se_incluye_en_la_respuesta() -> None:
    client = _ScriptedClient(
        [
            "select count(*) as total from query.v_process where country_code = 'CR'",
            "Se encontraron 7992 procesos.",
        ]
    )
    executor = _ScriptedExecutor(
        result=Rows(columns=["total"], rows=[{"total": 7992}], row_count=1, truncated=False)
    )

    response = await run_query(
        _request(narrative=True),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )

    assert response.outcome is Outcome.OK
    assert response.narrative == "Se encontraron 7992 procesos."
    assert response.narrative_verified is True
    assert response.unverified_numbers == []


@pytest.mark.asyncio
async def test_narrativa_alucinada_degrada_el_outcome_sin_perder_los_datos() -> None:
    MAX_NARRATIVE_ATTEMPTS = 2
    client = _ScriptedClient(
        [
            "select count(*) as total from query.v_process where country_code = 'CR'",
            *(["Se encontraron 999999 procesos."] * MAX_NARRATIVE_ATTEMPTS),
        ]
    )
    executor = _ScriptedExecutor(
        result=Rows(columns=["total"], rows=[{"total": 7992}], row_count=1, truncated=False)
    )

    response = await run_query(
        _request(narrative=True),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )

    # Metrica bloqueante: el outcome se degrada, pero los datos siguen ahi.
    assert response.outcome is Outcome.OK_DEGRADED_NARRATIVE
    assert response.narrative_verified is False
    assert response.row_count == 1
    assert response.rows == [{"total": 7992}]
    assert response.narrative is not None  # la plantilla determinista, no None


@pytest.mark.asyncio
async def test_sin_pedir_narrativa_no_se_llama_al_modelo_de_redaccion() -> None:
    client = _ScriptedClient(
        ["select count(*) as total from query.v_process where country_code = 'CR'"]
    )
    executor = _ScriptedExecutor(
        result=Rows(columns=["total"], rows=[{"total": 7992}], row_count=1, truncated=False)
    )

    response = await run_query(
        _request(narrative=False),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )

    assert response.narrative is None
    assert response.outcome is Outcome.OK


# --- Registro de auditoria (Hito 4) ------------------------------------------


@pytest.mark.asyncio
async def test_consulta_ok_escribe_query_log_y_un_query_attempt_aceptado() -> None:
    client = _ScriptedClient(
        ["select process_id from query.v_process where country_code = 'CR'"]
    )
    executor = _ScriptedExecutor(
        result=Rows(
            columns=["process_id"], rows=[{"process_id": "p1"}], row_count=1, truncated=False
        )
    )
    log_executor = _FakeLogExecutor()

    await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=log_executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )
    await wait_for_audit_tasks()

    assert len(log_executor.query_log_rows) == 1
    logged = log_executor.query_log_rows[0]
    assert logged["subject_key"] == "test-subject"
    assert logged["outcome"] == Outcome.OK.value
    assert logged["attempt_count"] == 1

    assert len(log_executor.query_attempt_rows) == 1
    attempt = log_executor.query_attempt_rows[0]
    assert attempt["attempt_number"] == 1
    assert attempt["outcome"] == Outcome.OK.value
    assert attempt["row_count"] == 1
    assert attempt["rejection_rule"] is None


@pytest.mark.asyncio
async def test_sql_irrecuperable_escribe_un_query_attempt_por_intento_rechazado() -> None:
    MAX_ATTEMPTS = 3
    client = _ScriptedClient(["select * from mart.processes"] * MAX_ATTEMPTS)
    executor = _ScriptedExecutor(error=AssertionError("no deberia ejecutarse"))
    log_executor = _FakeLogExecutor()

    await run_query(
        _request(),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=log_executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )
    await wait_for_audit_tasks()

    assert len(log_executor.query_log_rows) == 1
    assert log_executor.query_log_rows[0]["outcome"] == Outcome.REJECTED_SQL_RELATION.value

    assert len(log_executor.query_attempt_rows) == MAX_ATTEMPTS
    for attempt in log_executor.query_attempt_rows:
        assert attempt["outcome"] == Outcome.REJECTED_SQL_RELATION.value
        assert attempt["rejection_rule"] == "forbidden_schema"
        assert attempt["row_count"] is None


@pytest.mark.asyncio
async def test_presupuesto_agotado_escribe_query_log_sin_intentos() -> None:
    executor = _ScriptedExecutor(error=AssertionError("no deberia ejecutarse"))
    log_executor = _FakeLogExecutor()
    await log_executor.fetch_one(
        "insert into analytics.quota_counters ...",
        {
            "subject_key": "GLOBAL",
            "period_type": "DAY",
            "period_key": period_key("DAY"),
            "cost_usd": 999.0,
        },
    )

    await run_query(
        _request(),
        client=_RaisingClient(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=log_executor,  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=2.5,
        budget_monthly_usd=75.0,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
    )
    await wait_for_audit_tasks()

    assert len(log_executor.query_log_rows) == 1
    assert log_executor.query_log_rows[0]["outcome"] == Outcome.THROTTLED_BUDGET.value
    assert log_executor.query_attempt_rows == []


# --- Streaming (Hito 7): callback on_event -----------------------------------


@pytest.mark.asyncio
async def test_on_event_reporta_las_fases_en_orden_para_una_consulta_ok() -> None:
    client = _ScriptedClient(
        [
            "select count(*) as total from query.v_process where country_code = 'CR'",
            "Se encontraron 7992 procesos.",
        ]
    )
    executor = _ScriptedExecutor(
        result=Rows(columns=["total"], rows=[{"total": 7992}], row_count=1, truncated=False)
    )
    events: list[tuple[str, dict]] = []

    await run_query(
        _request(narrative=True),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
        on_event=lambda event, data: events.append((event, data)),
    )

    assert [e for e, _ in events] == ["sql", "row_count", "rows", "narrative", "done"]
    assert "query.v_process" in events[0][1]["sql"]
    assert events[1][1] == {"row_count": 1, "truncated": False}
    assert events[2][1]["rows"] == [{"total": 7992}]
    assert events[3][1]["text"] == "Se encontraron 7992 procesos."
    assert events[3][1]["verified"] is True
    assert events[4][1]["outcome"] == Outcome.OK.value


@pytest.mark.asyncio
async def test_on_event_reporta_error_y_done_para_fuera_de_dominio() -> None:
    client = _ScriptedClient(["OUT_OF_SCOPE"])
    executor = _ScriptedExecutor(error=AssertionError("no deberia ejecutarse"))
    events: list[tuple[str, dict]] = []

    await run_query(
        _request("cual es la capital de Francia"),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
        on_event=lambda event, data: events.append((event, data)),
    )

    assert [e for e, _ in events] == ["error", "done"]
    assert events[0][1]["outcome"] == Outcome.OUT_OF_SCOPE.value
    assert events[1][1]["outcome"] == Outcome.OUT_OF_SCOPE.value


@pytest.mark.asyncio
async def test_on_event_no_reporta_narrativa_si_no_se_pidio() -> None:
    client = _ScriptedClient(
        ["select count(*) as total from query.v_process where country_code = 'CR'"]
    )
    executor = _ScriptedExecutor(
        result=Rows(columns=["total"], rows=[{"total": 7992}], row_count=1, truncated=False)
    )
    events: list[tuple[str, dict]] = []

    await run_query(
        _request(narrative=False),
        client=client,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        log_executor=_FakeLogExecutor(),  # type: ignore[arg-type]
        system_blocks=[],
        model="claude-sonnet-5",
        narrative_model="claude-haiku-4-5-20251001",
        max_rows=MAX_ROWS,
        budget_daily_usd=BUDGET_DAILY,
        budget_monthly_usd=BUDGET_MONTHLY,
        subject_key="test-subject",
        prompt_version="0.1.0",
        app_version="0.1.0",
        on_event=lambda event, data: events.append((event, data)),
    )

    assert [e for e, _ in events] == ["sql", "row_count", "rows", "done"]


# --- Aviso de monedas mezcladas ----------------------------------------------


def _money_columns() -> list[Column]:
    return [
        Column(name="awarded_amount", kind="money", currency_code="CRC"),
        Column(name="currency_code", kind="text"),
    ]

def test_avisa_cuando_la_tabla_mezcla_monedas() -> None:
    """Una tabla ordenada por monto que mezcla monedas esta ordenada por
    numero, no por valor: un quetzal vale unos 65 colones."""
    rows = [
        {"country_code": "CR", "awarded_amount": 15_318_000_000, "currency_code": "CRC"},
        {"country_code": "GT", "awarded_amount": 3_900_000, "currency_code": "GTQ"},
    ]

    aviso = mixed_currency_warning(_money_columns(), rows, ["CR", "GT"])

    assert aviso is not None
    assert aviso.code == "MIXED_CURRENCY"
    assert "CRC" in aviso.message_es and "GTQ" in aviso.message_es
    assert "no son comparables" in aviso.message_es
    assert aviso.details["monedas"] == ["CRC", "GTQ"]


def test_avisa_cuando_un_pais_pedido_no_aparece_en_el_ranking() -> None:
    """El caso real (2026-08-21): se pidieron las 10 adjudicaciones mas caras
    de Costa Rica y Guatemala y volvieron diez costarricenses, cero
    guatemaltecas. La tabla se ve perfectamente normal -- una sola moneda,
    nada raro -- y por eso es peor: quien la lee no tiene como notar que falta
    un pais entero."""
    rows = [
        {"country_code": "CR", "awarded_amount": 15_318_000_000, "currency_code": "CRC"},
        {"country_code": "CR", "awarded_amount": 11_488_500_000, "currency_code": "CRC"},
    ]

    aviso = mixed_currency_warning(_money_columns(), rows, ["CR", "GT"])

    assert aviso is not None
    assert aviso.code == "MIXED_CURRENCY"
    assert "GT" in aviso.message_es
    assert aviso.details["paises_ausentes"] == ["GT"]


def test_sin_aviso_con_una_sola_moneda_y_un_solo_pais_pedido() -> None:
    """Inventar una advertencia donde no hace falta gasta la atencion de quien
    lee, y la proxima ya no la mira."""
    rows = [
        {"country_code": "CR", "awarded_amount": 15_318_000_000, "currency_code": "CRC"},
        {"country_code": "CR", "awarded_amount": 11_488_500_000, "currency_code": "CRC"},
    ]

    assert mixed_currency_warning(_money_columns(), rows, ["CR"]) is None


def test_sin_aviso_si_todos_los_paises_pedidos_aparecen() -> None:
    """Dos paises, una sola moneda (montos ya normalizados o un pais sin
    adjudicaciones propias): no hay nada que aclarar."""
    rows = [
        {"country_code": "CR", "awarded_amount": 900, "currency_code": "CRC"},
        {"country_code": "GT", "awarded_amount": 800, "currency_code": "CRC"},
    ]

    assert mixed_currency_warning(_money_columns(), rows, ["CR", "GT"]) is None


def test_sin_aviso_si_la_tabla_no_trae_montos() -> None:
    """Un conteo por pais no compara dinero: ahi si se pueden poner lado a
    lado sin mentir."""
    columns = [Column(name="country_code", kind="text"), Column(name="count", kind="number")]
    rows = [{"country_code": "CR", "count": 10}]

    assert mixed_currency_warning(columns, rows, ["CR", "GT"]) is None
