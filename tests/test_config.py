from __future__ import annotations

import pytest
from pydantic import ValidationError

from mira_api.config import Settings

REQUIRED_VARS = {
    "DATABASE_URL_QUERY": "postgresql://mira_query:pw@localhost:5432/postgres",
    "DATABASE_URL_WEB": "postgresql://mira_web:pw@localhost:5432/postgres",
    "DATABASE_URL_LOG": "postgresql://mira_logger:pw@localhost:5432/postgres",
    "TOKEN_HMAC_SECRET": "test-secret",
}


def _clear_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED_VARS:
        monkeypatch.delenv(key, raising=False)


def test_arranca_con_todas_las_variables_obligatorias(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_required(monkeypatch)
    for key, value in REQUIRED_VARS.items():
        monkeypatch.setenv(key, value)
    # _env_file=None evita que un .env real de la maquina del desarrollador
    # enmascare la ausencia de una variable de entorno.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_url_query == REQUIRED_VARS["DATABASE_URL_QUERY"]


@pytest.mark.parametrize("missing", sorted(REQUIRED_VARS))
def test_falla_al_construir_si_falta_una_variable_obligatoria(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El fallo debe ocurrir al instanciar Settings (arranque), no a mitad de una
    peticion: main.py llama a get_settings() a nivel de modulo precisamente para
    forzar esto."""
    _clear_required(monkeypatch)
    for key, value in REQUIRED_VARS.items():
        if key != missing:
            monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert missing.lower() in str(err.value)


def test_variables_obligatorias_son_exactamente_estas() -> None:
    """Igualdad, no subconjunto, a proposito.

    Volver obligatorio un campo rompe todos los sitios que construyen Settings
    a mano, y varios viven en pruebas que se saltan sin Postgres real: el fallo
    aparece recien en CI, lejos del cambio que lo causo. Paso exactamente eso
    con database_url_web el 2026-08-20.

    Con igualdad, agregar un campo obligatorio falla aqui de inmediato y en
    cualquier maquina, y obliga a revisar REQUIRED_VARS y los demas sitios.
    """
    required_fields = {name for name, field in Settings.model_fields.items() if field.is_required()}
    expected = {key.lower() for key in REQUIRED_VARS}
    assert required_fields == expected


def test_limites_de_reintentos_se_cargan_desde_el_ambiente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_required(monkeypatch)
    for key, value in REQUIRED_VARS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SQL_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("NARRATIVE_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("NARRATIVE_MAX_ROWS_IN_PROMPT", "30")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.sql_max_attempts == 4
    assert settings.narrative_max_attempts == 5
    assert settings.narrative_max_rows_in_prompt == 30


@pytest.mark.parametrize(
    "name",
    ["SQL_MAX_ATTEMPTS", "NARRATIVE_MAX_ATTEMPTS", "NARRATIVE_MAX_ROWS_IN_PROMPT"],
)
def test_limites_de_reintentos_deben_ser_mayores_que_cero(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_required(monkeypatch)
    for key, value in REQUIRED_VARS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(name, "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
