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
    # en v_process. Se seleccionan los montos fila por fila, no totalizados:
    # ver la seccion de prohibicion de totalizar dinero mas abajo.
    sql = (
        "select s.supplier_id, a.awarded_amount, a.currency_code "
        "from query.v_process p "
        "join query.v_awards a using (process_id) "
        "join query.v_award_suppliers asup on asup.award_id = a.award_id "
        "join query.v_suppliers s on s.supplier_id = asup.supplier_id "
        "where p.country_code = 'CR' "
        "order by a.awarded_amount desc"
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


# --- Prohibicion de totalizar dinero (decision de producto, 2026-08-21) -------


@pytest.mark.parametrize(
    "sql",
    [
        "select sum(awarded_amount) from query.v_awards",
        "select avg(awarded_amount) from query.v_awards",
        "select sum(estimated_amount) from query.v_process where country_code = 'CR'",
        # Agrupar por moneda tampoco alcanza: sigue siendo un total calculado.
        "select currency_code, sum(awarded_amount) from query.v_awards group by 1",
        # Ni escondido en una expresion.
        "select sum(a.awarded_amount * 2) from query.v_awards a",
    ],
)
def test_rechaza_totalizar_dinero(sql: str) -> None:
    """Un total equivocado es peor que ningun total: se ve autoritativo, se
    cita, y nadie lo vuelve a revisar. Ademas los montos vienen en monedas
    distintas, asi que un SUM() suma colones con dolares."""
    with pytest.raises(SqlRejected) as err:
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)

    assert err.value.outcome is Outcome.REJECTED_SQL_FUNCTION
    assert err.value.rule == "money_aggregation"


@pytest.mark.parametrize(
    "sql",
    [
        # count() no calcula un monto: cuenta filas, y no se puede sumar a mano.
        "select count(*) from query.v_process where country_code = 'CR'",
        # min/max devuelven un valor que SI existe en los datos, no uno calculado:
        # "la adjudicacion mas cara" tiene que seguir andando.
        "select max(awarded_amount) from query.v_awards",
        "select min(awarded_amount) from query.v_awards",
        # Mostrar los montos fila por fila es justamente lo que se quiere.
        "select award_id, awarded_amount, currency_code from query.v_awards"
        " order by awarded_amount desc",
    ],
)
def test_permite_contar_y_mostrar_montos(sql: str) -> None:
    assert validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES).sql


def test_el_rechazo_explica_que_hacer_en_su_lugar() -> None:
    """El detalle vuelve al modelo en el reintento, asi que tiene que decir
    como corregirlo, no solo que esta mal."""
    with pytest.raises(SqlRejected) as err:
        validate(
            "select sum(awarded_amount) from query.v_awards",
            max_rows=MAX_ROWS,
            countries=COUNTRIES,
        )

    assert "fila por fila" in err.value.detail
    assert "moneda" in err.value.detail


# --- NULLS LAST forzado (bug real, 2026-08-21) -------------------------------


def test_agrega_nulls_last_a_un_order_by_descendente() -> None:
    """PostgreSQL pone los nulos ARRIBA en un DESC. "Los 6 procesos mas
    recientes" devolvia seis filas sin fecha: seis procesos reales, con id y
    titulo, que no eran los mas recientes sino justo los que no tienen el
    dato. Nada fallaba, y por eso era grave."""
    sql = (
        "select process_id, publication_date from query.v_process "
        "where country_code = 'CR' order by publication_date desc limit 6"
    )

    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)

    assert result.nulls_last_injected
    assert "NULLS LAST" in result.sql.upper()


def test_no_toca_un_order_by_ascendente() -> None:
    """En ASC los nulos ya van al final: agregar NULLS LAST no cambiaria nada
    y ensuciaria el SQL que se le muestra al usuario como prueba."""
    sql = "select process_id from query.v_process where country_code = 'CR' order by process_id"

    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)

    assert not result.nulls_last_injected


def test_respeta_un_nulls_last_que_ya_venia() -> None:
    sql = (
        "select process_id, publication_date from query.v_process "
        "where country_code = 'CR' order by publication_date desc nulls last"
    )

    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)

    assert not result.nulls_last_injected
    assert "NULLS LAST" in result.sql.upper()


def test_corrige_un_nulls_first_explicito() -> None:
    """Aunque el modelo lo pida explicito: encabezar "las mas caras" con las
    que no tienen monto no es lo que nadie quiso preguntar."""
    sql = (
        "select award_id, awarded_amount from query.v_awards "
        "order by awarded_amount desc nulls first"
    )

    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)

    assert result.nulls_last_injected
    assert "NULLS FIRST" not in result.sql.upper()


def test_alcanza_a_varias_columnas_del_mismo_order_by() -> None:
    sql = (
        "select p.process_id, a.awarded_amount, p.publication_date "
        "from query.v_process p join query.v_awards a using (process_id) "
        "where p.country_code = 'CR' "
        "order by a.awarded_amount desc, p.publication_date desc"
    )

    result = validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)

    assert result.sql.upper().count("NULLS LAST") == 2


# --- Prohibicion de operar sobre dinero (bug real, produccion, 2026-08-25) ---


@pytest.mark.parametrize(
    "sql",
    [
        # El caso real: "el precio en dolares a 8Q el dolar" -> el modelo
        # genero esta division. No es SUM/AVG, asi que la otra regla no la ve.
        "select award_id, awarded_amount / 8 as monto_usd from query.v_awards",
        "select award_id, awarded_amount * 1.1 from query.v_awards",
        "select award_id, awarded_amount + 100 from query.v_awards",
        "select award_id, awarded_amount - estimated_amount from query.v_awards",
        "select estimated_amount / 8 from query.v_process where country_code = 'CR'",
        # Escondido detras de un CAST, igual se detecta.
        "select cast(awarded_amount as numeric) / 8 from query.v_awards",
    ],
)
def test_rechaza_operar_sobre_dinero(sql: str) -> None:
    """El caso real: pedir el precio en dolares a una tasa inventada hizo que
    el modelo generara una division simple sobre awarded_amount. No agrega
    filas, asi que _check_no_money_aggregation no la ve -- y el resultado
    queda como una celda mas, asi que el verificador anti-alucinacion la
    habria dado por buena: un tipo de cambio inventado, indistinguible de un
    dato real."""
    with pytest.raises(SqlRejected) as err:
        validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES)

    assert err.value.outcome is Outcome.REJECTED_SQL_FUNCTION
    assert err.value.rule == "money_arithmetic"
    assert "conversion de moneda" in err.value.detail


def test_permite_comparar_montos_sin_operar_sobre_ellos() -> None:
    """Un filtro (WHERE awarded_amount > X) no produce un valor nuevo -- no
    es lo mismo que operar sobre el monto para transformarlo."""
    sql = (
        "select award_id, awarded_amount from query.v_awards "
        "where awarded_amount > 1000000 order by awarded_amount desc"
    )
    assert validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES).sql


def test_permite_seguir_mostrando_montos_tal_cual() -> None:
    sql = "select award_id, awarded_amount, currency_code from query.v_awards"
    assert validate(sql, max_rows=MAX_ROWS, countries=COUNTRIES).sql
