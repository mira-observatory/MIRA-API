from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from mira_api.audit.outcomes import Outcome


class Column(BaseModel):
    name: str
    kind: Literal["number", "money", "date", "text"]
    #: Presente solo en columnas de dinero. Nunca se suman monedas distintas, asi que
    #: el frontend necesita saber en cual esta cada columna para formatear y para
    #: decidir si puede graficar.
    currency_code: str | None = None


class EntityCandidate(BaseModel):
    """Un candidato de comprador o proveedor, con su conteo real.

    Puede haber varios candidatos parecidos para la misma busqueda (p.ej.
    "Karro y Limon S.A" y "Carro y Limon S.A"); se devuelven todos con su
    conteo real, nunca fusionados. No se senala si dos candidatos parecen
    duplicados entre si -- decision de producto (2026-08-15): nombres
    parecidos pueden ser entidades distintas a proposito, y esa comparacion no
    se hace.
    """

    entity_type: Literal["supplier", "buyer"]
    entity_id: int
    display_name: str
    name_normalised: str
    country_code: str
    tax_id: str | None = None
    match_method: Literal["TAX_ID", "NAME_EXACT", "NAME_FUZZY"]
    similarity: float | None = None
    record_count: int = Field(description="Conteo real en la base, no una estimacion")


class Warning(BaseModel):
    code: Literal[
        "PARTIAL_COVERAGE",
        "MIXED_CURRENCY",
        "TRUNCATED_RESULT",
        "NULL_AMOUNTS_EXCLUDED",
        "NO_DATA_FOR_PERIOD",
        "UNNORMALISED_ITEM_TEXT",
        "MISSING_COUNTRY_IN_RESULT",
        "LIMIT_MAY_HIDE_ROWS",
        "NO_MATCH_FOR_TERM",
    ]
    message_es: str
    #: El mismo aviso en ingles. Se agrega en vez de renombrar `message_es`
    #: porque ese campo ya viaja en el contrato publico y en los tipos que
    #: genera el frontend: romperlo obligaria a desplegar los dos repos a la
    #: vez. El cliente elige segun `language` de la respuesta.
    message_en: str | None = None
    details: dict[str, Any] = {}


class CoverageNote(BaseModel):
    """Que respalda esta respuesta en concreto.

    Un cero por falta de datos cargados y un cero real son cosas distintas, y la
    respuesta debe permitir distinguirlas.
    """

    countries: list[str]
    period_from: str | None = None
    period_to: str | None = None
    rows_total: int | None = None
    rows_with_amount: int | None = None
    data_version: str | None = None


class QuotaState(BaseModel):
    remaining_hour: int
    remaining_month: int
    resets_at: str


class QueryResponse(BaseModel):
    """Contrato de respuesta. Los datos y la prosa nunca se mezclan.

    El frontend renderiza la tabla desde `rows`; `narrative` es un acompanante
    prescindible, marcado como generado por IA.
    """

    query_id: UUID
    question: str
    intent: str | None = None
    strategy: Literal["generated_sql", "cache", "out_of_scope"]
    outcome: Outcome

    #: El SQL ejecutado se devuelve al usuario. Es la prueba de que el numero no fue
    #: inventado.
    sql_executed: str | None = None
    countries_filter: list[str]

    #: Idioma en que se redacto la respuesta, detectado de la pregunta. El
    #: cliente lo usa para elegir entre `message_es` y `message_en` de cada
    #: aviso. Default "es": es un observatorio centroamericano.
    language: Literal["es", "en"] = "es"

    columns: list[Column] = []
    rows: list[dict[str, Any]] = []
    row_count: int = 0
    truncated: bool = False

    entity_candidates: list[EntityCandidate] = []
    warnings: list[Warning] = []
    coverage: CoverageNote | None = None

    narrative: str | None = None
    narrative_verified: bool = False
    #: Numeros hallados en la redaccion que no existen en el resultado. Si no esta
    #: vacio, la narrativa se descarta y el outcome pasa a OK_DEGRADED_NARRATIVE.
    unverified_numbers: list[str] = []

    quota: QuotaState | None = None
    timings_ms: dict[str, int] = {}


class ConversationTurn(BaseModel):
    """Una pregunta anterior y el SQL que la respondio.

    Se manda el SQL, no las filas: es lo que deja resolver un seguimiento como
    "¿y en Honduras?" cambiandole el pais a la consulta anterior, y ocupa poco
    en el prompt.

    Lo escribe el cliente, asi que no es confiable -- pero tampoco necesita
    serlo: nada de aqui se ejecuta. Solo entra al prompt, y todo lo que el
    modelo produzca despues pasa igual por el validador (lista blanca de
    vistas, filtro de pais, LIMIT). Lo peor que logra un historial falseado es
    que el modelo genere SQL que el validador rechaza.
    """

    question: str = Field(max_length=400)
    countries: list[str] = Field(min_length=1)
    sql: str = Field(max_length=4000)


class QueryRequest(BaseModel):
    question: str = Field(max_length=400)
    countries: list[str] = Field(min_length=1)
    #: Turnos anteriores de la conversacion, del mas viejo al mas reciente.
    #: Acotado a proposito: cada turno son tokens de entrada en cada llamada,
    #: y mas de tres rara vez ayuda a resolver un seguimiento.
    history: list[ConversationTurn] = Field(default=[], max_length=3)
    date_from: str | None = None
    date_to: str | None = None
    process_status: str | None = None
    procurement_method: str | None = None
    #: Permite pedir solo la tabla y ahorrar una llamada al modelo.
    narrative: bool = True
    #: Candidatos elegidos tras una desambiguacion previa.
    entity_ids: list[int] = []


class FeedbackRequest(BaseModel):
    query_id: UUID
    kind: Literal["USEFUL", "NOT_USEFUL", "WRONG_NUMBER", "WRONG_ENTITY", "MISSING_DATA"]
    comment: str | None = Field(default=None, max_length=1000)


class CoverageSource(BaseModel):
    source_key: str
    source_system: str
    display_name: str
    status: Literal["ACTIVE", "PLANNED", "INACTIVE"]
    process_count: int
    buyer_count: int
    supplier_count: int
    coverage_from: date | None = None
    coverage_to: date | None = None
    complete_process_count: int
    partial_process_count: int
    process_without_date_count: int
    last_successful_load_at: datetime | None = None
    refreshed_at: datetime | None = None


class CoverageCountry(BaseModel):
    country_code: str
    country_name: str
    flag_asset: str | None = None
    status: Literal["ACTIVE", "PLANNED", "INACTIVE"]
    active_sources: int
    process_count: int
    buyer_count: int
    supplier_count: int
    coverage_from: date | None = None
    coverage_to: date | None = None
    last_successful_load_at: datetime | None = None
    sources: list[CoverageSource] = Field(default_factory=list)


class CoverageSummary(BaseModel):
    active_countries: int
    planned_countries: int
    active_sources: int
    process_count: int
    coverage_from: date | None = None
    coverage_to: date | None = None
    last_successful_load_at: datetime | None = None


class CoverageResponse(BaseModel):
    summary: CoverageSummary
    countries: list[CoverageCountry] = Field(default_factory=list)


class Procedure(BaseModel):
    process_id: str
    process_number: str | None = None
    country_code: str
    title: str | None = None
    description: str | None = None
    procurement_method: str | None = None
    process_status: str | None = None
    source_status: str | None = None
    publication_date: datetime | None = None
    closing_date: datetime | None = None
    estimated_amount: Decimal | None = None
    currency_code: str | None = None
    source_system: str
    source_url: str | None = None
    data_quality_status: Literal["COMPLETE", "PARTIAL", "INVALID"]


class ProcedureFilters(BaseModel):
    q: str | None = None
    countries: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    procurement_methods: list[str] = Field(default_factory=list)
    published_from: date | None = None
    published_to: date | None = None


class ProceduresResponse(BaseModel):
    items: list[Procedure] = Field(default_factory=list)
    total: int
    page: int
    page_size: int
    total_pages: int
    filters: ProcedureFilters


class ProcessStatusOption(BaseModel):
    value: str
    process_count: int


class ProcessStatusesResponse(BaseModel):
    statuses: list[ProcessStatusOption] = Field(default_factory=list)
