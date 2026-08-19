from __future__ import annotations

from dataclasses import dataclass

from mira_api.db.log_executor import LogExecutor
from mira_api.quota.counters import CounterState, read_counter, record_spend

#: Sujeto fijo para el presupuesto global -- no depende de quien pregunta.
#: Siempre activo (a diferencia de la cuota por sujeto en quota/subject.py).
GLOBAL_SUBJECT_KEY = "GLOBAL"


@dataclass(frozen=True)
class BudgetStatus:
    blocked: bool
    daily_spent_usd: float
    monthly_spent_usd: float
    daily_limit_usd: float
    monthly_limit_usd: float
    reason: str | None = None


async def check_budget(
    executor: LogExecutor, *, daily_limit_usd: float, monthly_limit_usd: float
) -> BudgetStatus:
    """Se llama ANTES de invocar al modelo (T5.3): la cuota se consume antes de
    gastar, no despues. Bloquea consultas nuevas al 100% del limite diario o
    mensual -- no hay modo "solo cache" todavia (Hito 6 no existe), asi que el
    100% es un bloqueo total en vez de una degradacion a cache."""
    daily = await read_counter(executor, subject_key=GLOBAL_SUBJECT_KEY, period_type="DAY")
    if daily.spent_usd >= daily_limit_usd:
        return BudgetStatus(
            blocked=True,
            daily_spent_usd=daily.spent_usd,
            monthly_spent_usd=0.0,
            daily_limit_usd=daily_limit_usd,
            monthly_limit_usd=monthly_limit_usd,
            reason="daily_budget_exhausted",
        )

    monthly = await read_counter(executor, subject_key=GLOBAL_SUBJECT_KEY, period_type="MONTH")
    if monthly.spent_usd >= monthly_limit_usd:
        return BudgetStatus(
            blocked=True,
            daily_spent_usd=daily.spent_usd,
            monthly_spent_usd=monthly.spent_usd,
            daily_limit_usd=daily_limit_usd,
            monthly_limit_usd=monthly_limit_usd,
            reason="monthly_budget_exhausted",
        )

    return BudgetStatus(
        blocked=False,
        daily_spent_usd=daily.spent_usd,
        monthly_spent_usd=monthly.spent_usd,
        daily_limit_usd=daily_limit_usd,
        monthly_limit_usd=monthly_limit_usd,
    )


async def record_global_spend(
    executor: LogExecutor, *, cost_usd: float
) -> tuple[CounterState, CounterState]:
    """Registra el costo real (conocido solo despues de la llamada) en ambos
    periodos. No es lo que se revisa ANTES de llamar -- eso es check_budget()
    contra lo ya acumulado; esto es lo que deja acumulado para la proxima vez."""
    daily = await record_spend(
        executor, subject_key=GLOBAL_SUBJECT_KEY, period_type="DAY", cost_usd=cost_usd
    )
    monthly = await record_spend(
        executor, subject_key=GLOBAL_SUBJECT_KEY, period_type="MONTH", cost_usd=cost_usd
    )
    return daily, monthly
