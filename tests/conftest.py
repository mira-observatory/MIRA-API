from __future__ import annotations

import asyncio
import sys

# Mismo motivo que src/mira_api/main.py: psycopg async no funciona sobre el
# ProactorEventLoop, el default de Windows -- las pruebas que abren un pool
# real (test_llm_client.py) necesitan esto antes de que pytest-asyncio cree
# el event loop, no despues.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
