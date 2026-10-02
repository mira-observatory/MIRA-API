"""Conservative intent checks for requested rankings (Spanish/English)."""

from __future__ import annotations

import re
import unicodedata

from sqlglot import exp


def normalise(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def requested_row_limit(question: str) -> int | None:
    """Read a requested result size, without treating dates/filters as limits."""
    words = {
        "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
        "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    number = rf"(?P<count>[1-9]\d*|{'|'.join(words)})\b"
    entities = (
        r"(?:instituciones|proveedores|empresas|procesos|contratos|adjudicaciones|"
        r"compras|productos|resultados|filas|institutions|suppliers|companies|"
        r"processes|contracts|awards|purchases|products|results|rows)\b"
    )
    patterns = (
        rf"\btop\s*[-:]?\s*{number}",
        rf"\b(?:primeros|primeras|first)\s+{number}(?=\s+{entities}|\s*[,.;!?]|\s*$)",
        rf"\b(?:los|las|the)\s+{number}\s+(?:{entities}|"
        r"(?:mas|menos|mayores|menores|mejores|peores|most|least|highest|lowest)\b)",
        rf"\b(?:muestra|mostra|dame|lista|show|list|give me)\s+"
        rf"(?:(?:solo|only)\s+)?{number}\s+{entities}",
    )
    text = normalise(question)
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            count = match.group("count")
            return int(count) if count.isdigit() else words[count]
    return None


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
    """Require a monetary ranking attached to the supplier being ranked.

    Mentioning supplier names/counts alongside an award amount does not ask
    for cumulative supplier earnings. Global keyword co-occurrence used to
    reject award lists and buyer rankings that include those extra columns.
    """
    text = normalise(question)
    individual = re.search(
        r"\b(en (?:una|un|una sola|un solo) (?:adjudicacion|contrato)|"
        r"(?:single|one) (?:award|contract)|(?:adjudicacion|contrato) (?:mas|de mayor))\b",
        text,
    )
    supplier = r"(?:proveedor(?:es)?|empresa(?:s)?|supplier(?:s)?|compan(?:y|ies))"
    criterion = (
        r"(?:mas\s+(?:dinero|monto|montos)|mayor\s+(?:monto|dinero|acumulado)|"
        r"most\s+money|highest\s+(?:amount|earnings)|"
        r"(?:monto\s+)?(?:total|acumulado|acumulados)|total\s+(?:amount|earnings))"
    )
    linked_ranking = re.search(
        rf"\b{supplier}\b[^,.;:!?]{{0,80}}\b{criterion}\b", text,
    ) or re.search(
        rf"\b{criterion}\b[^,.;:!?]{{0,50}}\b(?:por|del|de los|per|by)\s+{supplier}\b",
        text,
    )
    return bool(
        not individual
        and linked_ranking
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
