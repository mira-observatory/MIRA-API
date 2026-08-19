from __future__ import annotations

from mira_api.nlq.number_extraction import (
    allowed_values_from_rows,
    extract_numbers,
    find_unverified_numbers,
)


def test_numero_entero_simple() -> None:
    assert extract_numbers("hay 7992 procesos") == {7992.0}


def test_miles_con_coma() -> None:
    assert extract_numbers("hay 7,992 procesos") == {7992.0}


def test_miles_con_punto_formato_latino() -> None:
    assert extract_numbers("hay 7.992 procesos") == {7992.0}


def test_decimal_con_coma() -> None:
    assert extract_numbers("un promedio de 45,5") == {45.5}


def test_decimal_con_punto() -> None:
    assert extract_numbers("un promedio de 45.5") == {45.5}


def test_miles_y_decimales_formato_latino() -> None:
    assert extract_numbers("el monto fue de 1.234,56") == {1234.56}


def test_miles_y_decimales_formato_en_us() -> None:
    assert extract_numbers("el monto fue de 1,234.56") == {1234.56}


def test_millones() -> None:
    assert extract_numbers("se gastaron 2,3 millones") == {2_300_000.0}
    assert extract_numbers("se gastaron 2 millones") == {2_000_000.0}


def test_mil_como_sufijo() -> None:
    assert extract_numbers("unos 45 mil colones") == {45_000.0}


def test_porcentaje_se_compara_sin_el_signo() -> None:
    assert extract_numbers("el 45% del total") == {45.0}


def test_varios_numeros_en_el_mismo_texto() -> None:
    result = extract_numbers("de 100 procesos, 45 fueron adjudicados (45%)")
    assert result == {100.0, 45.0}


def test_allowed_values_incluye_redondeos() -> None:
    allowed = allowed_values_from_rows([{"total": 7991.6}])
    assert 7991.6 in allowed
    assert 7992.0 in allowed  # redondeado a 0 decimales


def test_find_unverified_numbers_vacio_cuando_todo_coincide() -> None:
    rows = [{"count": 7992}]
    assert find_unverified_numbers("Hay 7992 procesos en total.", rows) == []


def test_find_unverified_numbers_detecta_numero_inventado() -> None:
    rows = [{"count": 7992}]
    invalid = find_unverified_numbers("Hay 8000 procesos en total.", rows)
    assert len(invalid) == 1
    assert "8000" in invalid[0] or "8.000" in invalid[0] or "8,000" in invalid[0]


def test_find_unverified_numbers_tolera_redondeo_a_entero() -> None:
    rows = [{"promedio": 45.499}]
    assert find_unverified_numbers("El promedio es 45.5.", rows) == []


def test_find_unverified_numbers_con_millones_desde_el_dato_real() -> None:
    rows = [{"awarded_amount": 2_300_000}]
    assert find_unverified_numbers("Se adjudicaron 2,3 millones.", rows) == []
    assert find_unverified_numbers("Se adjudicaron 5 millones.", rows) != []
