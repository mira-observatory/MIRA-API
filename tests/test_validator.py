from __future__ import annotations

import pytest

from mira_api.audit.outcomes import Outcome
from mira_api.nlq.validator import SqlRejected, validate

MAX_ROWS = 500


def test_acepta_select_sobre_vista_permitida() -> None:
    sql = "select country_code, count(*) from query.v_process group by 1"
    result = validate(sql, max_rows=MAX_ROWS)
    assert "query.v_process" in result.relations


def test_inyecta_limit_cuando_falta() -> None:
    result = validate("select * from query.v_process", max_rows=MAX_ROWS)
    assert result.limit_injected
    assert "LIMIT 500" in result.sql.upper()


def test_recorta_limit_excesivo() -> None:
    result = validate("select * from query.v_process limit 100000", max_rows=MAX_ROWS)
    assert result.limit_injected
    assert "100000" not in result.sql


def test_respeta_limit_razonable() -> None:
    result = validate("select * from query.v_process limit 10", max_rows=MAX_ROWS)
    assert not result.limit_injected


@pytest.mark.parametrize(
    "sql",
    [
        "delete from query.v_process",
        "update query.v_process set title = 'x'",
        "drop view query.v_process",
        "insert into query.v_process values (1)",
    ],
)
def test_rechaza_todo_lo_que_no_sea_select(sql: str) -> None:
    with pytest.raises(SqlRejected) as err:
        validate(sql, max_rows=MAX_ROWS)
    assert err.value.outcome is Outcome.REJECTED_SQL_NOT_SELECT


def test_rechaza_varias_sentencias() -> None:
    with pytest.raises(SqlRejected) as err:
        validate("select 1 from query.v_process; drop table mart.buyers", max_rows=MAX_ROWS)
    assert err.value.outcome is Outcome.REJECTED_SQL_NOT_SELECT


@pytest.mark.parametrize(
    "relation",
    [
        "mart.procurement_record_core",
        "raw.source_rows",
        "staging.normalized_candidates",
        "audit.etl_runs",
    ],
)
def test_rechaza_esquemas_privados(relation: str) -> None:
    with pytest.raises(SqlRejected) as err:
        validate(f"select * from {relation}", max_rows=MAX_ROWS)
    assert err.value.outcome is Outcome.REJECTED_SQL_RELATION


def test_rechaza_vista_no_permitida() -> None:
    with pytest.raises(SqlRejected) as err:
        validate("select * from query.v_secreta", max_rows=MAX_ROWS)
    assert err.value.outcome is Outcome.REJECTED_SQL_RELATION


def test_rechaza_funcion_peligrosa() -> None:
    with pytest.raises(SqlRejected) as err:
        validate("select pg_read_file('/etc/passwd') from query.v_process", max_rows=MAX_ROWS)
    assert err.value.outcome is Outcome.REJECTED_SQL_FUNCTION


def test_rechaza_cte_recursiva() -> None:
    sql = "with recursive t as (select 1 as n union all select n + 1 from t) select * from t"
    with pytest.raises(SqlRejected):
        validate(sql, max_rows=MAX_ROWS)


def test_rechaza_sql_no_parseable() -> None:
    with pytest.raises(SqlRejected) as err:
        validate("select from where", max_rows=MAX_ROWS)
    assert err.value.outcome in {Outcome.REJECTED_SQL_PARSE, Outcome.REJECTED_SQL_NOT_SELECT}


def test_permite_cte_no_recursiva_sobre_vista_permitida() -> None:
    sql = (
        "with base as (select supplier_id, awarded_amount from query.v_process) "
        "select supplier_id, sum(awarded_amount) from base group by 1"
    )
    result = validate(sql, max_rows=MAX_ROWS)
    assert "query.v_process" in result.relations
