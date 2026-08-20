from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracion del servicio.

    Nada aqui menciona a Supabase a proposito: el servicio habla con un PostgreSQL
    estandar. Migrar de proveedor es cambiar el valor de DATABASE_URL_QUERY.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- base de datos -----------------------------------------------------
    # Rol de solo lectura (mira_query). Sin permisos de escritura en ningun esquema.
    database_url_query: str = Field(description="DSN de PostgreSQL para consultas NLQ de lectura")
    # Rol de solo lectura para endpoints publicos de SQL fijo. No tiene acceso al
    # esquema `query` usado por el modelo ni a las tablas internas de `mart`.
    #
    # Obligatorio: el rol mira_web ya existe en produccion (creado 2026-08-20).
    # Estuvo temporalmente opcional mientras no existia, para que una
    # funcionalidad sin aprovisionar no impidiera arrancar el servicio entero.
    # Ese motivo ya no aplica, y fallar al arrancar es mejor que fallar a mitad
    # de una peticion.
    database_url_web: str = Field(description="DSN de PostgreSQL para datos publicos deterministas")
    # Rol minimo de escritura, solo para el esquema analytics (mira_logger).
    database_url_log: str = Field(description="DSN de PostgreSQL para el registro de auditoria")

    pool_min_size: int = 2
    pool_max_size: int = 8
    pool_timeout_s: float = 5.0
    web_pool_max_size: int = 4
    statement_timeout_ms: int = 8000

    # --- modelo de lenguaje ------------------------------------------------
    # No hay catalogo de plantillas: toda pregunta contestable se responde con
    # SQL generado por el modelo, validado antes de ejecutarse. Ver seccion 1.4.
    anthropic_api_key: str = ""
    model_fast: str = "claude-haiku-4-5-20251001"
    # Modelo que genera el SQL. No hay catalogo de consultas en esta version: toda
    # pregunta se responde con SQL generado, validado por sqlglot antes de ejecutarse.
    sql_model: str = "claude-sonnet-5"

    # --- identificacion del solicitante y proteccion del presupuesto -------
    # Firma el token anonimo de cuota (cookie HttpOnly) y el HMAC del prefijo de red
    # que se persiste en vez de la IP. Nunca tiene un valor por defecto: sin el, el
    # servicio no puede emitir cuotas de forma segura.
    token_hmac_secret: str = Field(
        description="Secreto HMAC para el token de cuota y el hash de IP"
    )
    turnstile_secret: str = ""
    #: SameSite de la cookie anonima. En desarrollo el front y la API comparten
    #: origen (localhost) y "lax" es lo correcto. Desplegados en dominios
    #: distintos la peticion es cross-site, y el navegador NO reenvia una
    #: cookie "lax": haria falta "none" (que exige Secure, ya activo). Sin
    #: esto, en produccion cada peticion pareceria de una sesion nueva y la
    #: atribucion del registro de auditoria se perderia en silencio.
    cookie_samesite: Literal["lax", "none", "strict"] = "lax"

    # --- limites -----------------------------------------------------------
    max_question_chars: int = 400
    max_rows: int = 500
    sql_max_attempts: int = Field(default=3, gt=0)
    narrative_max_attempts: int = Field(default=2, gt=0)
    narrative_max_rows_in_prompt: int = Field(default=25, gt=0)
    quota_per_day: int = 5
    quota_per_month: int = 15
    #: Presupuesto inicial (2026-08-18): $75/mes, repartido parejo en el mes.
    budget_daily_usd: float = 2.5
    budget_monthly_usd: float = 75.0
    #: Cortacircuito global (dia + mes) SIEMPRE esta activo -- protege el gasto.
    #: La cuota POR SUJETO (T5.1/T5.2) esta escrita pero deliberadamente inactiva:
    #: decision de producto (2026-08-18) de no limitar por usuario todavia. Se activa
    #: cambiando esta bandera, sin tocar el resto del codigo.
    enable_subject_quota: bool = False

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
