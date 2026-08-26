"""Deteccion de idioma de la pregunta.

El sesgo hacia el espanol es intencional: equivocarse hacia el ingles con un
usuario centroamericano es peor que lo contrario.
"""

from __future__ import annotations

import pytest

from mira_api.nlq.language import detect_language


@pytest.mark.parametrize(
    "pregunta",
    [
        "cuantos procesos hay en Costa Rica",
        "cuales son las 10 adjudicaciones mas caras",
        "que empresas recibieron mas contratos en Honduras",
        "muestrame las compras de medicamentos",
        "dame el producto mas vendido",
        "cuanto se gasto en equipo de computo en 2024",
        # Sin un solo acento y aun asi inequivocamente espanola.
        "que instituciones hicieron mas compras por adjudicacion directa",
    ],
)
def test_preguntas_en_espanol(pregunta: str) -> None:
    assert detect_language(pregunta) == "es"


@pytest.mark.parametrize(
    "pregunta",
    [
        "how many processes are there in Costa Rica",
        "what are the 10 most expensive awards",
        "which companies received the most contracts in Honduras",
        "show me the medicine purchases",
        "give me the most sold product",
        "how much was spent on computer equipment in 2024",
        "top 10 suppliers by amount awarded",
    ],
)
def test_preguntas_en_ingles(pregunta: str) -> None:
    assert detect_language(pregunta) == "en"


def test_un_acento_basta_para_decidir_espanol() -> None:
    """El ingles no lleva acentos, asi que uno solo ya es prueba. Su ausencia
    no prueba lo contrario, por eso el conteo de palabras existe."""
    assert detect_language("¿cuánto?") == "es"


def test_ante_la_duda_gana_el_espanol() -> None:
    """Preguntas que son casi puro nombre propio o numero no tienen senal de
    idioma. En un observatorio centroamericano el default correcto es espanol."""
    assert detect_language("Costa Rica 2026") == "es"
    assert detect_language("total") == "es"
    assert detect_language("") == "es"
    assert detect_language("12345") == "es"


def test_nombres_propios_no_arrastran_al_ingles() -> None:
    """"Ministerio de Salud" no es una pregunta en ingles por tener palabras
    que un detector ingenuo podria confundir."""
    assert detect_language("contratos del Ministerio de Salud") == "es"
