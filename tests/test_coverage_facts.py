from __future__ import annotations

import pytest

from mira_api.db.executor import Rows
from mira_api.nlq.coverage_facts import diagnose_empty_result, extract_text_search_predicates


class _FakeExecutor:
    """Devuelve conteos por vista. `None` simula que la consulta revienta."""

    def __init__(
        self,
        por_vista: dict[str, list[dict[str, object]]] | None,
        por_termino: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self._por_vista = por_vista
        #: Filas por termino de busqueda, para el chequeo de ILIKE de
        #: extract_text_search_predicates -- necesita distinguirse de la
        #: consulta de entidad de arriba aunque ambas toquen query.v_process.
        self._por_termino = por_termino or {}
        self.consultas: list[str] = []

    async def run(self, sql: str, *, max_rows: int, params: dict | None = None) -> Rows:
        if self._por_vista is None:
            raise RuntimeError("boom")
        self.consultas.append(sql)
        if "ilike %(patron)s" in sql and params is not None:
            termino = str(params.get("patron", "")).strip("%")
            filas = self._por_termino.get(termino, [])
            return Rows(columns=[], rows=filas, row_count=len(filas), truncated=False)
        # Se busca por "from query.v_x": la consulta de adjudicaciones tambien
        # menciona v_process en el JOIN, y un substring suelto la confundiria
        # con la de procesos.
        clave = next((v for v in self._por_vista if f"from {v}" in sql), None)
        filas = self._por_vista.get(clave or "", [])
        return Rows(columns=[], rows=filas, row_count=len(filas), truncated=False)


AWARDS = frozenset({"query.v_awards", "query.v_process"})


@pytest.mark.asyncio
async def test_avisa_cuando_el_pais_no_tiene_esa_entidad_cargada() -> None:
    """Caso real: Nicaragua tiene 409 procesos pero cero adjudicaciones. El
    cero de "cuanto se adjudico en Nicaragua" no significa que no se adjudico
    nada, significa que el ETL no cargo esa parte."""
    executor = _FakeExecutor(
        {
            "query.v_process": [{"country_code": "NI", "n": 409}],
            "query.v_awards": [],  # ninguna adjudicacion cargada
        }
    )

    d = await diagnose_empty_result(executor, countries=["NI"], relations=AWARDS)  # type: ignore[arg-type]

    assert len(d.warnings) == 1
    aviso = d.warnings[0]
    assert aviso.code == "PARTIAL_COVERAGE"
    assert "adjudicaciones" in aviso.message_es
    # El mensaje lo lee una persona: nombre, no codigo ISO.
    assert "Nicaragua" in aviso.message_es
    assert "no significa que no existan" in aviso.message_es.lower()
    # El codigo ISO si va en details, que es lo que consume el frontend.
    assert aviso.details["sin_datos"] == {"adjudicaciones": ["NI"]}


@pytest.mark.asyncio
async def test_sin_aviso_cuando_hay_datos_de_todo_lo_consultado() -> None:
    """Si las entidades tienen datos y aun asi no hubo filas, el cero es real
    y no hay nada que aclarar -- inventar una advertencia seria tan enganoso
    como omitirla."""
    executor = _FakeExecutor(
        {
            "query.v_process": [{"country_code": "CR", "n": 6650}],
            "query.v_awards": [{"country_code": "CR", "n": 43231}],
        }
    )

    d = await diagnose_empty_result(executor, countries=["CR"], relations=AWARDS)  # type: ignore[arg-type]

    assert d.warnings == []
    assert d.coverage is not None
    assert d.coverage.countries == ["CR"]


@pytest.mark.asyncio
async def test_distingue_pais_por_pais() -> None:
    """Con dos paises pedidos, solo se nombra el que no tiene datos."""
    executor = _FakeExecutor(
        {
            "query.v_process": [
                {"country_code": "CR", "n": 6650},
                {"country_code": "NI", "n": 409},
            ],
            "query.v_awards": [{"country_code": "CR", "n": 43231}],
        }
    )

    d = await diagnose_empty_result(executor, countries=["CR", "NI"], relations=AWARDS)  # type: ignore[arg-type]

    assert d.warnings[0].details["sin_datos"] == {"adjudicaciones": ["NI"]}
    # Costa Rica si tiene adjudicaciones: nombrarla como faltante seria falso.
    assert "adjudicaciones de Nicaragua" in d.warnings[0].message_es
    assert "Costa Rica" not in d.warnings[0].message_es


@pytest.mark.asyncio
async def test_un_fallo_del_diagnostico_no_tumba_la_respuesta() -> None:
    """El diagnostico es un extra sobre una respuesta que ya esta lista. Si
    falla, la persona recibe su cero sin explicacion -- que es lo que recibia
    antes -- pero nunca un error."""
    executor = _FakeExecutor(None)

    d = await diagnose_empty_result(executor, countries=["NI"], relations=AWARDS)  # type: ignore[arg-type]

    assert d.warnings == []
    assert d.coverage is None


@pytest.mark.asyncio
async def test_no_consulta_nada_si_la_vista_no_es_de_una_entidad_conocida() -> None:
    executor = _FakeExecutor({})

    d = await diagnose_empty_result(
        executor,  # type: ignore[arg-type]
        countries=["CR"],
        relations=frozenset({"query.v_process_buyers"}),
    )

    assert d.warnings == []
    assert executor.consultas == []


@pytest.mark.asyncio
async def test_avisa_cuando_la_consulta_pide_un_periodo_fuera_de_cobertura() -> None:
    """Caso real: Guatemala tiene datos de 2025 a 2026. Preguntar por 2020 devuelve
    cero filas, y ese cero no significa que no hubo contrataciones en 2020 sino que
    MIRA solo cubre 2025 a 2026."""
    import datetime

    executor = _FakeExecutor(
        {
            "query.v_process": [
                {
                    "country_code": "GT",
                    "n": 250000,
                    "dt_min": datetime.date(2025, 1, 2),
                    "dt_max": datetime.date(2026, 8, 20),
                }
            ],
            "query.v_awards": [
                {
                    "country_code": "GT",
                    "n": 200000,
                    "dt_min": datetime.date(2025, 1, 2),
                    "dt_max": datetime.date(2026, 8, 20),
                }
            ],
        }
    )

    sql = (
        "SELECT * FROM query.v_process WHERE country_code = 'GT' "
        "AND publication_date >= '2020-01-01' AND publication_date < '2021-01-01'"
    )
    d = await diagnose_empty_result(
        executor, countries=["GT"], relations=AWARDS, sql=sql  # type: ignore[arg-type]
    )

    assert len(d.warnings) == 1
    aviso = d.warnings[0]
    assert aviso.code == "NO_DATA_FOR_PERIOD"
    assert "Guatemala" in aviso.message_es
    assert "2025-01-02 a 2026-08-20" in aviso.message_es
    assert "2020" in aviso.message_es
    assert aviso.details["periodo_consultado"] == "2020"
    assert aviso.details["paises_fuera_de_rango"] == ["GT"]



ITEMS = frozenset({"query.v_process", "query.v_awards", "query.v_award_items", "query.v_items"})


@pytest.mark.asyncio
async def test_avisa_cuando_falta_el_vinculo_item_adjudicacion() -> None:
    """Caso real (2026-08-26): "producto mas vendido en Guatemala" dio cero
    filas. Guatemala tiene 405,623 filas en v_items y adjudicaciones de
    sobra, pero CERO en v_award_items -- el vinculo que dice que item cubrio
    cada adjudicacion nunca se cargo. v_items por si sola no lo delata: hay
    que revisar el vinculo, no el catalogo."""
    executor = _FakeExecutor(
        {
            "query.v_process": [{"country_code": "GT", "n": 253852}],
            "query.v_awards": [{"country_code": "GT", "n": 50000}],
            "query.v_award_items": [],  # el vinculo nunca se cargo para GT
        }
    )

    d = await diagnose_empty_result(executor, countries=["GT"], relations=ITEMS)  # type: ignore[arg-type]

    assert len(d.warnings) == 1
    aviso = d.warnings[0]
    assert aviso.code == "PARTIAL_COVERAGE"
    assert "productos" in aviso.message_es
    assert "Guatemala" in aviso.message_es
    assert aviso.details["sin_datos"] == {"productos": ["GT"]}


# --- extract_text_search_predicates ------------------------------------------


def test_extrae_ilike_sobre_columna_de_texto_conocida() -> None:
    sql = "SELECT * FROM query.v_process WHERE procurement_method ILIKE '%directa%'"
    assert extract_text_search_predicates(sql) == [("procurement_method", "directa")]


def test_ignora_ilike_sobre_columna_que_no_es_de_busqueda_de_categoria() -> None:
    """category_normalised/item_description ya tienen su propio manejo (regla
    6c los evita); un ILIKE ahi no es el caso que este chequeo cubre."""
    sql = "SELECT * FROM query.v_items WHERE item_description ILIKE '%meropenem%'"
    assert extract_text_search_predicates(sql) == []


def test_extrae_varios_predicados_sin_duplicar() -> None:
    sql = (
        "SELECT * FROM query.v_process WHERE title ILIKE '%medicamento%' "
        "OR description ILIKE '%medicamento%'"
    )
    assert extract_text_search_predicates(sql) == [
        ("title", "medicamento"),
        ("description", "medicamento"),
    ]


def test_sql_invalido_no_revienta() -> None:
    assert extract_text_search_predicates("esto no es sql") == []
    assert extract_text_search_predicates("") == []


# --- Termino de categoria/modalidad que no calza con un pais -----------------


PROCESS_ONLY = frozenset({"query.v_process"})


@pytest.mark.asyncio
async def test_avisa_cuando_el_termino_no_calza_con_ningun_pais() -> None:
    """Caso real (2026-08-27): "adjudicacion directa" en Costa Rica volvia
    vacio sin explicacion. CR tiene 12,783 procesos (el chequeo de entidad no
    dispara) y no hay periodo fuera de rango, pero CR nunca escribe la
    palabra "directa" en su procurement_method."""
    executor = _FakeExecutor(
        por_vista={"query.v_process": [{"country_code": "CR", "n": 12783}]},
        por_termino={"directa": []},  # ningun pais tiene una fila que calce
    )
    sql = "SELECT * FROM query.v_process WHERE country_code = 'CR' AND procurement_method ILIKE '%directa%'"

    d = await diagnose_empty_result(
        executor, countries=["CR"], relations=PROCESS_ONLY, sql=sql  # type: ignore[arg-type]
    )

    assert len(d.warnings) == 1
    aviso = d.warnings[0]
    assert aviso.code == "NO_MATCH_FOR_TERM"
    assert '"directa"' in aviso.message_es
    assert "Costa Rica" in aviso.message_es
    assert aviso.details["terminos_sin_match"] == {"directa": ["CR"]}


@pytest.mark.asyncio
async def test_sin_aviso_de_termino_si_el_pais_si_tiene_coincidencias() -> None:
    executor = _FakeExecutor(
        por_vista={"query.v_process": [{"country_code": "HN", "n": 196973}]},
        por_termino={"directa": [{"country_code": "HN", "n": 3192}]},
    )
    sql = "SELECT * FROM query.v_process WHERE country_code = 'HN' AND procurement_method ILIKE '%directa%'"

    d = await diagnose_empty_result(
        executor, countries=["HN"], relations=PROCESS_ONLY, sql=sql  # type: ignore[arg-type]
    )

    assert d.warnings == []


@pytest.mark.asyncio
async def test_termino_no_encontrado_distingue_pais_por_pais() -> None:
    executor = _FakeExecutor(
        por_vista={
            "query.v_process": [
                {"country_code": "GT", "n": 253852},
                {"country_code": "CR", "n": 12783},
            ]
        },
        por_termino={"directa": [{"country_code": "GT", "n": 236201}]},
    )
    sql = (
        "SELECT * FROM query.v_process WHERE country_code IN ('GT', 'CR') "
        "AND procurement_method ILIKE '%directa%'"
    )

    d = await diagnose_empty_result(
        executor, countries=["GT", "CR"], relations=PROCESS_ONLY, sql=sql  # type: ignore[arg-type]
    )

    assert d.warnings[0].details["terminos_sin_match"] == {"directa": ["CR"]}
    assert "Guatemala" not in d.warnings[0].message_es
