"""Limite por IP: lo que frena a un solo cliente sin depender del presupuesto
global, que frena a TODOS por igual cuando se agota."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from mira_api.api.rate_limit import IpRateLimiter, resolve_client_ip

# --- IpRateLimiter -----------------------------------------------------------


def test_permite_hasta_el_limite_y_despues_corta() -> None:
    limiter = IpRateLimiter(limit=3)

    resultados = [limiter.check("1.2.3.4", now=0.0) for _ in range(4)]

    assert [r.allowed for r in resultados] == [True, True, True, False]


def test_ips_distintas_no_comparten_balde() -> None:
    """Sin esto el limite protegeria contra un IP hammering pero de paso
    penalizaria a cualquier otro visitante que compartiera esa IP -- que es
    justo el bug que tenia antes de resolver la IP real (todos colapsaban en
    la del proxy de Render)."""
    limiter = IpRateLimiter(limit=1)

    primero = limiter.check("1.1.1.1", now=0.0)
    segundo = limiter.check("2.2.2.2", now=0.0)

    assert primero.allowed
    assert segundo.allowed


def test_la_ventana_se_reinicia_sola() -> None:
    limiter = IpRateLimiter(limit=1, window_s=60.0)

    limiter.check("1.2.3.4", now=0.0)
    bloqueado = limiter.check("1.2.3.4", now=10.0)
    liberado = limiter.check("1.2.3.4", now=61.0)

    assert not bloqueado.allowed
    assert liberado.allowed


def test_retry_after_es_el_tiempo_que_falta_para_la_siguiente_ventana() -> None:
    limiter = IpRateLimiter(limit=1, window_s=60.0)

    limiter.check("1.2.3.4", now=0.0)
    resultado = limiter.check("1.2.3.4", now=45.0)

    assert resultado.retry_after_s == 15.0


def test_limite_cero_desactiva_el_control() -> None:
    """Util en pruebas y para apagarlo sin tocar el resto del codigo, igual
    que enable_subject_quota."""
    limiter = IpRateLimiter(limit=0)

    resultados = [limiter.check("1.2.3.4") for _ in range(50)]

    assert all(r.allowed for r in resultados)


# --- resolve_client_ip --------------------------------------------------------


def test_prefiere_x_forwarded_for_sobre_el_host_de_la_conexion() -> None:
    """El host de la conexion TCP es el proxy de Render, no quien pregunta."""
    ip = resolve_client_ip("10.0.0.5", "203.0.113.7")

    assert ip == "203.0.113.7"


def test_toma_el_primer_salto_de_una_cadena() -> None:
    """"cliente, proxy1, proxy2": el primero es el mas cercano al origen."""
    ip = resolve_client_ip("10.0.0.5", "203.0.113.7, 10.0.0.9")

    assert ip == "203.0.113.7"


def test_sin_forwarded_cae_al_host_de_la_conexion() -> None:
    """Desarrollo local, sin proxy en el medio: no hay X-Forwarded-For y el
    host de la conexion SI es el real."""
    ip = resolve_client_ip("127.0.0.1", None)

    assert ip == "127.0.0.1"


def test_forwarded_vacio_tambien_cae_al_host() -> None:
    ip = resolve_client_ip("127.0.0.1", "")

    assert ip == "127.0.0.1"


# --- integrado sobre una ruta real, igual que test_cors.py -------------------


def _app_con_limite(limit: int) -> FastAPI:
    app = FastAPI()
    app.state.rate_limiter = IpRateLimiter(limit=limit)

    def enforce(http_request: Request) -> None:
        limiter: IpRateLimiter = http_request.app.state.rate_limiter
        ip = resolve_client_ip(
            http_request.client.host if http_request.client else None,
            http_request.headers.get("x-forwarded-for"),
        )
        resultado = limiter.check(ip or "desconocida")
        if not resultado.allowed:
            raise HTTPException(
                status_code=429,
                detail="Demasiadas preguntas seguidas. Espera un momento e intenta de nuevo.",
                headers={"Retry-After": str(int(resultado.retry_after_s) + 1)},
            )

    @app.post("/query", dependencies=[Depends(enforce)])
    def query() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_la_cuarta_peticion_seguida_recibe_429() -> None:
    client = TestClient(_app_con_limite(limit=3))

    respuestas = [client.post("/query") for _ in range(4)]

    assert [r.status_code for r in respuestas] == [200, 200, 200, 429]
    assert "Retry-After" in respuestas[-1].headers


def test_ips_distintas_no_se_bloquean_entre_si_en_la_ruta_real() -> None:
    client = TestClient(_app_con_limite(limit=1))

    primero = client.post("/query", headers={"X-Forwarded-For": "1.1.1.1"})
    segundo = client.post("/query", headers={"X-Forwarded-For": "2.2.2.2"})

    assert primero.status_code == 200
    assert segundo.status_code == 200
