import pytest
from fastapi.middleware.gzip import GZipMiddleware

from app.main import app


@pytest.mark.asyncio
async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_local_vite_origin_cors_preflight(client):
    response = await client.options(
        "/healthz",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_app_compresses_large_responses():
    assert any(middleware.cls is GZipMiddleware for middleware in app.user_middleware)
