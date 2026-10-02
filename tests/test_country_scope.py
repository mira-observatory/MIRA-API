from __future__ import annotations

import pytest

from mira_api.nlq.country_scope import resolve_country_scope

AVAILABLE = ["GT", "HN", "CR", "NI"]


@pytest.mark.parametrize(("question", "expected"), [
    ("Compara Guatemala y Costa Rica durante el primer trimestre de 2025", ["GT", "CR"]),
    ("Compare Guatemala and Costa Rica by publication month", ["GT", "CR"]),
    ("Compara COSTA   RICA con guatemala", ["GT", "CR"]),
    ("¿Y en Honduras?", ["HN"]),
    ("Contratos publicados en Nicaragua", ["NI"]),
    ("Compara todos los países, incluyendo Guatemala y Costa Rica", AVAILABLE),
    ("Compare all countries, including Guatemala", AVAILABLE),
    ("Compras en Centroamérica, incluyendo Costa Rica", AVAILABLE),
    ("Muestra procesos recientes", AVAILABLE),
    ("Muestra compras sin contratos ni adjudicaciones", AVAILABLE),
    ("Contratos de Guatemalavision", AVAILABLE),
])
def test_resolves_countries_named_in_question(question: str, expected: list[str]) -> None:
    assert resolve_country_scope(question, AVAILABLE) == expected


def test_normalises_selection_and_country_accents() -> None:
    assert resolve_country_scope("Compara Panamá y El Salvador", ["pa", "sv", "pa"]) == ["PA", "SV"]


def test_does_not_silently_drop_an_unavailable_named_country() -> None:
    assert resolve_country_scope("Compara Guatemala y Costa Rica", ["GT"]) == ["GT"]


@pytest.mark.parametrize(("question", "expected"), [
    ("¿Y en 2024?", ["GT", "CR"]),
    ("¿Y en Honduras?", ["HN"]),
    ("Ahora compara todos los países", AVAILABLE),
])
def test_follow_up_can_inherit_change_or_expand_scope(question: str, expected: list[str]) -> None:
    assert resolve_country_scope(question, AVAILABLE, previous=["GT", "CR"]) == expected


def test_new_selection_takes_precedence_over_unavailable_previous_scope() -> None:
    assert resolve_country_scope("¿Y en 2024?", ["HN"], previous=["GT", "CR"]) == ["HN"]
