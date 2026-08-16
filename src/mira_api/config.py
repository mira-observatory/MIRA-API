from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracion del servicio.

    Nada aqui menciona a Supabase a proposito: el servicio habla con un PostgreSQL
    estandar. Migrar de proveedor es cambiar el valor de DATABASE_URL.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- base de datos -----------------------------------------------------
    # Rol de solo lectura (mira_query). Sin permisos de escritura en ningun esquema.
    database_url: str = Field(description="DSN de PostgreSQL para consultas de lectura")
    # Rol minimo de escritura, solo para el esquema analytics (mira_logger).
    database_url_log: str = Field(description="DSN de PostgreSQL para el registro de auditoria")

    pool_min_size: int = 2
    pool_max_size: int = 8
    pool_timeout_s: float = 5.0
    statement_timeout_ms: int = 8000

    # --- modelo de lenguaje ------------------------------------------------
    # v1 no tiene catalogo de plantillas: toda pregunta contestable se responde con
    # SQL generado por el modelo, validado antes de ejecutarse. Ver seccion 1.4.
    anthropic_api_key: str = ""
    model_fast: str = "claude-haiku-4-5-20251001"
    # Configurable por variable de entorno: claude-opus-5 (calidad) o
    # claude-sonnet-5 (costo, ~30% mas turnos por el mismo presupuesto).
    sql_model: str = "claude-sonnet-5"

    # --- limites -----------------------------------------------------------
    max_question_chars: int = 400
    max_rows: int = 500

    # Cuota por sujeto (token anonimo o prefijo de red): acota el abuso, debe ser
    # generosa. El cortacircuitos global de abajo es lo unico que garantiza el gasto.
    quota_per_day: int = 5
    quota_per_month: int = 15

    # Cortacircuitos global de presupuesto. Al 80% del diario, modo solo cache;
    # al 100% del mensual, solo cache hasta el dia 1.
    budget_daily_usd: float = 3.50
    budget_monthly_usd: float = 100.0

    # --- versionado, para atribuir regresiones -----------------------------
    prompt_version: str = "0.1.0"
    app_version: str = "0.1.0"

    # --- red ---------------------------------------------------------------
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
