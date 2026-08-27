from __future__ import annotations


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
