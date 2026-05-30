import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app


@pytest.mark.asyncio
async def test_auth_required_for_api_when_secret_configured():
    settings = get_settings()
    previous_secret = settings.web_terminal_auth_secret
    previous_disable = settings.web_terminal_disable_auth_for_tests
    settings.web_terminal_auth_secret = "login-secret"
    settings.web_terminal_disable_auth_for_tests = False
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            rejected = await client.get("/api/clients")
            bad_login = await client.post("/api/auth/login", json={"secret": "wrong"})
            login = await client.post("/api/auth/login", json={"secret": "login-secret"})
            token = login.json()["token"]
            accepted = await client.get("/api/clients", headers={"Authorization": f"Bearer {token}"})
    finally:
        settings.web_terminal_auth_secret = previous_secret
        settings.web_terminal_disable_auth_for_tests = previous_disable

    assert rejected.status_code == 401
    assert bad_login.status_code == 401
    assert login.status_code == 200
    assert accepted.status_code != 401


@pytest.mark.asyncio
async def test_registration_script_is_public_when_secret_configured():
    settings = get_settings()
    previous_secret = settings.web_terminal_auth_secret
    previous_disable = settings.web_terminal_disable_auth_for_tests
    settings.web_terminal_auth_secret = "login-secret"
    settings.web_terminal_disable_auth_for_tests = False
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/clients/register-script")
    finally:
        settings.web_terminal_auth_secret = previous_secret
        settings.web_terminal_disable_auth_for_tests = previous_disable

    assert response.status_code == 200
    assert "WEB_TERMINAL_REGISTRATION_KEY" in response.text
