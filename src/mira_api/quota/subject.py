from __future__ import annotations

from dataclasses import dataclass

from mira_api.db.log_executor import LogExecutor
from mira_api.quota.counters import read_counter, record_spend


@dataclass(frozen=True)
class SubjectQuotaStatus:
    blocked: bool
    day_count: int
    month_count: int
    day_limit: int
    month_limit: int
    reason: str | None = None


async def check_subject_quota(
    executor: LogExecutor, *, subject_key: str, day_limit: int, month_limit: int
) -> SubjectQuotaStatus:
    """Cuota por sujeto (T5.2/T5.3) -- escrita y probada, pero nadie la llama
    desde el pipeline todavia: `config.enable_subject_quota` esta en False por
    decision de producto (2026-08-18). Activarla es agregar el `if` en
    nlq/pipeline.py, no reescribir esto.
    """
    day = await read_counter(executor, subject_key=subject_key, period_type="DAY")
    if day.query_count >= day_limit:
        return SubjectQuotaStatus(
            blocked=True,
            day_count=day.query_count,
            month_count=0,
            day_limit=day_limit,
            month_limit=month_limit,
            reason="daily_quota_exhausted",
        )

    month = await read_counter(executor, subject_key=subject_key, period_type="MONTH")
    if month.query_count >= month_limit:
        return SubjectQuotaStatus(
            blocked=True,
            day_count=day.query_count,
            month_count=month.query_count,
            day_limit=day_limit,
            month_limit=month_limit,
            reason="monthly_quota_exhausted",
        )

    return SubjectQuotaStatus(
        blocked=False,
        day_count=day.query_count,
        month_count=month.query_count,
        day_limit=day_limit,
        month_limit=month_limit,
    )


async def record_subject_usage(executor: LogExecutor, *, subject_key: str, cost_usd: float) -> None:
    await record_spend(executor, subject_key=subject_key, period_type="DAY", cost_usd=cost_usd)
    await record_spend(executor, subject_key=subject_key, period_type="MONTH", cost_usd=cost_usd)
