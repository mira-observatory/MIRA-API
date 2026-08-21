from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mira_api.main import app  # noqa: E402


async def main() -> None:
    async with app.router.lifespan_context(app):
        blocks = getattr(app.state, "sql_system_blocks", [])
        print(f"startup OK: sql_system_blocks={len(blocks)}")


if __name__ == "__main__":
    asyncio.run(main())
