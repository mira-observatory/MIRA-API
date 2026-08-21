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

_WEB_READ_ONLY_OPTIONS = (
    "-c statement_timeout=3000 "
    "-c idle_in_transaction_session_timeout=10000 "
    "-c lock_timeout=1000 "
    "-c default_transaction_read_only=on "
    "-c search_path=web"
)


def _with_options(dsn: str, options: str) -> str:
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options={quote(options)}"


def build_read_pool(settings: Settings) -> AsyncConnectionPool:
    """Pool de lectura. Es la unica puerta del servicio hacia los datos."""
    options = _READ_ONLY_OPTIONS.format(timeout=settings.statement_timeout_ms)
    return AsyncConnectionPool(
        conninfo=_with_options(settings.database_url_query, options),
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

    En Supabase Nano no debe abrir conexiones durante el startup: el diccionario
    semantico solo necesita el pool de lectura. Se configura con min_size=0 por
    defecto y abre conexiones bajo demanda.
    """
    return AsyncConnectionPool(
        conninfo=_with_options(settings.database_url_log, _LOG_OPTIONS),
        min_size=settings.log_pool_min_size,
        max_size=settings.log_pool_max_size,
        timeout=settings.pool_timeout_s,
        max_waiting=200,
        max_lifetime=1800,
        open=False,
    )


def build_web_pool(settings: Settings) -> AsyncConnectionPool:
    """Pool aislado para endpoints publicos respaldados por SQL fijo."""
    return AsyncConnectionPool(
        conninfo=_with_options(settings.database_url_web, _WEB_READ_ONLY_OPTIONS),
        min_size=settings.web_pool_min_size,
        max_size=settings.web_pool_max_size,
        timeout=settings.pool_timeout_s,
        max_waiting=32,
        max_lifetime=1800,
        max_idle=300,
        open=False,
    )
