from __future__ import annotations

import pytest
from pydantic import ValidationError

from mira_api.config import Settings

REQUIRED_VARS = {
    "DATABASE_URL": "postgresql://mira_query:pw@localhost:5432/postgres",
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
    assert settings.database_url == REQUIRED_VARS["DATABASE_URL"]


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


def test_variables_obligatorias_no_tienen_valor_por_defecto() -> None:
    required_fields = {name for name, field in Settings.model_fields.items() if field.is_required()}
    expected = {key.lower() for key in REQUIRED_VARS}
    assert expected <= required_fields
