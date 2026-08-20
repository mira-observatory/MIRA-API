from __future__ import annotations

from dataclasses import dataclass

from mira_api.audit.outcomes import Outcome
from mira_api.llm.client import ClaudeClient
from mira_api.nlq.prompts import (
    SQL_SYSTEM_PROMPT,
    SQL_USER_PROMPT,
    SQL_VALIDATION_RETRY_PROMPT,
)
from mira_api.nlq.semantic_dictionary import ColumnDoc, format_for_prompt
from mira_api.nlq.validator import SqlRejected, ValidatedSql, validate

#: Intento inicial + 2 reintentos. Dos fallos de validador seguidos significan
#: que la pregunta no es expresable contra el esquema -- un tercer intento no
#: cambia eso. Ver Parte 1.8 del plan de arquitectura.
MAX_ATTEMPTS = 3

#: Sentinela que el modelo devuelve cuando la pregunta no se puede responder
#: con las columnas disponibles. No pasa por el validador -- se detecta antes.
OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"

class OutOfScope(Exception):
    """El modelo determino que la pregunta no es respondible con el esquema
    disponible. No es un error -- es el sistema funcionando."""

    def __init__(self, question: str, usage: Usage, attempts: list[GenerationAttempt]) -> None:
        super().__init__(question)
        self.usage = usage
        self.attempts = attempts


class GenerationFailed(Exception):
    """Se agotaron los MAX_ATTEMPTS intentos sin una consulta valida. Envuelve
    el ultimo SqlRejected del validador (mismo outcome/rule/detail) y le suma
    el uso acumulado de TODOS los intentos -- un reintento tambien gasta
    tokens, el presupuesto tiene que contarlos aunque la generacion falle."""

    def __init__(
        self, last_rejection: SqlRejected, usage: Usage, attempts: list[GenerationAttempt]
    ) -> None:
        super().__init__(str(last_rejection))
        self.outcome = last_rejection.outcome
        self.rule = last_rejection.rule
        self.detail = last_rejection.detail
        self.usage = usage
        self.attempts = attempts


@dataclass(frozen=True)
class PriorTurn:
    """Un turno anterior ya resuelto. Espejo de api.schemas.ConversationTurn,
    repetido aqui para que este modulo no dependa de la capa HTTP."""

    question: str
    countries: list[str]
    sql: str


@dataclass(frozen=True)
class GenerationAttempt:
    attempt_no: int
    sql_text: str
    accepted: bool
    rejection_rule: str | None = None
    rejection_detail: str | None = None
    #: Codigo de la taxonomia (Outcome) para intentos rechazados. None en un
    #: intento aceptado -- el resultado final (OK/FAILED_DB_*/...) solo se
    #: conoce despues de ejecutar, y lo completa quien llama (pipeline.py).
    outcome: Outcome | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


@dataclass(frozen=True)
class GenerationResult:
    validated: ValidatedSql
    attempts: list[GenerationAttempt]
    #: Suma de TODOS los intentos -- un reintento tambien cuesta tokens, el
    #: presupuesto (Hito 5) tiene que verlo completo, no solo el ultimo.
    usage: Usage


def build_system_blocks(columns: list[ColumnDoc]) -> list[dict[str, object]]:
    """El diccionario semantico rara vez cambia -- es el contenido estable que
    se marca para cacheo. La pregunta y los paises van en `messages`, siempre
    despues del ultimo cache_control, para no invalidar el cache en cada
    llamada distinta."""
    text = SQL_SYSTEM_PROMPT.format(dictionary=format_for_prompt(columns))
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _user_turn(question: str, countries: list[str]) -> str:
    return SQL_USER_PROMPT.format(countries=", ".join(countries), question=question)


def _build_messages(
    question: str, countries: list[str], history: list[PriorTurn]
) -> list[dict[str, object]]:
    """Replica la conversacion como pares pregunta -> SQL. Cada turno anterior
    lleva SUS paises, no los de ahora: reescribirlos dejaria un mensaje que
    dice "Paises: HN" junto a un SQL que filtra 'CR', y esa contradiccion es
    justo lo que confunde al modelo en el turno que importa.

    Va todo en `messages`, despues del ultimo cache_control (que vive en el
    bloque `system`), asi que el historial no invalida el cache del prompt.
    """
    messages: list[dict[str, object]] = []
    for prior in history:
        messages.append({"role": "user", "content": _user_turn(prior.question, prior.countries)})
        messages.append({"role": "assistant", "content": prior.sql})
    messages.append({"role": "user", "content": _user_turn(question, countries)})
    return messages


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
    history: list[PriorTurn] | None = None,
    max_tokens: int = 4096,
) -> GenerationResult:
    """Genera SQL, lo valida, y si el validador lo rechaza reintenta hasta
    MAX_ATTEMPTS pasandole el error como retroalimentacion. Nunca ejecuta SQL
    -- eso es responsabilidad de quien llama, con el resultado ya validado.

    Levanta OutOfScope si el modelo determina que la pregunta no es
    respondible, y GenerationFailed si se agotan los intentos sin una consulta
    valida (el ultimo rechazo, con su regla exacta). Ambas excepciones cargan
    el uso acumulado de tokens -- un intento fallido tambien cuesta.
    """
    messages = _build_messages(question, countries, history or [])
    attempts: list[GenerationAttempt] = []
    usage = Usage()

    for attempt_no in range(1, MAX_ATTEMPTS + 1):
        completion = await client.complete_text(
            model=model, system=system, messages=messages, max_tokens=max_tokens
        )
        usage = usage + Usage(
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cache_read_tokens=completion.cache_read_tokens,
            cache_creation_tokens=completion.cache_creation_tokens,
        )
        raw = completion.text
        sql_text = _strip_markdown_fence(raw)

        if sql_text.strip().upper() == OUT_OF_SCOPE_SENTINEL:
            attempts.append(
                GenerationAttempt(
                    attempt_no, sql_text, accepted=False, outcome=Outcome.OUT_OF_SCOPE
                )
            )
            raise OutOfScope(question, usage, attempts)

        try:
            validated = validate(sql_text, max_rows=max_rows, countries=countries)
        except SqlRejected as err:
            attempts.append(
                GenerationAttempt(
                    attempt_no,
                    sql_text,
                    accepted=False,
                    rejection_rule=err.rule,
                    rejection_detail=err.detail,
                    outcome=err.outcome,
                )
            )
            if attempt_no == MAX_ATTEMPTS:
                raise GenerationFailed(err, usage, attempts) from err
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": SQL_VALIDATION_RETRY_PROMPT.format(
                        rule=err.rule, detail=err.detail
                    ),
                }
            )
            continue

        attempts.append(GenerationAttempt(attempt_no, sql_text, accepted=True))
        return GenerationResult(validated=validated, attempts=attempts, usage=usage)

    # Inalcanzable: el ultimo intento siempre re-lanza o retorna arriba.
    raise AssertionError("generate_validated_sql: bucle de intentos mal formado")
