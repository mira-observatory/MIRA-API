from __future__ import annotations

from anthropic import AsyncAnthropic


class ClaudeRefusal(Exception):
    """El modelo devolvio stop_reason == 'refusal'. Nunca se lee response.content
    en ese caso -- puede venir vacio."""

    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"refusal: {category} -- {explanation}")
        self.category = category
        self.explanation = explanation


class ClaudeClient:
    """Envoltorio delgado del SDK. Unico lugar del servicio que importa
    `anthropic`, igual que `db/` es el unico que importa `psycopg`.
    """

    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)

    async def complete_text(
        self,
        *,
        model: str,
        system: list[dict[str, object]],
        messages: list[dict[str, object]],
        max_tokens: int,
    ) -> str:
        """Pide una respuesta de solo texto (sin tools). Devuelve el texto
        concatenado de los bloques `text`.

        Sonnet 5 y Opus 5 rechazan temperature/top_p/top_k con 400 -- no se
        pasan aqui a proposito. `stop_reason` se revisa antes de tocar
        `response.content`: un refusal puede dejarlo vacio.
        """
        response = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
        )

        if response.stop_reason == "refusal":
            details = response.stop_details
            raise ClaudeRefusal(
                category=getattr(details, "category", None),
                explanation=getattr(details, "explanation", None),
            )

        return "".join(block.text for block in response.content if block.type == "text")

    async def cache_read_tokens(
        self,
        *,
        model: str,
        system: list[dict[str, object]],
        messages: list[dict[str, object]],
        max_tokens: int,
    ) -> int:
        """Igual que complete_text, pero devuelve cache_read_input_tokens en vez
        del texto -- solo para la prueba que verifica que el cacheo funciona."""
        response = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
        )
        return response.usage.cache_read_input_tokens or 0
