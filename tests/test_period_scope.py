from __future__ import annotations

import datetime as dt

import pytest

from mira_api.nlq.period_scope import PeriodScope, changes_period, extract_period, preserves_period

JANUARY = PeriodScope("award_date", dt.date(2025, 1, 1), dt.date(2025, 2, 1))
FILTER = "a.award_date >= '2025-01-01' AND a.award_date < '2025-02-01'"
BASE = "SELECT a.award_date FROM query.v_awards a WHERE "


@pytest.mark.parametrize("predicate", [
    FILTER,
    "a.award_date >= DATE '2025-01-01' AND a.award_date < '2025-02-01'::date",
    "a.award_date >= '2025-01-01 00:00:00' AND a.award_date < '2025-02-01 00:00:00'",
    "CAST(a.award_date AS DATE) BETWEEN '2025-01-01' AND '2025-01-31'",
    "a.award_date >= '2025-01-01' AND a.award_date <= '2025-01-31'",
    "EXTRACT(YEAR FROM a.award_date) = 2025 AND EXTRACT(MONTH FROM a.award_date) = 1",
    "DATE_TRUNC('month', a.award_date) = '2025-01-01'",
    "DATE(a.award_date) BETWEEN '2025-01-01' AND '2025-01-31'",
    "a.AWARD_DATE >= '2025-01-01' AND a.AWARD_DATE < '2025-02-01'",
    "a.awarded_amount = 0 AND (" + FILTER + ")",
])
def test_extracts_month_from_mandatory_date_predicates(predicate: str) -> None:
    assert extract_period(BASE + predicate) == JANUARY
    assert preserves_period(BASE + predicate, JANUARY)


@pytest.mark.parametrize("sql", [
    "SELECT '2025-01-01', '2025-02-01' FROM query.v_awards",
    BASE + "a.awarded_amount = 2025",
    BASE + "EXTRACT(MONTH FROM a.award_date) = 1",
    BASE + "EXTRACT(YEAR FROM a.award_date) = 2025 AND EXTRACT(MONTH FROM a.award_date) = 13",
    BASE + "a.award_date >= '2025-01-01'",
    BASE + "NOT (" + FILTER + ")",
    BASE + "(" + FILTER + ") OR a.awarded_amount = 0",
    BASE + "a.award_date >= '2025-01-01' AND b.award_date < '2025-02-01'",
    "WITH unused AS (" + BASE + FILTER + ") SELECT a.award_date FROM query.v_awards a",
    "not a valid sql statement",
])
def test_does_not_infer_period_from_literals_or_nonbinding_filters(sql: str) -> None:
    assert extract_period(sql) is None


@pytest.mark.parametrize("sql", [
    "SELECT a.award_date FROM query.v_awards a",
    BASE + FILTER.replace("2025", "2026"),
    BASE + "a.award_date >= '2025-01-01' AND a.award_date < '2026-01-01'",
    BASE + "a.award_date >= '2025-01-01' AND a.award_date < '2025-01-15'",
    BASE + "(" + FILTER + ") OR a.awarded_amount = 0",
    "WITH unused AS (" + BASE + FILTER + ") SELECT a.award_date FROM query.v_awards a",
    "SELECT a.award_date FROM query.v_awards a, query.v_awards b WHERE " + FILTER,
    "SELECT a.award_date FROM query.v_awards a JOIN query.v_process p USING (process_id) "
    "WHERE p.publication_date >= '2025-01-01' AND p.publication_date < '2025-02-01'",
])
def test_rejects_queries_that_drop_change_or_bypass_the_period(sql: str) -> None:
    assert not preserves_period(sql, JANUARY)


def test_accepts_date_filter_in_a_used_cte() -> None:
    sql = "WITH january AS (" + BASE + FILTER + ") SELECT * FROM january"
    assert extract_period(sql) == JANUARY
    assert preserves_period(sql, JANUARY)


def test_accepts_new_aliases_without_changing_date_semantics() -> None:
    assert preserves_period((BASE + FILTER).replace("a.", "aw.").replace(" a ", " aw "), JANUARY)


@pytest.mark.parametrize("question", [
    "Que adjudicaciones estuvieron con el valor de 0?",
    "Which awards had an amount of 0?",
    "¿Y en Honduras?",
    "Muestra el proveedor y la fecha de publicación",
    "Agrupa por mes",
    "Muestra las fechas de primera y última publicación",
    "Ahora muestra las que tuvieron un monto de 2025",
])
def test_detail_or_country_follow_up_preserves_period(question: str) -> None:
    assert not changes_period(question)


@pytest.mark.parametrize("question", [
    "¿Y en febrero?", "¿Y en 2026?", "Ahora durante marzo de 2025",
    "In February 2025", "In 2026", "Durante el primer trimestre",
    "Para los últimos 3 meses", "In the last month", "De ayer", "As of today",
    "En todos los meses", "Sin filtro por fecha", "Todo el historial",
    "For all time", "For all years", "Without a date filter",
    "Otra pregunta: cuáles son los procesos más caros?",
])
def test_explicit_date_change_or_reset_overrides_inheritance(question: str) -> None:
    assert changes_period(question)
