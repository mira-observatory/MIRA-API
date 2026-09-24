"""Conservative intent checks for explicit singular rankings (Spanish/English)."""

from __future__ import annotations

import re
import unicodedata

from sqlglot import exp


def normalise(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def asks_single_winner(question: str) -> bool:
    text = normalise(question)
    if re.search(r"\b(cada|por pais|por moneda|por mes|por ano|each|per)\b", text):
        return False
    singular = re.search(
        r"\b(?:cual es (?:el|la)|que|which|what) "
        r"(?:proveedor|institucion|empresa|adjudicacion|contrato|proceso|compra|"
        r"supplier|institution|company|award|contract)\b", text,
    )
    return bool(
        singular and re.search(r"\b(mas|menos|mayor|menor|most|least|highest|lowest)\b", text)
    )


def asks_supplier_total(question: str) -> bool:
    text = normalise(question)
    individual = re.search(
        r"\b(en (?:una|un|una sola|un solo) (?:adjudicacion|contrato)|"
        r"(?:single|one) (?:award|contract)|(?:adjudicacion|contrato) (?:mas|de mayor))\b",
        text,
    )
    return bool(
        not individual
        and re.search(r"\b(proveedor(?:es)?|empresa(?:s)?|supplier(?:s)?|compan(?:y|ies))\b", text)
        and re.search(r"\b(dinero|monto|montos|money|amount|earned|acumulado|acumulados)\b", text)
        and re.search(r"\b(mas|mayor|most|highest|total|acumulado|acumulados)\b", text)
    )


def single_currency(tree: exp.Select) -> bool:
    """Only an obligatory literal equality counts; OR/NOT are not evidence."""
    def terms(node: exp.Expression) -> list[exp.Expression]:
        if isinstance(node, (exp.Where, exp.Paren)):
            return terms(node.this)
        if isinstance(node, exp.And):
            return terms(node.this) + terms(node.expression)
        return [node]

    where = tree.args.get("where")
    if where is None:
        return False
    for condition in terms(where):
        if not isinstance(condition, exp.EQ):
            continue
        for column, literal in ((condition.this, condition.expression),
                                (condition.expression, condition.this)):
            if (isinstance(column, exp.Column) and column.name == "currency_code"
                    and isinstance(literal, exp.Literal) and literal.is_string
                    and re.fullmatch(r"[A-Z]{3}", literal.this)):
                return True
    return False


def winners_per_currency(tree: exp.Select) -> bool:
    distinct = tree.args.get("distinct")
    on = distinct.args.get("on") if distinct else None
    columns = on.expressions if isinstance(on, exp.Tuple) else []
    return bool(columns) and all(isinstance(c, exp.Column) for c in columns) and {
        c.name for c in columns
    } == {"currency_code"}
