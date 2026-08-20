from __future__ import annotations

from typing import Any

import pytest

from mira_api.audit.outcomes import Outcome
from mira_api.llm.client import Completion
from mira_api.nlq.sql_generation import (
    GenerationFailed,
    OutOfScope,
    PriorTurn,
    _strip_markdown_fence,
    build_system_blocks,
    generate_validated_sql,
)

MAX_ROWS = 500
MAX_ATTEMPTS = 3


def _completion(text: str) -> Completion:
    # Tokens ficticios pero no-cero: si algun bug deja de sumar el uso, una
    # prueba que revise usage.input_tokens > 0 lo detecta.
    return Completion(
        text=text, input_tokens=100, output_tokens=20, cache_read_tokens=0, cache_creation_tokens=0
    )


class _ScriptedClient:
    """Reemplaza ClaudeClient sin llamar a la API real. Devuelve las
    respuestas en orden y graba los mensajes de cada llamada, para poder
    verificar que la retroalimentacion del validador viaja al modelo."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete_text(
        self,
        *,
        model: str,
        system: list[dict[str, object]],
        messages: list[dict[str, object]],
        max_tokens: int,
    ) -> Completion:
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            raise AssertionError("se agotaron las respuestas guionadas")
        return _completion(self._responses.pop(0))


def test_strip_markdown_fence() -> None:
    assert _strip_markdown_fence("```sql\nselect 1\n```") == "select 1"
    assert _strip_markdown_fence("```\nselect 1\n```") == "select 1"
    assert _strip_markdown_fence("select 1") == "select 1"


def test_build_system_blocks_marca_cache_control() -> None:
    blocks = build_system_blocks([])
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_acepta_sql_valido_en_el_primer_intento() -> None:
    client = _ScriptedClient(
        ["select process_id from query.v_process where country_code = 'CR'"]
    )

    result = await generate_validated_sql(
        client,  # type: ignore[arg-type]
        model="claude-sonnet-5",
        system=[],
        question="dame procesos",
        countries=["CR"],
        max_rows=MAX_ROWS,
    )

    assert len(result.attempts) == 1
    assert result.attempts[0].accepted
    assert "query.v_process" in result.validated.relations
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_reintenta_con_la_retroalimentacion_del_validador() -> None:
    client = _ScriptedClient(
        [
            "select * from mart.processes",  # rechazado: esquema privado
            "select process_id from query.v_process where country_code = 'CR'",  # aceptado
        ]
    )

    result = await generate_validated_sql(
        client,  # type: ignore[arg-type]
        model="claude-sonnet-5",
        system=[],
        question="dame procesos",
        countries=["CR"],
        max_rows=MAX_ROWS,
    )

    assert len(result.attempts) == 2
    assert not result.attempts[0].accepted
    assert result.attempts[0].rejection_rule is not None
    assert result.attempts[1].accepted

    # El segundo intento debe llevar el SQL rechazado y el motivo como contexto.
    second_call_messages = client.calls[1]
    assert any("rechazado" in str(m.get("content", "")) for m in second_call_messages)
    assert any("mart.processes" in str(m.get("content", "")) for m in second_call_messages)


@pytest.mark.asyncio
async def test_falla_despues_de_agotar_los_intentos() -> None:
    client = _ScriptedClient(["select * from mart.processes"] * MAX_ATTEMPTS)

    with pytest.raises(GenerationFailed) as err:
        await generate_validated_sql(
            client,  # type: ignore[arg-type]
            model="claude-sonnet-5",
            system=[],
            question="dame lo que sea",
            countries=["CR"],
            max_rows=MAX_ROWS,
        )

    assert err.value.outcome is Outcome.REJECTED_SQL_RELATION
    # El uso se acumula por los 3 intentos, no solo el ultimo -- el presupuesto
    # tiene que ver lo que realmente se gasto.
    assert err.value.usage.input_tokens == 100 * MAX_ATTEMPTS
    assert len(client.calls) == MAX_ATTEMPTS
    # Hito 4: cada intento (con su rechazo) viaja en la excepcion para poder
    # escribir analytics.query_attempt aunque la generacion falle del todo.
    assert len(err.value.attempts) == MAX_ATTEMPTS
    assert all(a.outcome is Outcome.REJECTED_SQL_RELATION for a in err.value.attempts)


@pytest.mark.asyncio
async def test_out_of_scope_no_reintenta() -> None:
    client = _ScriptedClient(["OUT_OF_SCOPE"])

    with pytest.raises(OutOfScope) as err:
        await generate_validated_sql(
            client,  # type: ignore[arg-type]
            model="claude-sonnet-5",
            system=[],
            question="cual es la capital de Francia",
            countries=["CR"],
            max_rows=MAX_ROWS,
        )

    assert len(client.calls) == 1
    assert len(err.value.attempts) == 1
    assert err.value.attempts[0].outcome is Outcome.OUT_OF_SCOPE


# --- Memoria conversacional ---------------------------------------------------


HISTORIAL = [
    PriorTurn(
        question="cuantos procesos hay en Costa Rica",
        countries=["CR"],
        sql="SELECT COUNT(*) FROM query.v_process WHERE country_code = 'CR'",
    )
]


@pytest.mark.asyncio
async def test_el_historial_viaja_como_pares_pregunta_sql() -> None:
    client = _ScriptedClient(
        ["select count(*) from query.v_process where country_code = 'HN'"]
    )

    await generate_validated_sql(
        client,  # type: ignore[arg-type]
        model="claude-sonnet-5",
        system=[],
        question="y en Honduras?",
        countries=["HN"],
        max_rows=MAX_ROWS,
        history=HISTORIAL,
    )

    mensajes = client.calls[0]
    # user(anterior) -> assistant(SQL anterior) -> user(actual)
    assert [m["role"] for m in mensajes] == ["user", "assistant", "user"]
    assert "cuantos procesos hay en Costa Rica" in str(mensajes[0]["content"])
    assert mensajes[1]["content"] == HISTORIAL[0].sql
    assert "y en Honduras?" in str(mensajes[2]["content"])


@pytest.mark.asyncio
async def test_cada_turno_conserva_sus_propios_paises() -> None:
    """Reescribir los paises del turno anterior con los de ahora dejaria un
    mensaje que dice "Paises: HN" pegado a un SQL que filtra 'CR' -- una
    contradiccion justo en el ejemplo del que el modelo tiene que aprender."""
    client = _ScriptedClient(
        ["select count(*) from query.v_process where country_code = 'HN'"]
    )

    await generate_validated_sql(
        client,  # type: ignore[arg-type]
        model="claude-sonnet-5",
        system=[],
        question="y en Honduras?",
        countries=["HN"],
        max_rows=MAX_ROWS,
        history=HISTORIAL,
    )

    mensajes = client.calls[0]
    assert "Paises: CR" in str(mensajes[0]["content"])
    assert "Paises: HN" in str(mensajes[2]["content"])


@pytest.mark.asyncio
async def test_sin_historial_solo_va_la_pregunta_actual() -> None:
    client = _ScriptedClient(
        ["select process_id from query.v_process where country_code = 'CR'"]
    )

    await generate_validated_sql(
        client,  # type: ignore[arg-type]
        model="claude-sonnet-5",
        system=[],
        question="dame procesos",
        countries=["CR"],
        max_rows=MAX_ROWS,
    )

    assert [m["role"] for m in client.calls[0]] == ["user"]


@pytest.mark.asyncio
async def test_el_historial_sobrevive_a_un_reintento_del_validador() -> None:
    """El reintento agrega mensajes al final; el historial anterior tiene que
    seguir estando, o la correccion se hace sin el contexto que la origino."""
    client = _ScriptedClient(
        [
            "select * from mart.processes",  # rechazado
            "select count(*) from query.v_process where country_code = 'HN'",
        ]
    )

    await generate_validated_sql(
        client,  # type: ignore[arg-type]
        model="claude-sonnet-5",
        system=[],
        question="y en Honduras?",
        countries=["HN"],
        max_rows=MAX_ROWS,
        history=HISTORIAL,
    )

    segunda_llamada = client.calls[1]
    assert "cuantos procesos hay en Costa Rica" in str(segunda_llamada[0]["content"])
    assert segunda_llamada[1]["content"] == HISTORIAL[0].sql
    # y el ultimo mensaje es la retroalimentacion del validador
    assert "rechazado" in str(segunda_llamada[-1]["content"])


@pytest.mark.asyncio
async def test_out_of_scope_tolera_espacios_y_mayusculas() -> None:
    client = _ScriptedClient(["  out_of_scope  "])

    with pytest.raises(OutOfScope):
        await generate_validated_sql(
            client,  # type: ignore[arg-type]
            model="claude-sonnet-5",
            system=[],
            question="cualquier cosa",
            countries=["CR"],
            max_rows=MAX_ROWS,
        )
