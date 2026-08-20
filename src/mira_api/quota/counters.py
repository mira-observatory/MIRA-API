from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from mira_api.db.log_executor import LogExecutor

PeriodType = Literal["DAY", "MONTH"]

#: UPSERT atomico: ON CONFLICT ... DO UPDATE con una expresion que suma sobre el
#: valor ya guardado (no un read-modify-write en Python) es lo que hace que N
#: peticiones concurrentes produzcan exactamente N incrementos -- Postgres
#: serializa el UPDATE por fila, no hace falta un lock explicito aparte.
_UPSERT_SQL = """
    insert into analytics.quota_counters
        (subject_key, period_type, period_key, query_count, spent_usd, updated_at)
    values (%(subject_key)s, %(period_type)s, %(period_key)s, 1, %(cost_usd)s, now())
    on conflict (subject_key, period_type, period_key) do update set
        query_count = quota_counters.query_count + 1,
        spent_usd = quota_counters.spent_usd + excluded.spent_usd,
        updated_at = now()
    returning query_count, spent_usd
"""

_SELECT_SQL = """
    select query_count, spent_usd
    from analytics.quota_counters
    where subject_key = %(subject_key)s
      and period_type = %(period_type)s
      and period_key = %(period_key)s
"""


@dataclass(frozen=True)
class CounterState:
    query_count: int
    spent_usd: float


def period_key(period_type: PeriodType, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    if period_type == "DAY":
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m")


async def read_counter(
    executor: LogExecutor, *, subject_key: str, period_type: PeriodType, now: datetime | None = None
) -> CounterState:
    row = await executor.fetch_one(
        _SELECT_SQL,
        {
            "subject_key": subject_key,
            "period_type": period_type,
            "period_key": period_key(period_type, now=now),
        },
    )
    if row is None:
        return CounterState(query_count=0, spent_usd=0.0)
    return CounterState(query_count=row["query_count"], spent_usd=float(row["spent_usd"]))


async def record_spend(
    executor: LogExecutor,
    *,
    subject_key: str,
    period_type: PeriodType,
    cost_usd: float,
    now: datetime | None = None,
) -> CounterState:
    row = await executor.fetch_one(
        _UPSERT_SQL,
        {
            "subject_key": subject_key,
            "period_type": period_type,
            "period_key": period_key(period_type, now=now),
            "cost_usd": cost_usd,
        },
    )
    assert row is not None
    return CounterState(query_count=row["query_count"], spent_usd=float(row["spent_usd"]))
