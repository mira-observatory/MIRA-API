from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mira_api.llm.client import ClaudeClient, ClaudeRefusal
from mira_api.nlq.number_extraction import find_unverified_numbers
from mira_api.nlq.sql_generation import Usage

#: Intento inicial + 1 reintento (Parte 1.7): si el segundo tambien alucina un
#: numero, se sirve la plantilla determinista y se marca narrative_verified=False.
MAX_NARRATIVE_ATTEMPTS = 2

#: Cuantas filas del resultado se le mandan al modelo -- el resto de la tabla
#: ya se le entrego al usuario en `rows`, la redaccion no necesita verlo todo.
_MAX_ROWS_IN_PROMPT = 25

_SYSTEM_PROMPT = """\
Redactas un resumen breve en espanol (2 a 4 frases) del resultado de una \
consulta sobre contrataciones publicas de Centroamerica, para un ciudadano \
que no sabe SQL.

Reglas estrictas:
1. No calcules. No estimes. No sumes. No promedies. Usa UNICAMENTE los \
numeros que ya estan en la tabla, tal como estan.
2. Si la pregunta pide un total que no aparece como una celda de la tabla, \
di explicitamente que ese dato no esta disponible -- nunca lo inventes ni lo \
calcules a mano.
3. Si la tabla esta truncada (no muestra todas las filas), acláralo en vez \
de hablar como si fuera el total completo.
4. Nunca mezcles montos de monedas distintas como si fueran un solo total.
5. Responde solo con el resumen, sin titulos ni markdown."""


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
    question: str, rows: list[dict[str, Any]], row_count: int, truncated: bool
) -> str:
    sample = rows[:_MAX_ROWS_IN_PROMPT]
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
        {"role": "user", "content": _build_user_message(question, rows, row_count, truncated)}
    ]

    for attempt in range(1, MAX_NARRATIVE_ATTEMPTS + 1):
        try:
            completion = await client.complete_text(
                model=model,
                system=[{"type": "text", "text": _SYSTEM_PROMPT}],
                messages=messages,
                max_tokens=max_tokens,
            )
        except ClaudeRefusal:
            break
        usage = usage + Usage(
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cache_read_tokens=completion.cache_read_tokens,
            cache_creation_tokens=completion.cache_creation_tokens,
        )
        text = completion.text.strip()
        invalid = find_unverified_numbers(text, rows)
        if not invalid:
            return NarrativeResult(
                text=text, verified=True, unverified_numbers=[], usage=usage
            )
        if attempt == MAX_NARRATIVE_ATTEMPTS:
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
                "content": (
                    f"Estos numeros que escribiste no aparecen en la tabla: "
                    f"{', '.join(invalid)}. Reescribe el resumen usando solo "
                    "numeros que esten literalmente en los datos."
                ),
            }
        )

    return NarrativeResult(
        text=_fallback_template(row_count, truncated),
        verified=False,
        unverified_numbers=[],
        usage=usage,
    )
