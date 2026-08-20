"""Por que una consulta no devolvio nada.

"No hubo contrataciones" y "no tenemos esos datos" son afirmaciones muy
distintas, y solo una de las dos suele ser cierta. Un cero desnudo las
confunde, y en un observatorio de transparencia esa confusion es el peor
error posible: hace parecer que no pasa nada donde en realidad no estamos
mirando.

Nicaragua es el caso concreto (2026-08-20): tiene 409 procesos cargados pero
cero adjudicaciones y cero proveedores. Preguntar "cuanto se adjudico en
Nicaragua" devuelve cero filas, y ese cero no significa que no se adjudico
nada -- significa que el ETL todavia no cargo esa parte.

Esto solo se consulta cuando el resultado vino vacio, asi que no le cuesta
nada al camino normal.
"""

from __future__ import annotations

from dataclasses import dataclass

from mira_api.api.schemas import CoverageNote, Warning
from mira_api.db.executor import ReadOnlyExecutor

#: Que entidad representa cada vista, y como contarla por pais. Las vistas de
#: adjudicaciones e items no tienen country_code: se llega a el por el proceso.
_ENTITY_SQL: dict[str, tuple[str, str]] = {
    "procesos": (
        "query.v_process",
        "select country_code, count(*) as n from query.v_process"
        " where country_code = any(%(paises)s) group by country_code",
    ),
    "adjudicaciones": (
        "query.v_awards",
        "select p.country_code, count(*) as n from query.v_awards a"
        " join query.v_process p on p.process_id = a.process_id"
        " where p.country_code = any(%(paises)s) group by p.country_code",
    ),
    "proveedores": (
        "query.v_suppliers",
        "select country_code, count(*) as n from query.v_suppliers"
        " where country_code = any(%(paises)s) group by country_code",
    ),
    "compradores": (
        "query.v_buyers",
        "select country_code, count(*) as n from query.v_buyers"
        " where country_code = any(%(paises)s) group by country_code",
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
    executor: ReadOnlyExecutor, *, countries: list[str], relations: frozenset[str]
) -> EmptyResultDiagnosis:
    """Distingue "no hay datos cargados" de "la consulta no encontro nada".

    Nunca levanta: si el diagnostico falla, el usuario recibe su cero sin
    explicacion, que es lo que recibia antes. Un fallo aqui no puede tumbar
    una respuesta que ya esta lista.
    """
    entities = _entities_for(relations)
    if not entities or not countries:
        return EmptyResultDiagnosis(warnings=[], coverage=None)

    sin_datos: dict[str, list[str]] = {}
    total_disponible = 0
    for entity in entities:
        _, sql = _ENTITY_SQL[entity]
        try:
            rows = await executor.run(sql, max_rows=50, params={"paises": countries})
        except Exception:  # noqa: BLE001 - un extra nunca puede tumbar la respuesta
            # Deliberadamente amplio: esto se ejecuta sobre una respuesta que
            # ya esta lista para entregarse. Cualquier fallo aqui degrada a "no
            # se pudo explicar el vacio", que es lo que habia antes, nunca a un
            # error para quien pregunto.
            return EmptyResultDiagnosis(warnings=[], coverage=None)
        cargados = {str(row["country_code"]): int(row["n"]) for row in rows.rows}
        total_disponible += sum(cargados.values())
        faltantes = [pais for pais in countries if cargados.get(pais, 0) == 0]
        if faltantes:
            sin_datos[entity] = faltantes

    if not sin_datos:
        # Hay datos de sobra de todo lo que la consulta toco: el cero es real.
        return EmptyResultDiagnosis(
            warnings=[],
            coverage=CoverageNote(countries=countries, rows_total=total_disponible),
        )

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
