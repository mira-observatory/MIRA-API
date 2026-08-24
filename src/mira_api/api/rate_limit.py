"""Limite por IP sobre las rutas que llaman al modelo.

El presupuesto global (quota/budget.py) protege el gasto del dia entero, pero
bloquea a TODOS por igual cuando se agota. Sin nada mas, un solo cliente
hammering /query podria agotarlo en minutos y dejar sin servicio al resto por
el resto del dia -- el presupuesto no distingue quien lo gasto. Esto cierra
esa brecha: frena a un IP individual mucho antes de que llegue a mover la
aguja del presupuesto compartido.

Deliberadamente en memoria, un solo diccionario por proceso: el servicio corre
con --workers 1 (ver Dockerfile), el mismo supuesto bajo el que ya vive el resto
del estado de proceso unico (system_blocks, pools). Con mas de un worker esto
dejaria de ser correcto -- cada uno tendria su propio conteo -- pero escalar
horizontalmente aqui es agregar replicas de servicio, no workers de un mismo
proceso.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RateLimitResult:
    allowed: bool
    #: Segundos hasta que valga la pena reintentar. Solo tiene sentido cuando
    #: allowed es False.
    retry_after_s: float = 0.0


@dataclass
class IpRateLimiter:
    """Ventana fija por IP: cuenta pedidos en el minuto en curso.

    Una ventana fija (no deslizante) puede dejar pasar hasta 2x el limite
    pegado al borde entre dos minutos. Se acepta a proposito: la defensa real
    contra el gasto es el presupuesto global, esto es para frenar un abuso
    sostenido, no para contar con precision de laboratorio.
    """

    limit: int
    window_s: float = 60.0
    _counts: dict[str, tuple[int, float]] = field(default_factory=dict, repr=False)

    def check(self, key: str, *, now: float | None = None) -> RateLimitResult:
        """No lanza nada: quien llama decide que hacer con el resultado.

        `key` es la IP ya resuelta (ver resolve_client_ip). Pasar una cadena
        vacia o constante colapsaria a todo el mundo en el mismo balde --
        responsabilidad de quien llama resolverla primero.
        """
        if self.limit <= 0:
            return RateLimitResult(allowed=True)

        instante = now if now is not None else time.monotonic()
        cuenta, inicio_ventana = self._counts.get(key, (0, instante))

        if instante - inicio_ventana >= self.window_s:
            cuenta, inicio_ventana = 0, instante

        if cuenta >= self.limit:
            return RateLimitResult(
                allowed=False, retry_after_s=max(0.0, self.window_s - (instante - inicio_ventana))
            )

        self._counts[key] = (cuenta + 1, inicio_ventana)
        return RateLimitResult(allowed=True)

    def reset(self) -> None:
        """Solo para pruebas: un limitador de produccion vive todo el proceso."""
        self._counts.clear()


def resolve_client_ip(request_client_host: str | None, forwarded_for: str | None) -> str | None:
    """La IP real del visitante, no la del proxy de Render.

    Render (como cualquier PaaS detras de un balanceador) hace que
    `request.client.host` sea la direccion del proxy, no la de quien
    pregunta -- confirmado: el Dockerfile no pasaba --proxy-headers, asi que
    TODAS las peticiones colapsaban en la misma IP interna. Eso hacia este
    limite inutil (todo el mundo en el mismo balde) y de paso rompia en
    silencio la atribucion de auditoria por IP para quien llega sin cookie.

    X-Forwarded-For puede traer una cadena de saltos ("cliente, proxy1,
    proxy2"); el primero es el mas cercano al origen. Confiarlo aqui es
    correcto solo porque Uvicorn ya corre con --forwarded-allow-ips='*' --
    es decir, ya se confio en que el unico trafico que llega al contenedor
    paso por el borde de Render. Sin eso, un cliente podria escribir
    cualquier valor en ese encabezado y saltarse el limite.
    """
    if forwarded_for:
        primero = forwarded_for.split(",")[0].strip()
        if primero:
            return primero
    return request_client_host
