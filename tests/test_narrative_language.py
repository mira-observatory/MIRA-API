"""La respuesta sigue al idioma de la pregunta."""

from __future__ import annotations

import pytest

from mira_api.api.schemas import Warning
from mira_api.nlq.narrative import _fallback_template, generate_narrative
from mira_api.nlq.pipeline import _warning_text
from mira_api.nlq.prompts import NARRATIVE_LANGUAGE_NAMES, NARRATIVE_SYSTEM_PROMPT

# --- plantillas deterministas -----------------------------------------------


def test_la_plantilla_de_respaldo_existe_en_ingles() -> None:
    """Es la que se sirve cuando el modelo falla o alucina. Si solo existiera
    en espanol, una pregunta en ingles recibiria la explicacion del fallo en
    otro idioma -- justo cuando la persona menos entiende que paso."""
    assert _fallback_template(0, False, "en") == "No results were found for this query."
    assert "returned 5 row(s)" in _fallback_template(5, False, "en")
    assert "truncated" in _fallback_template(500, True, "en")


def test_el_espanol_sigue_siendo_el_default() -> None:
    assert "No se encontraron resultados" in _fallback_template(0, False)
    assert "No se encontraron resultados" in _fallback_template(0, False, "es")


def test_un_idioma_desconocido_cae_al_espanol() -> None:
    """Un codigo que no manejamos no puede dejar la respuesta en blanco."""
    assert "No se encontraron resultados" in _fallback_template(0, False, "fr")  # type: ignore[arg-type]


# --- prompt -----------------------------------------------------------------


def test_el_prompt_le_dice_al_modelo_en_que_idioma_escribir() -> None:
    en = NARRATIVE_SYSTEM_PROMPT.format(idioma=NARRATIVE_LANGUAGE_NAMES["en"])
    es = NARRATIVE_SYSTEM_PROMPT.format(idioma=NARRATIVE_LANGUAGE_NAMES["es"])

    assert "in English" in en
    assert "en espanol" in es


def test_el_prompt_prohibe_traducir_los_datos() -> None:
    """Los nombres de empresas e instituciones son el dato oficial: traducirlos
    inventaria una entidad que no existe en ningun registro."""
    texto = NARRATIVE_SYSTEM_PROMPT.format(idioma=NARRATIVE_LANGUAGE_NAMES["en"])

    assert "NO se traducen" in texto


# --- avisos -----------------------------------------------------------------


def _aviso(**kwargs: object) -> Warning:
    base = {"code": "PARTIAL_COVERAGE", "message_es": "vacio en espanol"}
    return Warning(**{**base, **kwargs})  # type: ignore[arg-type]


def test_el_aviso_se_sirve_en_ingles_cuando_hay_traduccion() -> None:
    aviso = _aviso(message_en="empty in English")

    assert _warning_text(aviso, "en") == "empty in English"
    assert _warning_text(aviso, "es") == "vacio en espanol"


def test_sin_traduccion_el_aviso_cae_al_espanol_en_vez_de_quedar_vacio() -> None:
    """Un aviso sin `message_en` tiene que mostrarse igual: con cero filas el
    aviso ES la respuesta, y dejarlo en blanco seria peor que el idioma
    equivocado."""
    assert _warning_text(_aviso(), "en") == "vacio en espanol"


# --- integracion ------------------------------------------------------------


@pytest.mark.asyncio
async def test_cero_filas_en_ingles_no_llama_al_modelo_y_responde_en_ingles() -> None:
    """Con cero filas se sirve la explicacion exacta sin gastar una llamada."""
    resultado = await generate_narrative(
        client=None,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        question="how many awards are there in Nicaragua",
        rows=[],
        row_count=0,
        truncated=False,
        language="en",
    )

    assert resultado.text == "No results were found for this query."
    assert resultado.verified
    assert resultado.usage.input_tokens == 0
