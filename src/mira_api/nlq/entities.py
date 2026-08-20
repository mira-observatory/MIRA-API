from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mira_api.api.schemas import EntityCandidate
from mira_api.db.executor import ReadOnlyExecutor

EntityType = Literal["supplier", "buyer"]

#: Umbral de similitud de trigrama por debajo del cual un candidato ni siquiera
#: se considera. No es el umbral que ordena los resultados -- coincidencia exacta
#: y tax_id siempre van primero (ver el ORDER BY en _build_sql).
FUZZY_SIMILARITY_THRESHOLD = 0.3


@dataclass(frozen=True)
class _EntityConfig:
    view: str
    id_column: str
    tax_id_column: str
    #: Vista de relacion donde se cuenta el conteo REAL de la entidad. Un
    #: comprador se cuenta por proceso (query.v_process_buyers); un proveedor se
    #: cuenta por adjudicacion (query.v_award_suppliers) -- un proceso puede tener
    #: varias adjudicaciones, cada una a un proveedor distinto.
    link_view: str
    link_count_column: str


#: Unicas relaciones que este modulo toca. A diferencia del SQL generado por el
#: modelo, este SQL lo escribe el servicio mismo -- no pasa por
#: nlq.validator.validate porque la unica entrada del usuario que llega aqui es
#: el texto de busqueda, y siempre va ligado como parametro, nunca interpolado.
_CONFIG: dict[EntityType, _EntityConfig] = {
    "buyer": _EntityConfig(
        view="query.v_buyers",
        id_column="buyer_id",
        tax_id_column="buyer_tax_id",
        link_view="query.v_process_buyers",
        link_count_column="process_id",
    ),
    "supplier": _EntityConfig(
        view="query.v_suppliers",
        id_column="supplier_id",
        tax_id_column="supplier_tax_id",
        link_view="query.v_award_suppliers",
        link_count_column="award_id",
    ),
}


def _build_sql(config: _EntityConfig) -> str:
    # query.f_unaccent es la misma funcion, calificada igual, que arma el indice
    # GIN de trigrama sobre mart.suppliers/buyers (sql/002_indexes_and_views.sql
    # en MIRA-ETL) -- si esta expresion no calza con la del indice, Postgres no
    # lo usa y cae a un escaneo secuencial completo.
    normalised = "lower(query.f_unaccent(e.name_normalised))"
    normalised_query = "lower(query.f_unaccent(%(query)s))"
    return f"""
        select
            e.{config.id_column} as entity_id,
            e.country_code,
            e.name_normalised as display_name,
            e.name_normalised,
            e.{config.tax_id_column} as tax_id,
            coalesce(link.record_count, 0) as record_count,
            case
                when e.{config.tax_id_column} = %(query)s then 'TAX_ID'
                when {normalised} = {normalised_query} then 'NAME_EXACT'
                else 'NAME_FUZZY'
            end as match_method,
            similarity({normalised}, {normalised_query}) as similarity
        from {config.view} e
        left join (
            select {config.id_column}, count(distinct {config.link_count_column}) as record_count
            from {config.link_view}
            group by {config.id_column}
        ) link on link.{config.id_column} = e.{config.id_column}
        where e.country_code = any(%(countries)s)
          and (
              e.{config.tax_id_column} = %(query)s
              or {normalised} = {normalised_query}
              or similarity({normalised}, {normalised_query}) >= %(threshold)s
          )
        order by
            (e.{config.tax_id_column} = %(query)s) desc,
            ({normalised} = {normalised_query}) desc,
            similarity({normalised}, {normalised_query}) desc,
            record_count desc
        limit %(limit)s
    """


async def resolve_entities(
    executor: ReadOnlyExecutor,
    *,
    query: str,
    entity_type: EntityType,
    countries: list[str],
    limit: int = 20,
) -> list[EntityCandidate]:
    """Resuelve un texto escrito por el usuario a candidatos de comprador o
    proveedor. Sin IA -- SQL parametrizado y trigramas, filtrado por los
    paises del selector.

    Devuelve SIEMPRE la lista completa de candidatos con su record_count real.
    Nunca fusiona ni suma: si "Karro y Limon S.A" tiene 6 procesos y "Carro y
    Limon S.A" tiene 9, ambos se devuelven con 6 y 9. No hay ninguna ruta en
    este codigo que combine dos candidatos en uno.
    """
    text = query.strip()
    if not text or not countries:
        return []

    config = _CONFIG[entity_type]
    sql = _build_sql(config)
    params = {
        "query": text,
        "countries": countries,
        "threshold": FUZZY_SIMILARITY_THRESHOLD,
        "limit": limit,
    }
    result = await executor.run(sql, max_rows=limit, params=params)

    return [
        EntityCandidate(
            entity_type=entity_type,
            entity_id=row["entity_id"],
            display_name=row["display_name"],
            name_normalised=row["name_normalised"],
            country_code=row["country_code"],
            tax_id=row["tax_id"],
            match_method=row["match_method"],
            similarity=row["similarity"],
            record_count=row["record_count"],
        )
        for row in result.rows
    ]
