from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    #: USD por millon de tokens, precio de lista completo (no promocional --
    #: el precio de Sonnet 5 baja hasta 2026-08-31, presupuestar a precio pleno).
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


#: Nombres exactos que usa este servicio (config.sql_model / config.model_fast).
#: Si se agrega un modelo nuevo y no esta aqui, PRICING.get() cae al de Sonnet 5
#: como aproximacion conservadora -- mejor sobreestimar el gasto que dejarlo sin
#: contar.
_PRICING: dict[str, ModelPricing] = {
    "claude-sonnet-5": ModelPricing(3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5-20251001": ModelPricing(1.00, 5.00, 0.10, 1.25),
    "claude-haiku-4-5": ModelPricing(1.00, 5.00, 0.10, 1.25),
    "claude-opus-5": ModelPricing(5.00, 25.00, 0.50, 6.25),
}

_FALLBACK = _PRICING["claude-sonnet-5"]


def compute_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    pricing = _PRICING.get(model, _FALLBACK)
    return (
        input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
        + cache_read_tokens * pricing.cache_read_per_mtok
        + cache_creation_tokens * pricing.cache_write_per_mtok
    ) / 1_000_000
