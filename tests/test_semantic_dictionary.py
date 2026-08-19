from __future__ import annotations

from mira_api.db.executor import Rows
from mira_api.nlq.semantic_dictionary import ColumnDoc, format_for_prompt, load_semantic_dictionary
from mira_api.nlq.validator import ALLOWED_RELATIONS


class _FakeExecutor:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.last_params: dict[str, object] | None = None

    async def run(
        self, sql: str, *, max_rows: int, params: dict[str, object] | None = None
    ) -> Rows:
        self.last_params = params
        return Rows(columns=[], rows=self._rows, row_count=len(self._rows), truncated=False)


def test_format_for_prompt_agrupa_por_vista_y_marca_advertencias() -> None:
    columns = [
        ColumnDoc(
            view_name="query.v_process",
            column_name="process_id",
            description_es="Identificador del proceso.",
            data_type="text",
            enum_values=None,
            unit=None,
            is_aggregable=False,
            caveat=None,
        ),
        ColumnDoc(
            view_name="query.v_process",
            column_name="country_code",
            description_es="Pais.",
            data_type="text",
            enum_values=["CR", "GT", "HN", "NI"],
            unit=None,
            is_aggregable=False,
            caveat=None,
        ),
        ColumnDoc(
            view_name="query.v_awards",
            column_name="awarded_amount",
            description_es="Monto adjudicado.",
            data_type="numeric",
            enum_values=None,
            unit="currency_code",
            is_aggregable=True,
            caveat="Nunca sumar monedas distintas.",
        ),
    ]

    text = format_for_prompt(columns)

    assert "query.v_process:" in text
    assert "query.v_awards:" in text
    assert "country_code" in text
    assert "CR, GT, HN, NI" in text
    assert "[agregable]" in text
    assert "ADVERTENCIA: Nunca sumar monedas distintas." in text
    # v_awards va antes que v_process alfabeticamente -- agrupado, no repetido.
    assert text.index("query.v_awards:") < text.index("query.v_process:")


async def test_load_semantic_dictionary_mapea_las_columnas_reales() -> None:
    fake_rows = [
        {
            "view_name": "query.v_process",
            "column_name": "process_id",
            "description_es": "Identificador.",
            "data_type": "text",
            "enum_values": None,
            "unit": None,
            "is_aggregable": False,
            "caveat": None,
        }
    ]
    executor = _FakeExecutor(fake_rows)

    columns = await load_semantic_dictionary(executor)  # type: ignore[arg-type]

    assert len(columns) == 1
    assert columns[0].view_name == "query.v_process"
    assert columns[0].column_name == "process_id"


async def test_solo_pide_las_vistas_que_el_validador_permite() -> None:
    """MIRA-ETL documenta vistas que el validador no permite (query.v_coverage
    son metadatos de las corridas del ETL). Describirselas al modelo garantiza
    que genere SQL contra ellas, que el validador lo rechace, y que se gasten
    los 3 intentos para terminar en REJECTED_SQL_RELATION."""
    executor = _FakeExecutor([])

    await load_semantic_dictionary(executor)  # type: ignore[arg-type]

    assert executor.last_params is not None
    pedidas = executor.last_params["allowed"]
    assert set(pedidas) == set(ALLOWED_RELATIONS)
    assert "query.v_coverage" not in pedidas
