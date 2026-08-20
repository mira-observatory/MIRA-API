"""CORS y cabeceras: lo que separa "solo nuestro front" de "cualquier sitio".

CORS no protege contra XSS ni contra que alguien llame la API con curl -- no
hay navegador ahi. Lo que si controla es que **otra pagina web** pueda usar el
navegador de un visitante, con su cookie, y leer la respuesta.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mira_api.config import Settings

REQUIRED = {
    "DATABASE_URL_QUERY": "postgresql://u:p@localhost/query",
    "DATABASE_URL_WEB": "postgresql://u:p@localhost/web",
    "DATABASE_URL_LOG": "postgresql://u:p@localhost/log",
    "TOKEN_HMAC_SECRET": "test-secret",
}

AJENO = "https://sitio-ajeno.example"


def _settings(monkeypatch: pytest.MonkeyPatch, **extra: str) -> Settings:
    for key, value in {**REQUIRED, **extra}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _allow_origin_para(origins: list[str]) -> str | None:
    """Que responde el middleware ante una peticion de un sitio ajeno."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/x")
    def x() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app).get("/x", headers={"Origin": AJENO}).headers.get(
        "access-control-allow-origin"
    )


def test_el_comodin_no_deja_arrancar_el_servicio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poner "*" es el atajo natural de quien depura CORS en produccion, y
    justamente el que abre la puerta. Documentarlo no basta: no arranca."""
    with pytest.raises(ValidationError) as err:
        _settings(monkeypatch, CORS_ORIGINS="*")

    assert "cors_origins" in str(err.value).lower()


def test_tampoco_disfrazado_entre_origenes_validos(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, CORS_ORIGINS="https://mira.example,*")


def test_una_lista_explicita_si_arranca(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, CORS_ORIGINS="https://mira.example, https://www.mira.example")

    assert s.cors_origin_list == ["https://mira.example", "https://www.mira.example"]


def test_por_que_el_comodin_es_peligroso() -> None:
    """La razon concreta, no una regla de memoria: con "*" y credenciales,
    Starlette le devuelve a cada quien su propio origen, asi que el navegador
    del visitante SI deja leer la respuesta a un sitio ajeno."""
    assert _allow_origin_para(["*"]) == AJENO
    assert _allow_origin_para(["https://mira.example"]) is None
