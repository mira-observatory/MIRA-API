from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mira_api.llm.client import ClaudeApiError, ClaudeClient, ClaudeRefusal
from mira_api.nlq.number_extraction import find_unverified_numbers
from mira_api.nlq.prompts import NARRATIVE_RETRY_PROMPT, NARRATIVE_SYSTEM_PROMPT
from mira_api.nlq.sql_generation import Usage


@dataclass(frozen=True)
class NarrativeResult:
    text: str | None
    verified: bool
    unverified_numbers: list[str]
    usage: Usage


def _fallback_template(row_count: int, truncated: bool) -> str:
    if row_count == 0:
        return "No se encontraron resultados para esta consulta."
    suffix = " (resultado truncado, hay mas filas de las que se muestran)" if truncated else ""
    return f"La consulta devolvio {row_count} fila(s){suffix}. Ver la tabla para el detalle."


def _build_user_message(
    question: str,
    rows: list[dict[str, Any]],
    row_count: int,
    truncated: bool,
    max_rows_in_prompt: int,
) -> str:
    sample = rows[:max_rows_in_prompt]
    payload = {
        "pregunta": question,
        "filas_totales": row_count,
        "truncado": truncated,
        "filas_mostradas": sample,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


async def generate_narrative(
    client: ClaudeClient,
    *,
    model: str,
    question: str,
    rows: list[dict[str, Any]],
    row_count: int,
    truncated: bool,
    max_attempts: int = 2,
    max_rows_in_prompt: int = 25,
    max_tokens: int = 512,
) -> NarrativeResult:
    """T3.5 (redaccion) + T3.6 (verificador anti-alucinacion). Nunca bloquea
    la respuesta: si el modelo sigue inventando numeros despues del
    reintento, se sirve una plantilla determinista y verified=False -- los
    datos ya se le entregaron al usuario de todas formas.
    """
    usage = Usage()
    if row_count == 0:
        return NarrativeResult(
            text=_fallback_template(row_count, truncated),
            verified=True,
            unverified_numbers=[],
            usage=usage,
        )

    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": _build_user_message(
                question, rows, row_count, truncated, max_rows_in_prompt
            ),
        }
    ]

    for attempt in range(1, max_attempts + 1):
        try:
            completion = await client.complete_text(
                model=model,
                system=[{"type": "text", "text": NARRATIVE_SYSTEM_PROMPT}],
                messages=messages,
                max_tokens=max_tokens,
            )
        except (ClaudeRefusal, ClaudeApiError):
            # La redaccion es prescindible: los datos ya estan. Si la API se
            # cae o se sobrecarga aqui, se sirve la plantilla determinista en
            # vez de tumbar una respuesta que ya tiene su tabla.
            break
        usage = usage + Usage(
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cache_read_tokens=completion.cache_read_tokens,
            cache_creation_tokens=completion.cache_creation_tokens,
        )
        text = completion.text.strip()
        if not text:
            # Una redaccion vacia pasaria el verificador sin objeciones (no
            # tiene numeros que revisar) y llegaria al usuario como una
            # respuesta en blanco. Mejor la plantilla, que al menos dice algo.
            break
        invalid = find_unverified_numbers(text, rows, row_count=row_count)
        if not invalid:
            return NarrativeResult(
                text=text, verified=True, unverified_numbers=[], usage=usage
            )
        if attempt == max_attempts:
            return NarrativeResult(
                text=_fallback_template(row_count, truncated),
                verified=False,
                unverified_numbers=invalid,
                usage=usage,
            )
        messages.append({"role": "assistant", "content": text})
        messages.append(
            {
                "role": "user",
                "content": NARRATIVE_RETRY_PROMPT.format(
                    invalid_numbers=", ".join(invalid)
                ),
            }
        )

    return NarrativeResult(
        text=_fallback_template(row_count, truncated),
        verified=False,
        unverified_numbers=[],
        usage=usage,
    )
