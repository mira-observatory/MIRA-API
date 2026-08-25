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
        if d_min == d_max:
            return d_min, d_max, str(d_min)
        return d_min, d_max, f"{d_min} a {d_max}"

    return None, None, None


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
        return EmptyResultDiagnosis(
            warnings=[
                Warning(
                    code="PARTIAL_COVERAGE",
                    message_es=mensaje,
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
            return EmptyResultDiagnosis(
                warnings=[
                    Warning(
                        code="NO_DATA_FOR_PERIOD",
                        message_es=mensaje,
                        details={
                            "periodo_consultado": period_label,
                            "paises_fuera_de_rango": [c for c, _, _ in out_of_range_countries],
                        },
                    )
                ],
                coverage=CoverageNote(countries=countries, rows_total=total_disponible),
            )

    # Hay datos de sobra y el periodo esta dentro de rango: el cero es real.
    return EmptyResultDiagnosis(
        warnings=[],
        coverage=CoverageNote(countries=countries, rows_total=total_disponible),
    )

