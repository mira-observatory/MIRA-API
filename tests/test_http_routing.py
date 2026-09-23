from __future__ import annotations

import json

import pytest


def test_public_routes_are_not_version_prefixed(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL_QUERY", "postgresql://user:pass@localhost/query")
    monkeypatch.setenv("DATABASE_URL_WEB", "postgresql://user:pass@localhost/web")
    monkeypatch.setenv("DATABASE_URL_LOG", "postgresql://user:pass@localhost/log")
    monkeypatch.setenv("TOKEN_HMAC_SECRET", "test-secret")

    from mira_api.main import app

    paths = set(app.openapi()["paths"])

    assert "/entities/resolve" in paths
    assert "/query" in paths
    assert "/query/stream" in paths
    assert "/coverage" in paths
    assert "/procedures" in paths
    assert "/procedures/statuses" in paths
    assert not any(path.startswith("/v1/") for path in paths)


@pytest.mark.asyncio
async def test_unexpected_stream_failure_is_audited_with_the_returned_id(monkeypatch):
    from mira_api.api.schemas import QueryRequest
    from tests.test_cookie_samesite import _settings
    from tests.test_pipeline import _FakeLogExecutor

    settings = _settings(monkeypatch)
    from mira_api import main

    log = _FakeLogExecutor()
    monkeypatch.setattr(main.app.state, "settings", settings, raising=False)
    monkeypatch.setattr(main.app.state, "log_executor", log, raising=False)
    monkeypatch.setattr(main.app.state, "claude_client", object(), raising=False)
    monkeypatch.setattr(main.app.state, "executor", object(), raising=False)
    monkeypatch.setattr(main.app.state, "sql_system_blocks", [], raising=False)

    async def fail(*args, **kwargs):
        raise RuntimeError("unexpected stream failure")

    monkeypatch.setattr(main, "run_query", fail)
    chunks = [
        chunk
        async for chunk in main._stream_query_events(
            QueryRequest(question="computadoras guatemala", countries=["GT"]), "test"
        )
    ]
    done = json.loads(chunks[-1].decode().split("data: ")[1])
    assert done["outcome"] == "FAILED_INTERNAL_ERROR"
    assert len(log.query_log_rows) == 1
    record = log.query_log_rows[0]
    assert str(record["query_id"]) == done["query_id"]
    assert record["error_stage"] == "stream"
    assert record["error_type"] == "RuntimeError"
