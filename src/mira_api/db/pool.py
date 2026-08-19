from __future__ import annotations

from urllib.parse import quote

from psycopg_pool import AsyncConnectionPool

from mira_api.config import Settings

# Los limites de sesion viajan en las opciones de la cadena de conexion, no en un
# SET posterior: asi sobreviven a cualquier proveedor y funcionarian incluso detras
# de un pooler en modo transaccion.
_READ_ONLY_OPTIONS = (
    "-c statement_timeout={timeout} "
    "-c idle_in_transaction_session_timeout=10000 "
    "-c lock_timeout=3000 "
    "-c default_transaction_read_only=on "
    "-c search_path=query"
)


def _with_options(dsn: str, options: str) -> str:
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options={quote(options)}"


def build_read_pool(settings: Settings) -> AsyncConnectionPool:
    """Pool de lectura. Es la unica puerta del servicio hacia los datos."""
    options = _READ_ONLY_OPTIONS.format(timeout=settings.statement_timeout_ms)
    return AsyncConnectionPool(
        conninfo=_with_options(settings.database_url, options),
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=settings.pool_timeout_s,
        max_waiting=32,
        max_lifetime=1800,  # recicla conexiones tras un failover del proveedor
        max_idle=300,
        open=False,  # se abre en el lifespan de FastAPI
    )


_LOG_OPTIONS = (
    "-c statement_timeout=3000 -c idle_in_transaction_session_timeout=10000 "
    "-c lock_timeout=2000 -c search_path=analytics"
)


def build_log_pool(settings: Settings) -> AsyncConnectionPool:
    """Pool para analytics.*, separado del de lectura -- si el registro se
    satura o falla, las consultas de los usuarios no deben verse afectadas.

    Ya no es "minimo": desde que existe el presupuesto global (Hito 5), CADA
    consulta lo toca varias veces (2 lecturas de check_budget + 2 escrituras
    de record_global_spend), no solo ocasionalmente para auditoria -- un
    max_size de 2 no aguanta trafico concurrente real (verificado: 100
    peticiones simultaneas agotaban el pool). Timeout corto (3s): un contador
    de cuota lento no debe alargar la respuesta al usuario.
    """
    return AsyncConnectionPool(
        conninfo=_with_options(settings.database_url_log, _LOG_OPTIONS),
        min_size=2,
        max_size=10,
        timeout=5.0,
        max_waiting=200,
        max_lifetime=1800,
        open=False,
    )
