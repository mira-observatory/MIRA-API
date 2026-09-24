from __future__ import annotations

import pytest

from mira_api.nlq.ranking import asks_single_winner, asks_supplier_total
from mira_api.nlq.sql_generation import generate_validated_sql
from mira_api.nlq.validator import SqlRejected, validate
from tests.test_sql_generation import _ScriptedClient

QUESTION = "Cual es el proveedor que mas dinero ha ganado en Guatemala?"
TOTALS = (
    "SELECT DISTINCT ON (currency_code) name_normalised, total_awarded_amount, "
    "currency_code, award_count, shared_award_count, refreshed_at "
    "FROM query.v_supplier_award_totals WHERE country_code = 'GT' "
    "ORDER BY currency_code, total_awarded_amount DESC, supplier_id LIMIT 100"
)
INDIVIDUAL = (
    "SELECT a.awarded_amount, a.currency_code FROM query.v_awards a "
    "JOIN query.v_process p USING (process_id) WHERE p.country_code = 'GT' "
    "ORDER BY a.awarded_amount DESC NULLS LAST LIMIT 1"
)


@pytest.mark.parametrize("question", [
    QUESTION, "Which supplier earned the most money in Guatemala?",
])
def test_default_is_cumulative(question: str) -> None:
    assert asks_supplier_total(question)
    assert asks_single_winner(question)


@pytest.mark.parametrize("suffix", [
    " en una adjudicacion", " en un contrato", " in a single award",
])
def test_individual_is_explicit(suffix: str) -> None:
    assert not asks_supplier_total(QUESTION + suffix)


def test_rejects_individual_answer_for_cumulative_question() -> None:
    with pytest.raises(SqlRejected, match="supplier_total_required"):
        validate(INDIVIDUAL, max_rows=500, countries=["GT"], question=QUESTION)


@pytest.mark.asyncio
async def test_retries_wrong_limit_for_individual_winner() -> None:
    client = _ScriptedClient([INDIVIDUAL.replace("LIMIT 1", "LIMIT 100"), INDIVIDUAL])
    result = await generate_validated_sql(
        client, model="test", system=[], question=QUESTION + " en una adjudicacion",
        countries=["GT"], max_rows=500,
    )
    assert result.attempts[0].rejection_rule == "single_winner_limit"
    assert result.validated.effective_limit == 1


def test_keeps_all_currencies_even_if_model_requests_one_row() -> None:
    result = validate(TOTALS.replace("LIMIT 100", "LIMIT 1"), max_rows=500,
                      countries=["GT"], question=QUESTION)
    assert result.effective_limit == 500
    assert "DISTINCT ON" in result.sql


def test_allows_requested_currency() -> None:
    sql = TOTALS.replace("DISTINCT ON (currency_code) ", "").replace(
        "WHERE country_code = 'GT'", "WHERE country_code = 'GT' AND currency_code = 'GTQ'",
    ).replace("LIMIT 100", "LIMIT 1")
    assert validate(sql, max_rows=500, countries=["GT"], question=QUESTION + " en GTQ")
    with pytest.raises(SqlRejected, match="currency_not_requested"):
        validate(sql, max_rows=500, countries=["GT"], question=QUESTION)


@pytest.mark.parametrize("sql", [
    TOTALS.replace("DISTINCT ON (currency_code) ", ""),
    TOTALS.replace("DISTINCT ON (currency_code) ", "").replace(
        "WHERE country_code = 'GT'",
        "WHERE country_code = 'GT' AND (currency_code = 'GTQ' OR award_count > 0)",
    ),
    TOTALS.replace("total_awarded_amount, currency_code",
                   "SUM(total_awarded_amount), currency_code"),
    TOTALS.replace("total_awarded_amount, currency_code",
                   "total_awarded_amount / 8, currency_code"),
    TOTALS.replace("total_awarded_amount DESC", "total_awarded_amount ASC"),
    TOTALS.replace("FROM query.v_supplier_award_totals", "FROM query.v_supplier_award_totals t "
                   "JOIN query.v_award_suppliers s ON s.supplier_id=t.supplier_id"),
])
def test_blocks_cross_currency_comparison_and_resumming(sql: str) -> None:
    with pytest.raises(SqlRejected):
        validate(sql, max_rows=500, countries=["GT"], question=QUESTION)


def test_does_not_replace_requested_period_with_all_history() -> None:
    with pytest.raises(SqlRejected, match="supplier_totals_period"):
        validate(TOTALS, max_rows=500, countries=["GT"], question=QUESTION + " en 2025")
