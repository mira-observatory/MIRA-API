from __future__ import annotations

from uuid import uuid4

import pytest

from mira_api.api.schemas import QueryResponse
from mira_api.audit.outcomes import Outcome
from mira_api.evals.cases import CASES, Case
from mira_api.evals.runner import evaluate
from mira_api.nlq.validator import ALLOWED_RELATIONS

MAX_ROWS = 500


def _response(**overrides: object) -> QueryResponse:
    base: dict = {
        "query_id": uuid4(),
        "question": "cuantos procesos hay en Costa Rica",
        "strategy": "generated_sql",
        "outcome": Outcome.OK,
        "sql_executed": "SELECT COUNT(*) FROM query.v_process WHERE country_code = 'CR'",
        "countries_filter": ["CR"],
        "row_count": 1,
        "narrative": "Con gusto, hay 5 procesos.",
        "narrative_verified": True,
    }
    base.update(overrides)
    return QueryResponse(**base)


def _case(**overrides: object) -> Case:
    base: dict = {
        "id": "prueba",
        "question": "cuantos procesos hay en Costa Rica",
        "countries": ["CR"],
        "allowed_outcomes": frozenset({Outcome.OK}),
        "expect_relations": frozenset({"query.v_process"}),
        "expect_countries": frozenset({"CR"}),
    }
    base.update(overrides)
    return Case(**base)


# --- El catalogo en si --------------------------------------------------------


def test_los_casos_solo_esperan_vistas_que_el_validador_permite() -> None:
    """Un caso que exigiera una vista fuera de la lista blanca no fallaria por
    un bug del sistema: seria imposible de cumplir por diseno."""
    for case in CASES:
        fuera = case.expect_relations - ALLOWED_RELATIONS
        assert not fuera, f"{case.id} espera vistas no permitidas: {fuera}"


def test_los_casos_esperan_los_paises_que_piden() -> None:
    """El validador rechaza SQL que filtre un pais fuera de `countries`, asi
    que esperar otro seria pedirle al caso algo que nunca puede pasar."""
    for case in CASES:
        pedidos = {c.upper() for c in case.countries}
        assert case.expect_countries <= pedidos, (
            f"{case.id} espera filtrar {case.expect_countries - pedidos}, "
            f"que no esta en countries={sorted(pedidos)}"
        )


def test_los_identificadores_son_unicos() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


# --- La evaluacion de invariantes ---------------------------------------------


def test_caso_que_cumple_todo_pasa() -> None:
    assert evaluate(_case(), _response(), max_rows=MAX_ROWS).ok


def test_detecta_un_outcome_inesperado() -> None:
    result = evaluate(
        _case(), _response(outcome=Outcome.FAILED_DB_TIMEOUT), max_rows=MAX_ROWS
    )
    assert not result.ok
    assert any("FAILED_DB_TIMEOUT" in f for f in result.failures)


def test_detecta_que_falta_la_vista_esperada() -> None:
    result = evaluate(
        _case(expect_relations=frozenset({"query.v_awards"})),
        _response(),
        max_rows=MAX_ROWS,
    )
    assert not result.ok
    assert any("v_awards" in f for f in result.failures)


def test_no_confunde_v_process_con_v_process_buyers() -> None:
    """Comprobar por substring daria un falso OK: 'query.v_process' aparece
    dentro de 'query.v_process_buyers'."""
    result = evaluate(
        _case(expect_relations=frozenset({"query.v_process"})),
        _response(
            sql_executed="SELECT buyer_id FROM query.v_process_buyers",
            countries_filter=["CR"],
        ),
        max_rows=MAX_ROWS,
    )
    assert not result.ok
    assert any("v_process" in f for f in result.failures)


def test_detecta_el_pais_equivocado() -> None:
    """El fallo mas peligroso: responder con datos de otro pais se ve
    perfectamente correcto en pantalla."""
    caso = _case(countries=["HN"], expect_countries=frozenset({"HN"}))
    result = evaluate(
        caso,
        _response(
            sql_executed="SELECT COUNT(*) FROM query.v_process WHERE country_code = 'CR'"
        ),
        max_rows=MAX_ROWS,
    )
    assert not result.ok
    assert any("CR" in f for f in result.failures)


def test_detecta_una_narrativa_no_verificada() -> None:
    result = evaluate(
        _case(),
        _response(narrative_verified=False, unverified_numbers=["999"]),
        max_rows=MAX_ROWS,
    )
    assert not result.ok
    assert any("999" in f for f in result.failures)


def test_con_cero_filas_no_exige_narrativa_verificada() -> None:
    """Sin filas el backend sirve una plantilla determinista; exigir que este
    "verificada" seria pedirle algo que no aplica."""
    result = evaluate(
        _case(allowed_outcomes=frozenset({Outcome.OK_ZERO_ROWS})),
        _response(outcome=Outcome.OK_ZERO_ROWS, row_count=0, narrative_verified=False),
        max_rows=MAX_ROWS,
    )
    assert result.ok


def test_fuera_de_dominio_no_debe_generar_sql() -> None:
    caso = _case(
        allowed_outcomes=frozenset({Outcome.OUT_OF_SCOPE}),
        expect_sql=False,
        expect_verified_narrative=False,
        expect_relations=frozenset(),
        expect_countries=frozenset(),
    )
    result = evaluate(
        caso,
        _response(outcome=Outcome.OUT_OF_SCOPE, sql_executed=None, row_count=0, narrative=None),
        max_rows=MAX_ROWS,
    )
    assert result.ok


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_cada_caso_del_catalogo_es_evaluable(case: Case) -> None:
    """Que evaluate() no reviente con ninguno del catalogo, aunque el
    resultado sea un fallo."""
    result = evaluate(case, _response(outcome=Outcome.OK), max_rows=MAX_ROWS)
    assert isinstance(result.ok, bool)
