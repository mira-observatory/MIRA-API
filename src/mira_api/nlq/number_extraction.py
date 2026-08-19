from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

#: Numero con separadores de miles/decimales (formato latino "1.234,56" o
#: formato en-US "1,234.56"), opcionalmente seguido de "millon(es)", "mil" o
#: "%". No intenta ser un parser numerico general -- solo lo suficiente para
#: verificar que un texto generado no invente cifras.
_NUMBER_RE = re.compile(
    r"(?<![\w.,])"
    r"(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"\s?"
    r"(?P<suffix>mill[oó]n(?:es)?|mil(?!l)|%)?",
    re.IGNORECASE,
)

_MULTIPLIERS = {
    "millon": 1_000_000,
    "millón": 1_000_000,
    "millones": 1_000_000,
    "mil": 1_000,
}


def _normalise_numeric_literal(raw: str) -> float | None:
    """Decide cual separador es el decimal cuando aparecen los dos ('.' y ',')
    -- el ultimo que aparece en el texto. Si solo aparece uno, se asume
    separador de miles cuando el grupo final tiene exactamente 3 digitos y hay
    mas de un grupo (p.ej. "1.234" o "1,234"); si no, se asume decimal
    (p.ej. "45,5" o "45.5")."""
    has_dot = "." in raw
    has_comma = "," in raw
    try:
        if has_dot and has_comma:
            decimal_sep = "," if raw.rindex(",") > raw.rindex(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            cleaned = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
            return float(cleaned)
        if has_comma or has_dot:
            sep = "," if has_comma else "."
            groups = raw.split(sep)
            if len(groups) > 1 and len(groups[-1]) == 3:
                return float(raw.replace(sep, ""))
            return float(raw.replace(sep, "."))
        return float(raw)
    except ValueError:
        return None


def extract_numbers(text: str) -> set[float]:
    """Todos los numeros que aparecen en un texto en espanol, ya normalizados
    a float (con "millones"/"mil" aplicados). No distingue de donde vino cada
    uno -- eso lo hace verify_narrative comparandolos contra los datos reales."""
    found: set[float] = set()
    for match in _NUMBER_RE.finditer(text):
        value = _normalise_numeric_literal(match.group("num"))
        if value is None:
            continue
        suffix = (match.group("suffix") or "").lower()
        if suffix in _MULTIPLIERS:
            value *= _MULTIPLIERS[suffix]
        # "%" no cambia el valor numerico -- 45% se compara como 45.
        found.add(value)
    return found


def _cell_to_floats(value: Any) -> set[float]:
    if isinstance(value, bool):
        return set()
    if isinstance(value, int | float | Decimal):
        return {round(float(value), 6)}
    return set()


def allowed_values_from_rows(rows: list[dict[str, Any]]) -> set[float]:
    """Todo numero que existe de verdad en las celdas del resultado --
    incluyendo variantes redondeadas a 0/1/2 decimales, porque una redaccion
    razonable puede escribir 7991.5 como "7,991.50" o "7,992"."""
    allowed: set[float] = set()
    for row in rows:
        for value in row.values():
            for number in _cell_to_floats(value):
                allowed.add(number)
                for digits in (0, 1, 2):
                    allowed.add(round(number, digits))
    return allowed


def find_unverified_numbers(
    narrative: str, rows: list[dict[str, Any]], *, tolerance: float = 0.01
) -> list[str]:
    """Numeros del texto que no aparecen (ni redondeados) en ninguna celda del
    resultado. Devuelve la representacion de texto original de cada uno
    invalido, no el float normalizado -- es lo que se le muestra de vuelta al
    modelo para que se corrija."""
    allowed = allowed_values_from_rows(rows)
    invalid: list[str] = []
    for match in _NUMBER_RE.finditer(narrative):
        value = _normalise_numeric_literal(match.group("num"))
        if value is None:
            continue
        suffix = (match.group("suffix") or "").lower()
        if suffix in _MULTIPLIERS:
            value *= _MULTIPLIERS[suffix]
        if not any(abs(value - a) <= tolerance for a in allowed):
            invalid.append(match.group(0).strip())
    return invalid
