from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from mira_api.audit.outcomes import Outcome

#: Unicas relaciones que el servicio puede consultar. El rol de base de datos
#: (mira_query) solo tiene USAGE sobre el esquema `query`, asi que esta lista es la
#: segunda linea de defensa, no la primera.
#:
#: Refleja el esquema query.* real de MIRA-ETL (sql/002_indexes_and_views.sql,
#: verificado contra produccion 2026-08-15): un proceso tiene adjudicaciones
#: (awards), y cada adjudicacion tiene proveedores e items -- el monto adjudicado
#: vive en la adjudicacion, no en el proceso. "Cuanto se gasto" siempre requiere
#: join v_process -> v_awards -> v_award_suppliers.
#:
#: Dos vistas quedan deliberadamente fuera:
#:
#: query.v_duplicate_hints no existe en la base (verificado contra produccion
#: 2026-08-20) y no va a existir: decision de producto (2026-08-15) de no
#: senalar posibles duplicados entre entidades parecidas, porque dos nombres
#: parecidos pueden ser entidades distintas a proposito.
#:
#: query.v_coverage si existe, pero son metadatos de las corridas del ETL, no
#: datos de contrataciones. La cobertura se sirve por GET /coverage, que lee
#: agregados exactos de web.coverage_sources -- no tiene por que pasar por el
#: modelo. Describirsela ademas garantizaba el peor final posible: el modelo
#: generaba SQL contra ella, el validador lo rechazaba, y se gastaban los 3
#: intentos para terminar en REJECTED_SQL_RELATION.
ALLOWED_RELATIONS: frozenset[str] = frozenset(
    {
        "query.v_process",
        "query.v_buyers",
        "query.v_suppliers",
        "query.v_process_buyers",
        "query.v_items",
        "query.v_awards",
        "query.v_award_items",
        "query.v_award_suppliers",
    }
)

#: Esquemas que jamas deben aparecer en una consulta del servicio.
FORBIDDEN_SCHEMAS: frozenset[str] = frozenset(
    {"mart", "raw", "staging", "audit", "analytics", "pg_catalog", "information_schema"}
)

#: Funciones que permiten leer archivos, abrir conexiones o alterar la sesion.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_connect",
        "pg_sleep",
        "query_to_xml",
        "set_config",
        "current_setting",
        "pg_terminate_backend",
        "pg_cancel_backend",
    }
)


class SqlRejected(Exception):
    """El SQL no paso la validacion. Nunca llega a la base de datos."""

    def __init__(self, outcome: Outcome, rule: str, detail: str = "") -> None:
        super().__init__(f"{outcome}: {rule} {detail}".strip())
        self.outcome = outcome
        self.rule = rule
        self.detail = detail


@dataclass(frozen=True)
class ValidatedSql:
    sql: str
    relations: frozenset[str]
    limit_injected: bool
    #: Si hubo que agregar NULLS LAST a algun ORDER BY descendente.
    nulls_last_injected: bool = False


#: Vistas que traen country_code. Si el SQL las toca, tiene que filtrar por
#: pais -- v_awards/v_items/v_process_buyers/v_award_* no tienen la columna,
#: se llega a ellas por process_id/award_id ya acotado via v_process.
_COUNTRY_SCOPED_VIEWS = frozenset({"query.v_process", "query.v_buyers", "query.v_suppliers"})


def validate(sql: str, *, max_rows: int, countries: list[str]) -> ValidatedSql:
    """Valida SQL sobre el arbol sintactico, nunca con expresiones regulares.

    Devuelve el SQL reescrito con LIMIT forzado, o levanta SqlRejected con el codigo
    de la taxonomia que corresponde. El codigo se registra en analytics.query_attempt
    para poder medir la tasa de rechazo del validador, que es el detector de
    regresiones mas rapido que tiene el servicio.

    `countries` son los paises que el usuario realmente pidio -- el modelo nunca
    decide por su cuenta que paises son validos, solo puede filtrar dentro de esa
    lista o el SQL se rechaza (REJECTED_SQL_COUNTRY_SCOPE) y se reintenta.
    """
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as err:  # noqa: BLE001 - sqlglot levanta varios tipos
        raise SqlRejected(Outcome.REJECTED_SQL_PARSE, "parse_error", str(err)) from err

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlRejected(
            Outcome.REJECTED_SQL_NOT_SELECT,
            "multi_statement",
            f"se recibieron {len(statements)} sentencias",
        )

    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise SqlRejected(
            Outcome.REJECTED_SQL_NOT_SELECT, "not_select", type(tree).__name__
        )

    for cte in tree.find_all(exp.With):
        if cte.args.get("recursive"):
            raise SqlRejected(Outcome.REJECTED_SQL_NOT_SELECT, "recursive_cte")

    relations = _collect_relations(tree)
    for relation in relations:
        schema = relation.split(".")[0] if "." in relation else ""
        if schema in FORBIDDEN_SCHEMAS:
            raise SqlRejected(Outcome.REJECTED_SQL_RELATION, "forbidden_schema", relation)
        if relation not in ALLOWED_RELATIONS:
            raise SqlRejected(Outcome.REJECTED_SQL_RELATION, "not_allowlisted", relation)

    for func in tree.find_all(exp.Anonymous):
        name = str(func.this).lower()
        if name in FORBIDDEN_FUNCTIONS:
            raise SqlRejected(Outcome.REJECTED_SQL_FUNCTION, "forbidden_function", name)

    _check_no_money_aggregation(tree)
    _check_no_money_arithmetic(tree)

    if relations & _COUNTRY_SCOPED_VIEWS:
        _check_country_scope(tree, countries)

    limit_injected = _enforce_limit(tree, max_rows)
    nulls_last_injected = _enforce_nulls_last(tree)
    return ValidatedSql(
        sql=tree.sql(dialect="postgres"),
        relations=frozenset(relations),
        limit_injected=limit_injected,
        nulls_last_injected=nulls_last_injected,
    )


#: Columnas de dinero. No hay una columna de monto unificada, y es a proposito:
#: cada monto viene con su moneda al lado.
_MONEY_COLUMNS = frozenset({"awarded_amount", "estimated_amount"})

#: Agregaciones que producen un numero que no existe en ningun renglon.
#: MIN y MAX quedan fuera a proposito: devuelven un valor que si esta en los
#: datos, no uno calculado, asi que "la adjudicacion mas cara" sigue andando.
_MONEY_AGGREGATES = (exp.Sum, exp.Avg)


#: Operaciones que producen un valor de dinero que no es el que esta guardado.
_MONEY_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div)


def _check_no_money_arithmetic(tree: exp.Expression) -> None:
    """Prohibe sumar, restar, multiplicar o dividir un monto (decision de
    producto, verificado en produccion: pedir "el precio en dolares a 8Q el
    dolar" hizo que el modelo generara `awarded_amount / 8`).

    Esto es distinto de _check_no_money_aggregation: SUM/AVG combinan varias
    filas en una, pero `awarded_amount / 8` es una division simple que no
    agrega nada -- pasa esa otra regla sin problema. Y es mas traicionera:
    el resultado queda como una celda mas en la tabla, asi que el verificador
    anti-alucinacion (que solo compara contra las celdas del resultado) lo da
    por verificado. Un tipo de cambio inventado por el modelo se ve entonces
    identico a un dato real.

    No hay dolarizacion todavia -- decision de producto. Todo monto se
    muestra tal cual esta guardado, en su propia moneda, sin excepcion.
    """
    for tipo in _MONEY_ARITHMETIC:
        for operacion in tree.find_all(tipo):
            for columna in operacion.find_all(exp.Column):
                if columna.name.lower() in _MONEY_COLUMNS:
                    raise SqlRejected(
                        Outcome.REJECTED_SQL_FUNCTION,
                        "money_arithmetic",
                        f"no se puede operar sobre {columna.name} con {tipo.__name__.lower()}: "
                        "los montos se muestran tal cual estan guardados, en su propia moneda, "
                        "sin sumar, restar, multiplicar ni dividir. No hay conversion de moneda "
                        "disponible todavia -- si preguntan por otra moneda, dilo y muestra el "
                        "monto original.",
                    )


def _check_no_money_aggregation(tree: exp.Expression) -> None:
    """Prohibe sumar o promediar dinero (decision de producto, 2026-08-21).

    Un total equivocado es peor que ningun total: se ve autoritativo, se cita,
    y nadie lo vuelve a revisar. Y aqui las sumas son especialmente
    traicioneras porque los montos vienen en monedas distintas -- Costa Rica
    mezcla CRC, USD y EUR en la misma tabla, asi que un SUM() sin cuidado
    suma colones con dolares y produce una cifra sin significado.

    La regla vive en el prompt tambien, pero un prompt es un ruego: el modelo
    puede ignorarlo. Esto es lo que lo hace imposible. Al rechazarse, el
    reintento le devuelve el motivo y el modelo genera la consulta sin la
    suma, mostrando las filas para que la persona sume si quiere.
    """
    for tipo in _MONEY_AGGREGATES:
        for agregacion in tree.find_all(tipo):
            for columna in agregacion.find_all(exp.Column):
                if columna.name.lower() in _MONEY_COLUMNS:
                    raise SqlRejected(
                        Outcome.REJECTED_SQL_FUNCTION,
                        "money_aggregation",
                        f"no se puede {tipo.__name__.upper()}({columna.name}): los montos se "
                        "muestran fila por fila, sin totalizar. Devuelve las filas con su "
                        "monto y su moneda.",
                    )


def _collect_relations(tree: exp.Expression) -> set[str]:
    """Nombres cualificados de toda tabla o vista referenciada, sin alias de CTE."""
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    relations: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if name in cte_names:
            continue
        db = (table.db or "").lower()
        relations.add(f"{db}.{name}" if db else name)
    return relations


def _check_country_scope(tree: exp.Expression, countries: list[str]) -> None:
    """Exige un predicado sobre country_code (=, IN, = ANY(...)) cuyos valores
    sean subconjunto de `countries`. No importa el alias de tabla -- solo el
    nombre de columna, porque el modelo no siempre califica con alias."""
    values, found = _country_code_predicate_values(tree)
    if not found:
        raise SqlRejected(
            Outcome.REJECTED_SQL_COUNTRY_SCOPE,
            "missing_country_filter",
            f"la consulta debe filtrar country_code dentro de {sorted(countries)}",
        )
    allowed = {c.upper() for c in countries}
    extra = {v.upper() for v in values} - allowed
    if extra:
        raise SqlRejected(
            Outcome.REJECTED_SQL_COUNTRY_SCOPE,
            "country_not_allowed",
            f"filtra {sorted(extra)}, fuera de lo pedido {sorted(countries)}",
        )


def _country_code_predicate_values(tree: exp.Expression) -> tuple[set[str], bool]:
    values: set[str] = set()
    found = False

    for eq in tree.find_all(exp.EQ):
        if _is_country_code_column(eq.this):
            found = True
            values |= _literal_values(eq.expression)
        elif _is_country_code_column(eq.expression):
            found = True
            values |= _literal_values(eq.this)

    for is_in in tree.find_all(exp.In):
        if not _is_country_code_column(is_in.this):
            continue
        if is_in.args.get("query") is not None:
            # `country_code IN (SELECT ...)`: no hay literales que extraer, no
            # se puede verificar que el subquery solo devuelva paises
            # permitidos. Se rechaza directo en vez de dejarlo pasar sin
            # verificar.
            raise SqlRejected(
                Outcome.REJECTED_SQL_COUNTRY_SCOPE,
                "country_filter_not_verifiable",
                "el filtro de country_code debe ser una lista de literales, no una subconsulta",
            )
        found = True
        for item in is_in.expressions:
            values |= _literal_values(item)

    return values, found


def _is_country_code_column(node: exp.Expression) -> bool:
    return isinstance(node, exp.Column) and node.name.lower() == "country_code"


def _literal_values(node: exp.Expression) -> set[str]:
    if isinstance(node, exp.Literal) and node.is_string:
        return {node.this}
    if isinstance(node, exp.Array):
        result: set[str] = set()
        for item in node.expressions:
            result |= _literal_values(item)
        return result
    if isinstance(node, exp.Any):
        return _literal_values(node.this)
    if isinstance(node, exp.Paren):
        return _literal_values(node.this)
    return set()


def _enforce_nulls_last(tree: exp.Expression) -> bool:
    """Fuerza NULLS LAST en todo ORDER BY descendente.

    PostgreSQL ordena los nulos ARRIBA en un DESC. Verificado en produccion
    el 2026-08-21: "los 6 procesos mas recientes" con ORDER BY
    publication_date DESC devolvio seis filas sin fecha ninguna. No fallo
    nada -- la respuesta traia seis procesos reales, con su id y su titulo,
    y quien la leyera no tenia como saber que no eran los mas recientes sino
    justo los que no tienen el dato.

    Se impone en el arbol, como el LIMIT, en vez de solo pedirselo al prompt:
    el prompt lo cumple casi siempre, y "casi siempre" en una respuesta que
    se ve bien equivocada es peor que un error visible.
    """
    corregidos = False
    for orden in tree.find_all(exp.Ordered):
        if orden.args.get("desc") and orden.args.get("nulls_first") is not False:
            orden.set("nulls_first", False)
            corregidos = True
    return corregidos


def _enforce_limit(tree: exp.Select, max_rows: int) -> bool:
    """Impone el LIMIT en el arbol. Nunca se confia en que el modelo lo puso."""
    limit = tree.args.get("limit")
    if limit is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return True
    try:
        current = int(limit.expression.name)
    except (AttributeError, ValueError):
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return True
    if current > max_rows:
        limit.set("expression", exp.Literal.number(max_rows))
        return True
    return False
