from __future__ import annotations

import pytest

from mira_api.audit.outcomes import Outcome
from mira_api.nlq.validator import SqlRejected, validate

MAX_ROWS = 500
COUNTRIES = ["CR"]


def test_acepta_select_sobre_vista_permitida() -> None:
    sql = "select country_code, count(*) from query.v_process where country_code = 'CR' group by 1"
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert "query.v_process" in result.relations


def test_acepta_select_sobre_v_process_buyers() -> None:
    # v_process_buyers no tiene country_code -- no exige filtro de pais.
    sql = "select buyer_id, count(*) from query.v_process_buyers group by 1"
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert "query.v_process_buyers" in result.relations


def test_acepta_join_entre_process_awards_y_award_suppliers() -> None:
    # "Cuanto se gasto" siempre pasa por aqui: el monto vive en v_awards, no
    # en v_process.
    sql = (
        "select s.supplier_id, sum(a.awarded_amount) "
        "from query.v_process p "
        "join query.v_awards a using (process_id) "
        "join query.v_award_suppliers asup on asup.award_id = a.award_id "
        "join query.v_suppliers s on s.supplier_id = asup.supplier_id "
        "where p.country_code = 'CR' "
        "group by s.supplier_id"
    )
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    expected = {"query.v_process", "query.v_awards", "query.v_award_suppliers", "query.v_suppliers"}
    assert expected <= result.relations


def test_inyecta_limit_cuando_falta() -> None:
    sql = "select * from query.v_process where country_code = 'CR'"
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert result.limit_injected
    assert "LIMIT 500" in result.sql.upper()


def test_recorta_limit_excesivo() -> None:
    sql = "select * from query.v_process where country_code = 'CR' limit 100000"
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert result.limit_injected
    assert "100000" not in result.sql


def test_respeta_limit_razonable() -> None:
    sql = "select * from query.v_process where country_code = 'CR' limit 10"
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
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
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_NOT_SELECT


def test_rechaza_varias_sentencias() -> None:
    with pytest.raises(SqlRejected) as err:
        sql = "select 1 from query.v_process; drop table mart.buyers"
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_NOT_SELECT


@pytest.mark.parametrize(
    "relation",
    [
        "mart.processes",
        "raw.source_rows",
        "staging.normalized_candidates",
        "audit.etl_runs",
    ],
)
def test_rechaza_esquemas_privados(relation: str) -> None:
    with pytest.raises(SqlRejected) as err:
        validate(f"select * from {relation}", max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_RELATION


def test_rechaza_vista_no_permitida() -> None:
    with pytest.raises(SqlRejected) as err:
        validate("select * from query.v_secreta", max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_RELATION


def test_rechaza_funcion_peligrosa() -> None:
    with pytest.raises(SqlRejected) as err:
        sql = "select pg_read_file('/etc/passwd') from query.v_process where country_code = 'CR'"
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_FUNCTION


def test_rechaza_cte_recursiva() -> None:
    sql = "with recursive t as (select 1 as n union all select n + 1 from t) select * from t"
    with pytest.raises(SqlRejected):
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)


def test_rechaza_sql_no_parseable() -> None:
    with pytest.raises(SqlRejected) as err:
        validate("select from where", max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome in {Outcome.REJECTED_SQL_PARSE, Outcome.REJECTED_SQL_NOT_SELECT}


def test_permite_cte_no_recursiva_sobre_vista_permitida() -> None:
    sql = (
        "with base as ("
        "select process_id, estimated_amount from query.v_process where country_code = 'CR'"
        ") "
        "select process_id, estimated_amount from base"
    )
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert "query.v_process" in result.relations


# --- REJECTED_SQL_COUNTRY_SCOPE ---------------------------------------------


def test_rechaza_v_process_sin_filtro_de_pais() -> None:
    with pytest.raises(SqlRejected) as err:
        validate("select * from query.v_process", max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_COUNTRY_SCOPE
    assert err.value.rule == "missing_country_filter"


def test_rechaza_filtro_de_pais_no_pedido() -> None:
    sql = "select * from query.v_process where country_code = 'GT'"
    with pytest.raises(SqlRejected) as err:
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_COUNTRY_SCOPE
    assert err.value.rule == "country_not_allowed"


def test_acepta_lista_in_dentro_de_lo_pedido() -> None:
    sql = "select * from query.v_process where country_code in ('CR', 'GT')"
    result = validate(sql, max_rows=MAX_ROWS, countries=["CR", "GT", "HN"])
    assert "query.v_process" in result.relations


def test_rechaza_lista_in_con_un_pais_fuera_de_lo_pedido() -> None:
    sql = "select * from query.v_process where country_code in ('CR', 'GT')"
    with pytest.raises(SqlRejected) as err:
        validate(sql, max_rows=MAX_ROWS, countries=["CR"])
    assert err.value.outcome is Outcome.REJECTED_SQL_COUNTRY_SCOPE


def test_acepta_igual_any_array() -> None:
    sql = "select * from query.v_process where country_code = any(array['CR', 'GT'])"
    result = validate(sql, max_rows=MAX_ROWS, countries=["CR", "GT"])
    assert "query.v_process" in result.relations


def test_rechaza_subconsulta_como_filtro_de_pais() -> None:
    sql = (
        "select * from query.v_process "
        "where country_code in (select country_code from query.v_buyers)"
    )
    with pytest.raises(SqlRejected) as err:
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert err.value.outcome is Outcome.REJECTED_SQL_COUNTRY_SCOPE


def test_no_exige_filtro_de_pais_si_no_toca_vistas_con_country_code() -> None:
    # v_awards / v_items / v_award_* no tienen country_code -- se llega a
    # ellas por process_id/award_id, ya acotado cuando se une con v_process.
    sql = "select award_id, awarded_amount from query.v_awards"
    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)
    assert "query.v_awards" in result.relations
