from __future__ import annotations

import pytest

from mira_api.nlq.ranking import requested_row_limit


@pytest.mark.parametrize("question", [
    "En Honduras durante 2023, muestra las 5 instituciones con más procesos "
    "publicados que utilizaron al menos 5 modalidades de contratación distintas.",
    "Top 5 instituciones por cantidad de procesos",
    "top-5 proveedores de Guatemala",
    "Muestra las cinco instituciones con más procesos",
    "Dame 5 procesos recientes",
    "Mostrá solo 5 resultados",
    "Los primeros 5 contratos por monto",
    "Las 5 más caras de 2023",
    "Show the 5 institutions with the most processes",
    "Show only five results",
    "The five highest awards",
    "First 5 suppliers by amount",
    "Muestra los primeros 5",
])
def test_detects_explicit_result_size(question: str) -> None:
    assert requested_row_limit(question) == 5


@pytest.mark.parametrize("question", [
    "Instituciones con al menos 5 modalidades de contratación en 2023",
    "Muestra instituciones con más de 5 procesos",
    "Instituciones que utilizaron las 5 modalidades de contratación",
    "Contratos publicados en los 5 meses anteriores",
    "Instituciones con más procesos en los primeros 5 meses de 2023",
    "Institutions with the most processes in the first 5 months of 2023",
    "Institutions with at least 5 distinct procurement methods in 2023",
    "Top institutions by process count",
    "Muestra 0 filas",
])
def test_does_not_confuse_filters_with_result_size(question: str) -> None:
    assert requested_row_limit(question) is None
