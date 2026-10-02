"""Keep a conversation's date filter when a follow-up does not change it."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from mira_api.nlq.ranking import normalise

_DATE_VIEWS = {
    "award_date": {"v_awards", "v_awards_all"},
    "publication_date": {"v_process"},
}
_MONTHS = (
    "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|"
    "noviembre|diciembre|january|february|march|april|may|june|july|august|"
    "september|october|november|december"
)


@dataclass(frozen=True)
class PeriodScope:
    column: str
    start: dt.date
    end: dt.date

    def instruction(self) -> str:
        return (
            "Periodo heredado de la conversacion (obligatorio): "
            f"{self.column} >= '{self.start.isoformat()}' AND "
            f"{self.column} < '{self.end.isoformat()}'. "
            "Conserva este periodo y la misma columna de fecha al aplicar el nuevo filtro; "
            "no amplies la busqueda a otros meses o anios."
        )


def changes_period(question: str) -> bool:
    """A new period or an explicit reset overrides temporal inheritance."""
    text = normalise(question)
    # A four-digit amount is not a requested year.
    text = re.sub(r"\b(?:monto|valor|amount|value|cantidad)\s+(?:(?:de|of)\s+)?\d{4}\b", "", text)
    return bool(re.search(
        rf"\b(?:{_MONTHS})\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b|"
        r"\b(?:en|de|del|durante|para|in|during|year|ano|anio)\s+"
        r"(?:(?:el|the)\s+)?(?:(?:ano|anio|year)\s+)?(?:19|20)\d{2}\b|"
        r"^\W*(?:(?:y|and)\s+)?(?:19|20)\d{2}\W*$|"
        r"\b(?:hoy|ayer|today|yesterday|actualmente)\b|"
        r"\b(?:ultimo[s]?|ultima[s]?|proximo[s]?|proxima[s]?|last|past|next|previous)\s+"
        r"(?:\d+\s+)?(?:dia[s]?|mes(?:es)?|ano[s]?|anio[s]?|semana[s]?|"
        r"day[s]?|month[s]?|year[s]?|week[s]?)\b|"
        r"\b(?:primer|segundo|tercer|cuarto|first|second|third|fourth)\s+"
        r"(?:trimestre|quarter)\b|\bq[1-4]\b|"
        r"\b(?:todo el historial|todos los (?:periodos|anos|anios|meses)|"
        r"sin (?:filtro|limite|restriccion) (?:de |por )?fecha[s]?|"
        r"all time|entire history|all (?:years|months|periods)|"
        r"without (?:a )?date (?:filter|limit)|"
        r"otra pregunta|nueva consulta|cambiando de tema|new question|different question)\b",
        text,
    ))


def _column(node: exp.Expression) -> exp.Column | None:
    while isinstance(node, (exp.Cast, exp.Paren, exp.Date)):
        node = node.this
    return node if isinstance(node, exp.Column) and node.name.lower() in _DATE_VIEWS else None


def _date(node: exp.Expression) -> dt.date | None:
    while isinstance(node, (exp.Cast, exp.Paren, exp.Date)):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        try:
            return dt.date.fromisoformat(node.this)
        except ValueError:
            try:
                timestamp = dt.datetime.fromisoformat(node.this)
                if timestamp.time() == dt.time():
                    return timestamp.date()
            except ValueError:
                pass
    return None


def _terms(node: exp.Expression) -> Iterator[exp.Expression]:
    if isinstance(node, (exp.Where, exp.Paren)):
        yield from _terms(node.this)
    elif isinstance(node, exp.And):
        yield from _terms(node.this)
        yield from _terms(node.expression)
    else:
        # An OR, NOT or subquery cannot establish a mandatory outer filter.
        yield node


def _periods(scope: Scope) -> dict[tuple[str, str], PeriodScope]:
    where = scope.expression.args.get("where")
    if where is None:
        return {}
    lower: dict[tuple[str, str], dt.date] = {}
    upper: dict[tuple[str, str], dt.date] = {}
    parts: dict[tuple[str, str], dict[str, int]] = {}
    for term in _terms(where):
        if isinstance(term, exp.EQ) and isinstance(term.this, exp.Extract):
            column = _column(term.this.expression)
            value = term.expression
            if column is not None and isinstance(value, exp.Literal) and value.is_int:
                parts.setdefault((column.table, column.name.lower()), {})[
                    term.this.this.name.lower()
                ] = int(value.this)
        elif isinstance(term, exp.EQ) and isinstance(term.this, exp.TimestampTrunc):
            column, value = _column(term.this.this), _date(term.expression)
            unit = term.this.args.get("unit")
            if column is not None and value is not None and isinstance(unit, exp.Expression):
                if unit.name.lower() == "month" and value.day == 1:
                    parts[(column.table, column.name.lower())] = {
                        "year": value.year, "month": value.month,
                    }
                elif unit.name.lower() == "year" and value.month == value.day == 1:
                    parts[(column.table, column.name.lower())] = {"year": value.year}
        elif isinstance(term, exp.Between):
            column = _column(term.this)
            start, end = _date(term.args["low"]), _date(term.args["high"])
            if column is not None and start is not None and end is not None and end < dt.date.max:
                key = (column.table, column.name.lower())
                lower[key] = max(lower.get(key, start), start)
                upper[key] = min(upper.get(key, dt.date.max), end + dt.timedelta(days=1))
        elif isinstance(term, (exp.GTE, exp.LT, exp.LTE)):
            column, value = _column(term.this), _date(term.expression)
            if column is not None and value is not None:
                key = (column.table, column.name.lower())
                if isinstance(term, exp.GTE):
                    lower[key] = max(lower.get(key, value), value)
                elif isinstance(term, exp.LT):
                    upper[key] = min(upper.get(key, value), value)
                elif value < dt.date.max:
                    upper[key] = min(upper.get(key, dt.date.max), value + dt.timedelta(days=1))
    for key, extracted in parts.items():
        year, month = extracted.get("year"), extracted.get("month")
        if year is None or (month is not None and not 1 <= month <= 12):
            continue
        try:
            start = dt.date(year, month or 1, 1)
            end = (dt.date(year + 1, 1, 1) if month is None or month == 12
                   else dt.date(year, month + 1, 1))
        except ValueError:
            continue
        lower[key] = max(lower.get(key, start), start)
        upper[key] = min(upper.get(key, end), end)
    return {key: PeriodScope(key[1], start, upper[key]) for key, start in lower.items()
            if key in upper and start < upper[key]}


def _used_scopes(scope: Scope, seen: set[int] | None = None) -> Iterator[Scope]:
    seen = set() if seen is None else seen
    if id(scope) in seen:
        return
    seen.add(id(scope))
    yield scope
    for _, source in scope.selected_sources.values():
        if isinstance(source, Scope):
            yield from _used_scopes(source, seen)


def extract_period(sql: str) -> PeriodScope | None:
    """Read date predicates in used SELECTs, never arbitrary date literals."""
    try:
        root = build_scope(sqlglot.parse_one(sql, dialect="postgres"))
        if root is None:
            return None
        periods = {period for scope in _used_scopes(root) for period in _periods(scope).values()}
        return next(iter(periods)) if len(periods) == 1 else None
    except (ValueError, sqlglot.errors.SqlglotError):
        return None


def preserves_period(sql: str, expected: PeriodScope) -> bool:
    """Require the same date filter on the data sources actually being read."""
    root = build_scope(sqlglot.parse_one(sql, dialect="postgres"))
    if root is None:
        return False

    def check(scope: Scope, inherited: bool = False) -> tuple[bool, bool]:
        periods = _periods(scope)
        found = inherited
        for alias, (_, source) in scope.selected_sources.items():
            restricted = periods.get((alias, expected.column), periods.get(("", expected.column)))
            if isinstance(source, exp.Table) and source.name in _DATE_VIEWS[expected.column]:
                if not inherited and restricted != expected:
                    return False, False
                found = True
            elif isinstance(source, Scope):
                valid, child_found = check(source, inherited or restricted == expected)
                if not valid:
                    return False, False
                found |= child_found
        return True, found

    valid, found = check(root)
    return valid and found
