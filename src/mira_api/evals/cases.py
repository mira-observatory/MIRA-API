"""Preguntas de referencia y lo que debe cumplirse en cada una.

**Ninguna espera un valor concreto.** No dicen "7992 procesos" ni "15,318,000,000":
los datos cambian con cada recarga del ETL, y una suite que se cae porque
Costa Rica ahora tiene otro numero de procesos no mide nada, solo estorba.

Lo que afirman son invariantes -- propiedades que deben cumplirse con
cualquier dato: que la consulta filtre por el pais pedido, que toque las
vistas correctas, que la narrativa no cite un numero que no este en el
resultado, que una pregunta fuera de dominio no genere SQL. Eso sigue siendo
cierto antes y despues de vaciar la base.

Varios casos existen porque son regresiones de bugs reales:
ver `regression` en cada uno.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mira_api.audit.outcomes import Outcome
from mira_api.nlq.sql_generation import PriorTurn


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    countries: list[str]

    #: Resultados aceptables. Se listan varios cuando el resultado depende de
    #: que haya datos cargados: sin datos, una consulta correcta devuelve
    #: OK_ZERO_ROWS, y eso no es un fallo del sistema.
    allowed_outcomes: frozenset[Outcome]

    #: Si la pregunta debe producir SQL. Falso para lo que esta fuera de dominio.
    expect_sql: bool = True

    #: Vistas que el SQL tiene que tocar. Vacio = no se comprueba.
    expect_relations: frozenset[str] = frozenset()

    #: Codigos de pais que el SQL debe filtrar (en mayuscula). Vacio = no se
    #: comprueba. Es el invariante mas importante: sin el, una pregunta sobre
    #: Honduras puede devolver datos de Costa Rica.
    expect_countries: frozenset[str] = frozenset()

    #: Si hay filas, la narrativa no puede citar numeros ausentes del
    #: resultado. Se omite cuando row_count es 0, porque ahi se sirve una
    #: plantilla y no hay redaccion que verificar.
    expect_verified_narrative: bool = True

    #: Turnos previos, para los casos de memoria conversacional.
    history: list[PriorTurn] = field(default_factory=list)

    #: Que bug concreto vigila este caso, si nacio de uno.
    regression: str = ""


_CR = ["CR"]

#: SQL de un turno anterior, para los casos de seguimiento. Es SQL que el
#: sistema genero de verdad, no inventado para la prueba.
_CONTEO_CR = PriorTurn(
    question="cuantos procesos hay en Costa Rica",
    countries=["CR"],
    sql="SELECT COUNT(*) FROM query.v_process WHERE country_code = 'CR'",
)


CASES: list[Case] = [
    Case(
        id="conteo_simple",
        question="cuantos procesos hay en Costa Rica",
        countries=_CR,
        allowed_outcomes=frozenset({Outcome.OK}),
        expect_relations=frozenset({"query.v_process"}),
        expect_countries=frozenset({"CR"}),
    ),
    Case(
        id="top_n_por_monto",
        question="cuales son las 10 adjudicaciones mas caras de Costa Rica",
        countries=_CR,
        allowed_outcomes=frozenset({Outcome.OK, Outcome.OK_ZERO_ROWS}),
        expect_relations=frozenset({"query.v_awards"}),
        expect_countries=frozenset({"CR"}),
        regression=(
            "El verificador marcaba el 10 de 'las 10 mas caras' como inventado "
            "porque no estaba en ninguna celda, y descartaba una narrativa "
            "correcta. Rompia toda pregunta de tipo 'top N'."
        ),
    ),
    Case(
        id="fecha_en_la_narrativa",
        question="dame las 5 adjudicaciones mas recientes de Costa Rica con su fecha",
        countries=_CR,
        allowed_outcomes=frozenset({Outcome.OK, Outcome.OK_ZERO_ROWS}),
        expect_relations=frozenset({"query.v_awards"}),
        expect_countries=frozenset({"CR"}),
        regression=(
            "El verificador solo leia celdas numericas, asi que 'adjudicada el "
            "9 de mayo de 2025' se marcaba como alucinacion aunque la fecha "
            "estuviera en la celda award_date."
        ),
    ),
    Case(
        id="proveedores_por_contratos",
        question="que proveedores recibieron mas adjudicaciones en Costa Rica",
        countries=_CR,
        allowed_outcomes=frozenset({Outcome.OK, Outcome.OK_ZERO_ROWS}),
        expect_relations=frozenset({"query.v_suppliers"}),
        expect_countries=frozenset({"CR"}),
    ),
    Case(
        id="fuera_de_dominio",
        question="cual es la capital de Guatemala",
        countries=["GT"],
        allowed_outcomes=frozenset({Outcome.OUT_OF_SCOPE}),
        expect_sql=False,
        expect_verified_narrative=False,
    ),
    Case(
        id="cobertura_no_pasa_por_el_modelo",
        question="hasta que fecha llegan los datos cargados de Costa Rica",
        countries=_CR,
        # Puede responderla con fechas de v_process, o declararla fuera de
        # dominio. Lo que NO puede es tocar query.v_coverage.
        allowed_outcomes=frozenset(
            {Outcome.OK, Outcome.OK_ZERO_ROWS, Outcome.OUT_OF_SCOPE}
        ),
        expect_sql=False,
        expect_verified_narrative=False,
        regression=(
            "El diccionario semantico describia query.v_coverage, que el "
            "validador no permite: el modelo generaba SQL contra ella y se "
            "gastaban los 3 intentos para terminar en REJECTED_SQL_RELATION."
        ),
    ),
    Case(
        id="seguimiento_cambia_pais",
        question="y en Honduras?",
        countries=["HN"],
        allowed_outcomes=frozenset({Outcome.OK, Outcome.OK_ZERO_ROWS}),
        expect_relations=frozenset({"query.v_process"}),
        expect_countries=frozenset({"HN"}),
        history=[_CONTEO_CR],
        regression=(
            "Sin historial, '¿y en Honduras?' llegaba sola al modelo y este "
            "adivinaba una consulta distinta (agrupada por moneda con sumas) "
            "en vez de repetir el conteo anterior cambiando el pais."
        ),
    ),
    Case(
        id="seguimiento_no_arrastra_el_pais_viejo",
        question="y cuantos hay en Honduras?",
        countries=["HN"],
        allowed_outcomes=frozenset({Outcome.OK, Outcome.OK_ZERO_ROWS}),
        expect_countries=frozenset({"HN"}),
        history=[_CONTEO_CR],
        regression=(
            "El turno anterior filtra 'CR'. Si el historial arrastra ese pais, "
            "la respuesta sale con datos del pais equivocado -- el peor fallo "
            "posible aqui, porque parece correcta."
        ),
    ),
]
