from __future__ import annotations

import pytest

from mira_api.config import get_settings
from mira_api.db.executor import ReadOnlyExecutor
from mira_api.db.pool import build_read_pool
from mira_api.llm.client import ClaudeClient
from mira_api.nlq.semantic_dictionary import load_semantic_dictionary
from mira_api.nlq.sql_generation import build_system_blocks


def _has_real_credentials() -> bool:
    try:
        settings = get_settings()
    except Exception:
        return False
    return bool(settings.anthropic_api_key) and bool(settings.database_url)


pytestmark = pytest.mark.skipif(
    not _has_real_credentials(),
    reason="requiere ANTHROPIC_API_KEY y DATABASE_URL reales -- prueba de integracion",
)


@pytest.mark.asyncio
async def test_el_cacheo_de_prompt_funciona_de_verdad() -> None:
    """T3.1, bloqueante: si esto da cache_read_input_tokens == 0 en la segunda
    llamada, el cacheo esta silenciosamente roto -- casi siempre porque algo
    volatil (una fecha, un UUID) se cuela antes del ultimo cache_control."""
    settings = get_settings()
    pool = build_read_pool(settings)
    await pool.open()
    try:
        executor = ReadOnlyExecutor(pool)
        columns = await load_semantic_dictionary(executor)
        system = build_system_blocks(columns)

        client = ClaudeClient(api_key=settings.anthropic_api_key)
        messages = [{"role": "user", "content": "Paises: CR\nPregunta: cuantos procesos hay"}]

        # No se afirma que la primera llamada tenga cache_read_input_tokens == 0:
        # el cache de Anthropic vive por hash de prompt en su servidor, no por
        # proceso de prueba, asi que una corrida anterior con el mismo prompt
        # (dentro del TTL de ~5 min) puede dejarlo ya caliente. Lo unico que
        # importa es que una llamada identica SI lea del cache.
        await client.cache_read_tokens(
            model=settings.sql_model, system=system, messages=messages, max_tokens=16
        )
        second_tokens = await client.cache_read_tokens(
            model=settings.sql_model, system=system, messages=messages, max_tokens=16
        )
    finally:
        await pool.close()

    assert second_tokens > 0, (
        "el cacheo de prompt no esta funcionando -- revisar que no haya "
        "contenido volatil antes del ultimo cache_control en build_system_blocks"
    )
