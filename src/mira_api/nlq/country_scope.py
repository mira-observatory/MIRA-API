"""Resolve the countries being asked about within the available selection."""

from __future__ import annotations

import re
import unicodedata

_COUNTRY_NAMES = {
    "GT": "guatemala", "CR": "costa rica", "HN": "honduras", "NI": "nicaragua",
    "SV": "el salvador", "PA": "panama",
}


def resolve_country_scope(
    question: str, available: list[str], previous: list[str] | None = None,
) -> list[str]:
    """Explicit names narrow the selection; unnamed follow-ups inherit scope.

    Do not silently drop a named country outside the selection: leave the
    original scope so SQL validation can reject an unavailable country.
    """
    available = list(dict.fromkeys(c.upper() for c in available))
    text = " ".join("".join(
        c for c in unicodedata.normalize("NFKD", question.lower())
        if not unicodedata.combining(c)
    ).split())
    if re.search(
        r"\b(?:todos los paises|todos estos paises|all (?:the )?countries|"
        r"centroamerica|central america)\b", text,
    ):
        return available
    named = {code for code, name in _COUNTRY_NAMES.items()
             if re.search(rf"\b{name}\b", text)}
    if named:
        return [c for c in available if c in named] if named <= set(available) else available
    if previous:
        previous = list(dict.fromkeys(c.upper() for c in previous))
        if set(previous) <= set(available):
            return previous
    return available
