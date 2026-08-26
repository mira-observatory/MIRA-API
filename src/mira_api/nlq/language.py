"""En que idioma esta escrita la pregunta.

Deliberadamente sin dependencia externa y deliberadamente binario (es/en): el
producto es para Centroamerica y el espanol es el default. Lo unico que se
decide aqui es si una pregunta viene claramente en ingles; ante la duda, gana
el espanol, porque equivocarse hacia el ingles con un usuario hispanohablante
es mucho peor que lo contrario.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

Language = Literal["es", "en"]

#: Palabras funcionales que casi no aparecen en el otro idioma. No se incluyen
#: las que se escriben igual o casi ("no", "in"/"sin", "me", "as"), ni terminos
#: de dominio que viajan entre idiomas ("total", "contrato"/"contract").
_SPANISH_MARKERS = frozenset(
    {
        "que", "cual", "cuales", "cuanto", "cuantos", "cuanta", "cuantas",
        "como", "donde", "cuando", "quien", "quienes", "por", "para", "del",
        "las", "los", "una", "unos", "unas", "es", "son", "esta", "estan",
        "hay", "mas", "menos", "muestra", "muestrame", "dame", "listame",
        "ensename", "busca", "buscame", "todos", "todas", "entre", "desde",
        "hasta", "sobre", "segun", "tiene", "tienen", "fue", "fueron",
        "compro", "compraron", "gasto", "gastaron", "vendido", "caras",
        "caro", "mayor", "menor", "ultimo", "ultimos", "ultima", "ultimas",
        "primer", "primeros", "empresa", "empresas", "proveedor", "proveedores",
        "comprador", "compradores", "adjudicacion", "adjudicaciones",
        "contrataciones", "monto", "montos", "anio", "ano", "mes", "pais",
        "paises", "y", "o", "el", "la", "de", "en", "un",
    }
)

_ENGLISH_MARKERS = frozenset(
    {
        "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
        "the", "and", "or", "of", "to", "for", "from", "with", "without",
        "show", "list", "give", "find", "tell", "get", "display",
        "is", "are", "was", "were", "has", "have", "had", "do", "does", "did",
        "many", "much", "most", "least", "more", "less", "top", "biggest",
        "largest", "smallest", "highest", "lowest", "expensive", "cheapest",
        "all", "any", "between", "during", "since", "until", "about",
        "company", "companies", "supplier", "suppliers", "buyer", "buyers",
        "award", "awards", "awarded", "contract", "contracts", "procurement",
        "amount", "amounts", "year", "month", "country", "countries",
        "bought", "purchased", "spent", "sold", "me", "my", "in", "on", "by",
    }
)

_WORD_PATTERN = re.compile(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+")


def _strip_accents(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def detect_language(question: str) -> Language:
    """"es" salvo que la pregunta sea claramente inglesa.

    Se cuentan palabras funcionales de cada idioma en vez de mirar solo los
    acentos: una pregunta perfectamente espanola puede escribirse sin ninguno
    ("cuanto se gasto en medicamentos"), y ahi los acentos no dicen nada.

    El desempate es hacia el espanol a proposito. Una pregunta ambigua suele
    ser de dominio ("total 2026", "Costa Rica"), y en un observatorio
    centroamericano el default correcto es el espanol.
    """
    if not question:
        return "es"

    palabras = [w.lower() for w in _WORD_PATTERN.findall(question)]
    if not palabras:
        return "es"

    # Un acento es evidencia fuerte de espanol: el ingles no los usa. Su
    # ausencia, en cambio, no prueba nada.
    if any(_strip_accents(w) != w for w in palabras):
        return "es"

    es = sum(1 for w in palabras if w in _SPANISH_MARKERS)
    en = sum(1 for w in palabras if w in _ENGLISH_MARKERS)
    return "en" if en > es else "es"
