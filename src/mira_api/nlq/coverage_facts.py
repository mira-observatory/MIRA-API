from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from mira_api.api.schemas import CoverageNote, Warning
from mira_api.db.executor import ReadOnlyExecutor

#: Que entidad representa cada vista, y como contarla por pais junto con sus fechas extremas.
_ENTITY_SQL: dict[str, tuple[str, str]] = {
    "procesos": (
        "query.v_process",
        "select country_code, count(*) as n, "
        "min(publication_date)::date as dt_min, max(publication_date)::date as dt_max "
        "from query.v_process where country_code = any(%(paises)s) group by country_code",
    ),
    "adjudicaciones": (
        "query.v_awards",
        "select p.country_code, count(*) as n, "
        "min(a.award_date)::date as dt_min, max(a.award_date)::date as dt_max "
        "from query.v_awards a join query.v_process p on p.process_id = a.process_id "
        "where p.country_code = any(%(paises)s) group by p.country_code",
    ),
    "proveedores": (
        "query.v_suppliers",
        "select country_code, count(*) as n, "
        "null::date as dt_min, null::date as dt_max "
        "from query.v_suppliers where country_code = any(%(paises)s) group by country_code",
    ),
    "compradores": (
        "query.v_buyers",
        "select country_code, count(*) as n, "
        "null::date as dt_min, null::date as dt_max "
        "from query.v_buyers where country_code = any(%(paises)s) group by country_code",
    ),
    #: query.v_items por si sola no basta: Guatemala tiene 405,623 filas ahi
    #: pero CERO en query.v_award_items (verificado 2026-08-26), asi que
    #: "producto mas vendido" -- que necesita saber que adjudicacion cubrio
    #: cada item -- da cero sin que sea un error de la consulta. La entidad
    #: clave es el vinculo (v_award_items), no el catalogo de items en si.
    "productos": (
        "query.v_award_items",
        "select p.country_code, count(*) as n, "
        "null::date as dt_min, null::date as dt_max "
        "from query.v_award_items ai "
        "join query.v_awards a on a.award_id = ai.award_id "
        "join query.v_process p on p.process_id = a.process_id "
        "where p.country_code = any(%(paises)s) group by p.country_code",
    ),
}

#: El mensaje lo lee una persona, no un sistema: "adjudicaciones de Nicaragua"
#: se entiende, "adjudicaciones de NI" hay que descifrarlo. El codigo ISO
#: sigue yendo en `details`, que es lo que consume el frontend.
_COUNTRY_NAMES = {
    "CR": "Costa Rica",
    "GT": "Guatemala",
    "HN": "Honduras",
    "NI": "Nicaragua",
    "SV": "El Salvador",
    "PA": "Panamá",
}


#: Como se nombra cada entidad al explicar un resultado vacio en ingles.
#: Las claves son las de _ENTITY_SQL.
_ENTITY_NAMES_EN = {
    "procesos": "processes",
    "adjudicaciones": "awards",
    "proveedores": "suppliers",
    "compradores": "buyers",
    "productos": "products",
}


def _country_name(code: str) -> str:
    return _COUNTRY_NAMES.get(code.upper(), code)


def extract_queried_date_range(
    sql: str,
) -> tuple[datetime.date | None, datetime.date | None, str | None]:
    """Extrae el rango o año consultado en la cláusula WHERE del SQL."""
    if not sql:
        return None, None, None
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return None, None, None

    dates: list[datetime.date] = []
    years: list[int] = []

    for lit in tree.find_all(exp.Literal):
        if lit.is_string:
            text = str(lit.this).strip()
            if len(text) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", text):
                try:
                    dt = datetime.date.fromisoformat(text[:10])
                    dates.append(dt)
                except ValueError:
                    pass
        elif lit.is_number:
            val_str = str(lit.this).strip()
            if val_str.isdigit():
                val = int(val_str)
                if 1990 <= val <= 2100:
                    years.append(val)

    if years and not dates:
        y = years[0]
        return datetime.date(y, 1, 1), datetime.date(y, 12, 31), str(y)

    if dates:
        d_min = min(dates)
        d_max = max(dates)
        if d_min.year == d_max.year and (d_max - d_min).days >= 350:
            return d_min, d_max, str(d_min.year)
        if (
            d_max.year == d_min.year + 1
            and d_min.month == 1
            and d_min.day == 1
            and d_max.month == 1
            and d_max.day == 1
        ):
            return d_min, d_max, str(d_min.year)
        if d_min == d_max:
            return d_min, d_max, str(d_min)
        return d_min, d_max, f"{d_min} a {d_max}"

    return None, None, None


#: Columnas de texto libre de query.v_process contra las que el prompt instruye
#: buscar una categoria o modalidad (regla 6c para categoria de producto, y
#: procurement_method para modalidad de contratacion). Cada fuente escribe su
#: propio vocabulario ahi -- Guatemala dice "Compra Directa...", Costa Rica
#: nunca usa la palabra "directa" -- asi que un ILIKE que no calza con NINGUNA
#: fila de un pais no significa que falten datos, solo que ese pais no usa ese
#: nombre.
_TEXT_SEARCH_COLUMNS = frozenset({"procurement_method", "title", "description"})


def extract_text_search_predicates(sql: str) -> list[tuple[str, str]]:
    """Extrae (columna, termino) de cada ILIKE del WHERE sobre una columna de
    _TEXT_SEARCH_COLUMNS. El termino sale sin los '%' de comodin.

    Solo lee el arbol sintactico -- igual que extract_queried_date_range, no
    ejecuta nada. Si el SQL no parsea, devuelve una lista vacia: un fallo aca
    no puede tumbar una respuesta que ya esta lista.
    """
    if not sql:
        return []
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:
        return []

    encontrados: list[tuple[str, str]] = []
    for ilike in tree.find_all(exp.ILike):
        columna = ilike.this
        if not isinstance(columna, exp.Column):
            continue
        nombre_columna = columna.name.lower()
        if nombre_columna not in _TEXT_SEARCH_COLUMNS:
            continue
        patron = ilike.expression
        if not (isinstance(patron, exp.Literal) and patron.is_string):
            continue
        termino = str(patron.this).strip("%").strip()
        if termino and (nombre_columna, termino) not in encontrados:
            encontrados.append((nombre_columna, termino))
    return encontrados


@dataclass(frozen=True)
class EmptyResultDiagnosis:
    warnings: list[Warning]
    coverage: CoverageNote | None


def _entities_for(relations: frozenset[str]) -> list[str]:
    """Entidades que la consulta toco, en el orden en que se declararon.

    Si toco varias, se revisan todas: una pregunta que une procesos con
    adjudicaciones puede fallar por cualquiera de las dos, y decir cual es
    justamente el punto.
    """
    return [name for name, (view, _) in _ENTITY_SQL.items() if view in relations]


async def diagnose_empty_result(
    executor: ReadOnlyExecutor,
    *,
    countries: list[str],
    relations: frozenset[str],
    sql: str = "",
) -> EmptyResultDiagnosis:
    """Distingue "no hay datos cargados" / "periodo no cubierto" de "la consulta no encontro nada".

    Nunca levanta: si el diagnostico falla, el usuario recibe su cero sin
    explicacion, que es lo que recibia antes. Un fallo aqui no puede tumbar
    una respuesta que ya esta lista.
    """
    entities = _entities_for(relations)
    if not entities or not countries:
        return EmptyResultDiagnosis(warnings=[], coverage=None)

    sin_datos: dict[str, list[str]] = {}
    country_dates: dict[str, tuple[datetime.date | None, datetime.date | None]] = {}
    total_disponible = 0

    for entity in entities:
        _, sql_entity = _ENTITY_SQL[entity]
        try:
            rows = await executor.run(sql_entity, max_rows=50, params={"paises": countries})
        except Exception:  # noqa: BLE001 - un extra nunca puede tumbar la respuesta
            return EmptyResultDiagnosis(warnings=[], coverage=None)

        cargados = {str(row["country_code"]): int(row["n"]) for row in rows.rows}
        for row in rows.rows:
            code = str(row["country_code"])
            dt_min = row.get("dt_min")
            dt_max = row.get("dt_max")
            if dt_min is not None or dt_max is not None:
                cur_min, cur_max = country_dates.get(code, (None, None))
                new_min = min(filter(None, [cur_min, dt_min])) if (cur_min or dt_min) else None
                new_max = max(filter(None, [cur_max, dt_max])) if (cur_max or dt_max) else None
                country_dates[code] = (new_min, new_max)

        total_disponible += sum(cargados.values())
        faltantes = [pais for pais in countries if cargados.get(pais, 0) == 0]
        if faltantes:
            sin_datos[entity] = faltantes

    if sin_datos:
        detalle = ", ".join(
            f"{entity} de {', '.join(_country_name(p) for p in paises)}"
            for entity, paises in sorted(sin_datos.items())
        )
        mensaje = (
            f"El resultado esta vacio porque todavia no hay {detalle} en la base. "
            "No significa que no existan: significa que aun no se han cargado."
        )
        detalle_en = ", ".join(
            f"{_ENTITY_NAMES_EN.get(entity, entity)} for "
            f"{', '.join(_country_name(p) for p in paises)}"
            for entity, paises in sorted(sin_datos.items())
        )
        mensaje_en = (
            f"This result is empty because there are no {detalle_en} in the database "
            "yet. That does not mean none exist: it means they have not been loaded."
        )
        return EmptyResultDiagnosis(
            warnings=[
                Warning(
                    code="PARTIAL_COVERAGE",
                    message_es=mensaje,
                    message_en=mensaje_en,
                    details={"sin_datos": sin_datos},
                )
            ],
            coverage=CoverageNote(countries=countries, rows_total=total_disponible),
        )

    # Revisar si la consulta filtro por un periodo fuera de rango para los paises consultados
    q_min, q_max, period_label = extract_queried_date_range(sql)
    if period_label and country_dates:
        out_of_range_countries: list[tuple[str, datetime.date | None, datetime.date | None]] = []
        for code in countries:
            dt_min, dt_max = country_dates.get(code, (None, None))
            if dt_min is not None and q_max is not None and q_max < dt_min:
                out_of_range_countries.append((code, dt_min, dt_max))
            elif dt_max is not None and q_min is not None and q_min > dt_max:
                out_of_range_countries.append((code, dt_min, dt_max))

        if out_of_range_countries:
            detalles_cobertura = ", ".join(
                f"{_country_name(c)} (disponible: {dmin} a {dmax})"
                for c, dmin, dmax in out_of_range_countries
            )
            mensaje = (
                f"No hay datos disponibles para el periodo consultado ({period_label}) en "
                f"{detalles_cobertura}. El resultado en cero refleja la ausencia de datos "
                "en ese rango, no que no hayan existido contrataciones."
            )
            cobertura_en = ", ".join(
                f"{_country_name(c)} (available: {dmin} to {dmax})"
                for c, dmin, dmax in out_of_range_countries
            )
            mensaje_en = (
                f"No data is available for the period asked about ({period_label}) in "
                f"{cobertura_en}. The zero reflects the absence of data in that range, "
                "not that no procurement took place."
            )
            return EmptyResultDiagnosis(
                warnings=[
                    Warning(
                        code="NO_DATA_FOR_PERIOD",
                        message_es=mensaje,
                        message_en=mensaje_en,
                        details={
                            "periodo_consultado": period_label,
                            "paises_fuera_de_rango": [c for c, _, _ in out_of_range_countries],
                        },
                    )
                ],
                coverage=CoverageNote(countries=countries, rows_total=total_disponible),
            )

    # Revisar si el WHERE busca una categoria/modalidad en texto libre
    # (procurement_method, title, description) que no calza con NINGUNA fila
    # de alguno de los paises pedidos. Verificado en vivo (2026-08-27):
    # "adjudicacion directa" en Costa Rica volvia vacio sin explicacion --
    # CR tiene 12,783 procesos (el chequeo de entidad de arriba no dispara) y
    # el periodo no estaba fuera de rango, pero CR nunca escribe la palabra
    # "directa" en su procurement_method (usa "Procedimiento por Excepcion",
    # etc.). Sin este chequeo, ese cero se ve identico a "no hay datos".
    predicados = extract_text_search_predicates(sql)
    if predicados and "query.v_process" in relations:
        sin_match: dict[str, list[str]] = {}
        for columna, termino in predicados:
            sql_check = (
                f"select country_code, count(*) as n from query.v_process "  # noqa: S608
                f"where country_code = any(%(paises)s) and {columna} ilike %(patron)s "
                f"group by country_code"
            )
            try:
                rows = await executor.run(
                    sql_check,
                    max_rows=50,
                    params={"paises": countries, "patron": f"%{termino}%"},
                )
            except Exception:  # noqa: BLE001 - un extra nunca puede tumbar la respuesta
                continue
            con_match = {str(row["country_code"]) for row in rows.rows if row["n"] > 0}
            faltan = [p for p in countries if p not in con_match]
            if faltan:
                sin_match[termino] = faltan

        if sin_match:
            detalle = "; ".join(
                f'"{termino}" en {", ".join(_country_name(p) for p in paises)}'
                for termino, paises in sin_match.items()
            )
            mensaje = (
                f"No se encontro nada relacionado con {detalle} en los datos "
                "cargados. Puede que esa fuente use otro nombre para clasificar "
                "esa categoria o modalidad de compra, no que falten datos en general."
            )
            detalle_en = "; ".join(
                f'"{termino}" in {", ".join(_country_name(p) for p in paises)}'
                for termino, paises in sin_match.items()
            )
            mensaje_en = (
                f"Nothing related to {detalle_en} was found in the loaded data. "
                "The source may classify that category or procurement method under "
                "a different name, not that data is missing overall."
            )
            return EmptyResultDiagnosis(
                warnings=[
                    Warning(
                        code="NO_MATCH_FOR_TERM",
                        message_es=mensaje,
                        message_en=mensaje_en,
                        details={"terminos_sin_match": sin_match},
                    )
                ],
                coverage=CoverageNote(countries=countries, rows_total=total_disponible),
            )

    # Hay datos de sobra y el periodo esta dentro de rango: el cero es real.
    return EmptyResultDiagnosis(
        warnings=[],
        coverage=CoverageNote(countries=countries, rows_total=total_disponible),
    )

