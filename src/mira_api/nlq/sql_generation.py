from __future__ import annotations

from dataclasses import dataclass

from mira_api.llm.client import ClaudeClient
from mira_api.nlq.semantic_dictionary import ColumnDoc, format_for_prompt
from mira_api.nlq.validator import SqlRejected, ValidatedSql, validate

#: Intento inicial + 2 reintentos. Dos fallos de validador seguidos significan
#: que la pregunta no es expresable contra el esquema -- un tercer intento no
#: cambia eso. Ver Parte 1.8 del plan de arquitectura.
MAX_ATTEMPTS = 3

#: Sentinela que el modelo devuelve cuando la pregunta no se puede responder
#: con las columnas disponibles. No pasa por el validador -- se detecta antes.
OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"

_SYSTEM_INSTRUCTIONS = """\
Eres el traductor de preguntas en espanol a SQL de PostgreSQL de solo lectura \
para MIRA, un observatorio de contrataciones publicas de Centroamerica \
(Costa Rica, Guatemala, Honduras, Nicaragua).

Reglas estrictas, sin excepcion:
1. Responde UNICAMENTE con la sentencia SQL -- sin explicaciones, sin \
markdown, sin comentarios, sin punto y coma final.
2. Solo SELECT. Nunca escribas INSERT/UPDATE/DELETE/DROP/ALTER ni ninguna \
otra sentencia.
3. Solo puedes usar las vistas listadas abajo, siempre con el prefijo \
"query." exacto (por ejemplo query.v_process). Nunca inventes una columna o \
vista que no este en la lista.
4. Cuando la pregunta menciona o implica uno o mas paises, filtra siempre \
por country_code. Los codigos validos son CR, GT, HN, NI.
5. Nunca sumes columnas de dinero (estimated_amount, awarded_amount) de \
distinta moneda sin agrupar antes por currency_code.
6. El monto adjudicado vive en query.v_awards, no en query.v_process. Para \
"cuanto se gasto" siempre hace falta unir query.v_process con query.v_awards \
(y query.v_award_suppliers si se pregunta por un proveedor especifico).
7. Si la pregunta no se puede responder con las columnas disponibles, \
responde exactamente con este texto y nada mas: OUT_OF_SCOPE

Columnas disponibles:
{dictionary}
"""


class OutOfScope(Exception):
    """El modelo determino que la pregunta no es respondible con el esquema
    disponible. No es un error -- es el sistema funcionando."""


@dataclass(frozen=True)
class GenerationAttempt:
    attempt_no: int
    sql_text: str
    accepted: bool
    rejection_rule: str | None = None
    rejection_detail: str | None = None


@dataclass(frozen=True)
class GenerationResult:
    validated: ValidatedSql
    attempts: list[GenerationAttempt]


def build_system_blocks(columns: list[ColumnDoc]) -> list[dict[str, object]]:
    """El diccionario semantico rara vez cambia -- es el contenido estable que
    se marca para cacheo. La pregunta y los paises van en `messages`, siempre
    despues del ultimo cache_control, para no invalidar el cache en cada
    llamada distinta."""
    text = _SYSTEM_INSTRUCTIONS.format(dictionary=format_for_prompt(columns))
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


async def generate_validated_sql(
    client: ClaudeClient,
    *,
    model: str,
    system: list[dict[str, object]],
    question: str,
    countries: list[str],
    max_rows: int,
    max_tokens: int = 4096,
) -> GenerationResult:
    """Genera SQL, lo valida, y si el validador lo rechaza reintenta hasta
    MAX_ATTEMPTS pasandole el error como retroalimentacion. Nunca ejecuta SQL
    -- eso es responsabilidad de quien llama, con el resultado ya validado.

    Levanta OutOfScope si el modelo determina que la pregunta no es
    respondible, y SqlRejected si se agotan los intentos sin una consulta
    valida (el ultimo rechazo, con su regla exacta).
    """
    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": f"Paises: {', '.join(countries)}\nPregunta: {question}",
        }
    ]
    attempts: list[GenerationAttempt] = []

    for attempt_no in range(1, MAX_ATTEMPTS + 1):
        raw = await client.complete_text(
            model=model, system=system, messages=messages, max_tokens=max_tokens
        )
        sql_text = _strip_markdown_fence(raw)

        if sql_text.strip().upper() == OUT_OF_SCOPE_SENTINEL:
            attempts.append(GenerationAttempt(attempt_no, sql_text, accepted=False))
            raise OutOfScope(question)

        try:
            validated = validate(sql_text, max_rows=max_rows)
        except SqlRejected as err:
            attempts.append(
                GenerationAttempt(
                    attempt_no,
                    sql_text,
                    accepted=False,
                    rejection_rule=err.rule,
                    rejection_detail=err.detail,
                )
            )
            if attempt_no == MAX_ATTEMPTS:
                raise
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Ese SQL fue rechazado por el validador: {err.rule} "
                        f"{err.detail}. Corrigelo y responde unicamente con el "
                        "SQL corregido, siguiendo las mismas reglas."
                    ),
                }
            )
            continue

        attempts.append(GenerationAttempt(attempt_no, sql_text, accepted=True))
        return GenerationResult(validated=validated, attempts=attempts)

    # Inalcanzable: el ultimo intento siempre re-lanza o retorna arriba.
    raise AssertionError("generate_validated_sql: bucle de intentos mal formado")
