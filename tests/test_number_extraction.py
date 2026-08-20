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


def test_el_numero_de_filas_cuenta_como_verificado() -> None:
    """Caso real reportado (2026-08-19): "top 10 adjudicaciones mas caras"
    devolvia la tabla y ningun texto. El redactor escribia "las 10
    adjudicaciones mas caras", el 10 no estaba en ninguna celda, se marcaba
    como alucinacion, y tras dos intentos se servia la plantilla generica.

    El numero de filas es un hecho real del resultado -- se le entrega al
    redactor en el prompt como `filas_totales`."""
    rows = [{"awarded_amount": 15_318_000_000}, {"awarded_amount": 11_488_500_000}]

    sin_row_count = find_unverified_numbers("Estas son las 10 mas caras.", rows)
    assert sin_row_count != []  # el bug que se corrige

    con_row_count = find_unverified_numbers("Estas son las 10 mas caras.", rows, row_count=10)
    assert con_row_count == []


def test_una_fecha_del_resultado_se_puede_citar() -> None:
    """Caso real reportado (2026-08-19): la redaccion decia "adjudicada el 9
    de mayo de 2025" -- correcto, la fecha esta en la celda -- pero el
    verificador solo miraba celdas numericas y marcaba 9 y 2025 como
    inventados, tirando una narrativa buena."""
    rows = [{"award_date": "2025-05-09 00:00:00+00:00", "awarded_amount": 15_318_000_000}]

    invalid = find_unverified_numbers(
        "La mas grande fue por 15,318,000,000 USD, adjudicada el 9 de mayo de 2025.",
        rows,
        row_count=1,
    )

    assert invalid == []


def test_un_numero_dentro_de_un_titulo_tambien_cuenta() -> None:
    rows = [{"title": "Adquisicion 2 Fibras Opticas capacidad de 18 Tbps IRU (25 anios)"}]

    assert find_unverified_numbers("Se contrataron 18 Tbps por 25 anios.", rows) == []


def test_un_total_calculado_por_el_modelo_se_sigue_rechazando() -> None:
    """La celda de texto no puede volverse un pase libre: lo que el verificador
    existe para atrapar es una cifra que el modelo calculo, y esa no aparece
    en ninguna celda -- ni numerica ni de texto."""
    rows = [
        {"award_date": "2025-05-09 00:00:00+00:00", "awarded_amount": 15_318_000_000},
        {"award_date": "2025-06-11 00:00:00+00:00", "awarded_amount": 11_488_500_000},
    ]

    invalid = find_unverified_numbers("En total se adjudicaron 26,806,500,000.", rows, row_count=2)

    assert len(invalid) == 1
    assert "26" in invalid[0]


def test_un_conteo_equivocado_se_sigue_rechazando() -> None:
    """La correccion no puede volverse un pase libre: si se piden 20 y solo
    hay 7, decir "las 20" sigue siendo falso."""
    rows = [{"awarded_amount": 100}]

    invalid = find_unverified_numbers("Estas son las 20 mas caras.", rows, row_count=7)

    assert len(invalid) == 1
    assert "20" in invalid[0]
