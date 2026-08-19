from __future__ import annotations

from datetime import date, datetime
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

    Nunca se fusionan candidatos parecidos. Si "Karro y Limon S.A" tiene 6 procesos
    y "Carro y Limon S.A" tiene 9, se devuelven los dos con 6 y 9. Nunca 15.
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
    #: Otros candidatos que se le parecen. Alimenta la advertencia de posible
    #: duplicado; no implica ninguna accion sobre los datos.
    similar_to: list[int] = []


class Warning(BaseModel):
    code: Literal[
        "POSSIBLE_DUPLICATE_ENTITY",
        "PARTIAL_COVERAGE",
        "MIXED_GRAIN",
        "MIXED_CURRENCY",
        "TRUNCATED_RESULT",
        "NULL_AMOUNTS_EXCLUDED",
        "NO_DATA_FOR_PERIOD",
    ]
    message_es: str
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


class QueryRequest(BaseModel):
    question: str = Field(max_length=400)
    countries: list[str] = Field(min_length=1)
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
