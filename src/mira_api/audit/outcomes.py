from __future__ import annotations

from enum import StrEnum


class Family(StrEnum):
    """Familia de un resultado. Se deriva del codigo, nunca se asigna a mano.

    La distincion entre RECHAZO y FALLO no es cosmetica: un rechazo es el sistema
    funcionando (decidio no ejecutar algo que no debia), un fallo es el sistema roto.
    Mezclarlos en una sola columna de "error" hace imposible saber si una subida en
    la grafica es buena noticia o mala.
    """

    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    THROTTLED = "THROTTLED"


class Outcome(StrEnum):
    """Taxonomia cerrada. Toda consulta termina exactamente en uno de estos codigos.

    Un registro solo sirve para mejorar si las causas de rechazo y de fallo estan
    codificadas. Si se guardan como texto libre, se acumulan filas que nadie puede
    agrupar y el registro degenera en vigilancia sin aprendizaje.
    """

    # --- exito -------------------------------------------------------------
    OK = "OK"
    OK_ZERO_ROWS = "OK_ZERO_ROWS"
    # Datos entregados, pero la redaccion se reemplazo por una plantilla porque el
    # verificador hallo numeros que no estaban en el resultado. Metrica bloqueante.
    OK_DEGRADED_NARRATIVE = "OK_DEGRADED_NARRATIVE"

    # --- rechazo: el sistema decidio no ejecutar ---------------------------
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    REJECTED_ENTITY_NOT_FOUND = "REJECTED_ENTITY_NOT_FOUND"
    # No es un error: es el comportamiento deseado ante nombres parecidos.
    REJECTED_ENTITY_AMBIGUOUS = "REJECTED_ENTITY_AMBIGUOUS"
    REJECTED_SQL_PARSE = "REJECTED_SQL_PARSE"
    REJECTED_SQL_NOT_SELECT = "REJECTED_SQL_NOT_SELECT"
    REJECTED_SQL_RELATION = "REJECTED_SQL_RELATION"
    REJECTED_SQL_FUNCTION = "REJECTED_SQL_FUNCTION"
    REJECTED_SQL_COST = "REJECTED_SQL_COST"

    # --- fallo: se intento y se rompio -------------------------------------
    FAILED_DB_TIMEOUT = "FAILED_DB_TIMEOUT"
    FAILED_DB_ERROR = "FAILED_DB_ERROR"
    FAILED_LLM_ERROR = "FAILED_LLM_ERROR"

    # --- limitacion --------------------------------------------------------
    THROTTLED_QUOTA = "THROTTLED_QUOTA"
    THROTTLED_BUDGET = "THROTTLED_BUDGET"

    @property
    def family(self) -> Family:
        if self.value.startswith(("OK", "OUT_OF_SCOPE")):
            return Family.SUCCESS if self.value.startswith("OK") else Family.REJECTED
        if self.value.startswith("REJECTED_"):
            return Family.REJECTED
        if self.value.startswith("FAILED_"):
            return Family.FAILED
        return Family.THROTTLED

    @property
    def is_answered(self) -> bool:
        """Si el usuario recibio datos, aunque la redaccion se haya degradado."""
        return self.family is Family.SUCCESS


#: Codigos que alimentan `analytics.v_unanswered`, la vista que es literalmente el
#: backlog del producto. No hay catalogo: el registro de estas preguntas es la
#: materia prima con la que despues se construira el catalogo de consultas
#: parametrizadas (seccion 1.4).
UNANSWERED_OUTCOMES: frozenset[Outcome] = frozenset(
    {
        Outcome.OUT_OF_SCOPE,
        Outcome.REJECTED_ENTITY_NOT_FOUND,
    }
)

#: Codigos que deben ser cero en operacion normal. Distintos de cero significan un
#: intento de abuso o un prompt degradado, y disparan alerta.
MUST_BE_ZERO: frozenset[Outcome] = frozenset(
    {
        Outcome.OK_DEGRADED_NARRATIVE,
        Outcome.REJECTED_SQL_NOT_SELECT,
        Outcome.REJECTED_SQL_RELATION,
        Outcome.REJECTED_SQL_FUNCTION,
    }
)
