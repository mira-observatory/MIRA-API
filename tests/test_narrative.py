from __future__ import annotations

import pytest

from mira_api.llm.client import Completion
from mira_api.nlq.narrative import MAX_NARRATIVE_ATTEMPTS, generate_narrative


def _completion(text: str) -> Completion:
    return Completion(
        text=text, input_tokens=50, output_tokens=15, cache_read_tokens=0, cache_creation_tokens=0
    )


class _ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete_text(
        self, *, model: str, system: list, messages: list, max_tokens: int
    ) -> Completion:
        self.calls.append([dict(m) for m in messages])
        return _completion(self._responses.pop(0))


ROWS = [{"process_id": "p1", "awarded_amount": 7992}]


@pytest.mark.asyncio
async def test_narrativa_valida_pasa_en_el_primer_intento() -> None:
    client = _ScriptedClient(["Se encontro un proceso adjudicado por 7992."])

    result = await generate_narrative(
        client,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        question="cuanto se adjudico",
        rows=ROWS,
        row_count=1,
        truncated=False,
    )

    assert result.verified is True
    assert result.text == "Se encontro un proceso adjudicado por 7992."
    assert result.unverified_numbers == []
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_numero_inventado_se_reintenta_con_retroalimentacion() -> None:
    client = _ScriptedClient(
        [
            "Se adjudicaron 50000 en total.",  # inventado
            "Se adjudico un proceso por 7992.",  # corregido
        ]
    )

    result = await generate_narrative(
        client,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        question="cuanto se adjudico",
        rows=ROWS,
        row_count=1,
        truncated=False,
    )

    assert result.verified is True
    assert result.text == "Se adjudico un proceso por 7992."
    assert len(client.calls) == 2
    # El segundo mensaje debe traer el numero invalido como retroalimentacion.
    second_call = client.calls[1]
    contents = [str(m.get("content", "")) for m in second_call]
    assert any("50000" in c or "50.000" in c for c in contents)


@pytest.mark.asyncio
async def test_dos_fallos_caen_a_la_plantilla_determinista() -> None:
    client = _ScriptedClient(["Se adjudicaron 50000."] * MAX_NARRATIVE_ATTEMPTS)

    result = await generate_narrative(
        client,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        question="cuanto se adjudico",
        rows=ROWS,
        row_count=1,
        truncated=False,
    )

    assert result.verified is False
    assert result.text is not None
    assert "1" in result.text  # la plantilla menciona el numero de filas
    assert result.unverified_numbers != []
    assert len(client.calls) == MAX_NARRATIVE_ATTEMPTS


@pytest.mark.asyncio
async def test_una_redaccion_vacia_cae_a_la_plantilla() -> None:
    """Un texto vacio pasaria el verificador sin objeciones (no hay numeros
    que revisar) y llegaria al usuario como una respuesta en blanco."""
    client = _ScriptedClient(["   "])

    result = await generate_narrative(
        client,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        question="cuanto se adjudico",
        rows=ROWS,
        row_count=1,
        truncated=False,
    )

    assert result.text
    assert result.verified is False


@pytest.mark.asyncio
async def test_puede_citar_el_numero_de_filas() -> None:
    """Caso reportado: "top 10 ..." devolvia tabla sin texto porque el 10 no
    estaba en ninguna celda."""
    client = _ScriptedClient(["Estas son las 3 adjudicaciones mas caras."])

    result = await generate_narrative(
        client,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        question="top 3 adjudicaciones mas caras",
        rows=[{"awarded_amount": 7992}],
        row_count=3,
        truncated=False,
    )

    assert result.verified is True
    assert result.text == "Estas son las 3 adjudicaciones mas caras."


@pytest.mark.asyncio
async def test_cero_filas_no_llama_al_modelo() -> None:
    client = _ScriptedClient([])  # si se llamara, pop(0) fallaria

    result = await generate_narrative(
        client,  # type: ignore[arg-type]
        model="claude-haiku-4-5-20251001",
        question="procesos en un pais sin datos",
        rows=[],
        row_count=0,
        truncated=False,
    )

    assert result.verified is True
    assert "No se encontraron" in (result.text or "")
    assert len(client.calls) == 0
