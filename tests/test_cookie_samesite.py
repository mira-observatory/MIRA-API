from __future__ import annotations

import pytest
from pydantic import ValidationError

from mira_api.config import Settings

REQUIRED = {
    "DATABASE_URL_QUERY": "postgresql://u:p@localhost/query",
    "DATABASE_URL_WEB": "postgresql://u:p@localhost/web",
    "DATABASE_URL_LOG": "postgresql://u:p@localhost/log",
    "TOKEN_HMAC_SECRET": "test-secret",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **extra: str) -> Settings:
    for key, value in {**REQUIRED, **extra}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_por_defecto_es_lax_para_desarrollo(monkeypatch: pytest.MonkeyPatch) -> None:
    """En local el front y la API comparten origen (localhost) y "lax" es lo
    correcto y lo mas restrictivo que funciona."""
    assert _settings(monkeypatch).cookie_samesite == "lax"


def test_se_puede_poner_none_para_un_despliegue_cross_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desplegados en dominios distintos la peticion es cross-site, y el
    navegador NO reenvia una cookie "lax". Sin poder cambiarlo a "none", cada
    peticion pareceria de una sesion nueva y la atribucion del registro de
    auditoria se perderia sin dar ningun error -- por eso fly.toml lo fija."""
    assert _settings(monkeypatch, COOKIE_SAMESITE="none").cookie_samesite == "none"


def test_rechaza_un_valor_que_el_navegador_no_entiende(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un SameSite invalido lo ignoraria el navegador en silencio. Mejor no
    arrancar que servir cookies que nadie va a reenviar."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, COOKIE_SAMESITE="siempre")


def test_fly_toml_fija_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """El despliegue real deja el front y la API en dominios distintos. Si
    alguien quita esta linea de fly.toml, la cookie deja de viajar y no hay
    ningun sintoma visible."""
    from pathlib import Path

    fly = Path(__file__).resolve().parent.parent / "fly.toml"
    assert 'COOKIE_SAMESITE = "none"' in fly.read_text(encoding="utf-8")
