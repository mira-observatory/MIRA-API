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
#: query.v_coverage TODAVIA NO EXISTE en MIRA-ETL (bloquea distinguir "cero
#: real" de "cero por falta de datos"). Agregarla aqui cuando exista alla, no
#: antes. No se pide query.v_duplicate_hints: decision de producto
#: (2026-08-15) de no senalar posibles duplicados entre entidades parecidas.
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


def validate(sql: str, *, max_rows: int) -> ValidatedSql:
    """Valida SQL sobre el arbol sintactico, nunca con expresiones regulares.

    Devuelve el SQL reescrito con LIMIT forzado, o levanta SqlRejected con el codigo
    de la taxonomia que corresponde. El codigo se registra en analytics.query_attempt
    para poder medir la tasa de rechazo del validador, que es el detector de
    regresiones mas rapido que tiene el servicio.
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

    limit_injected = _enforce_limit(tree, max_rows)
    return ValidatedSql(
        sql=tree.sql(dialect="postgres"),
        relations=frozenset(relations),
        limit_injected=limit_injected,
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
