from __future__ import annotations

from mira_api.quota.pricing import compute_cost_usd


def test_calcula_costo_a_precio_pleno_de_sonnet_5() -> None:
    # $3/$15 por millon de tokens -- precio pleno, no el promocional
    # (Parte 1.11 del plan: "Presupuestar a precio pleno").
    cost = compute_cost_usd(
        "claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost == 3.00 + 15.00


def test_cache_read_es_mas_barato_que_input_normal() -> None:
    normal = compute_cost_usd("claude-sonnet-5", input_tokens=1000, output_tokens=0)
    cached = compute_cost_usd(
        "claude-sonnet-5", input_tokens=0, output_tokens=0, cache_read_tokens=1000
    )
    assert 0 < cached < normal


def test_modelo_desconocido_no_queda_sin_costo() -> None:
    # Mejor sobreestimar (cae al precio de Sonnet 5) que dejar un modelo nuevo
    # sin contar en el presupuesto.
    cost = compute_cost_usd("un-modelo-que-no-existe", input_tokens=1000, output_tokens=1000)
    assert cost > 0


def test_sin_tokens_el_costo_es_cero() -> None:
    assert compute_cost_usd("claude-sonnet-5", input_tokens=0, output_tokens=0) == 0.0
